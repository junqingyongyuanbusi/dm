"""Capture and compare sanitized Railway source-migration configuration evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ID = "abcf3199-e5ac-415b-a22e-062206390331"
ENVIRONMENT_ID = "db0d6750-eb77-40ee-8a79-f706cd1f828a"
SERVICES = {
    "api": "b84107eb-c945-4279-92aa-c4691532d9ec",
    "worker": "71034ef8-cb9c-44e6-b4da-2b3f9869cd4e",
    "scheduler": "4646baef-e71f-4ffe-b4e2-244afdeec6ce",
}

_QUERY = """
query ServiceSnapshot($projectId: String!, $environmentId: String!, $serviceId: String!) {
  serviceInstance(environmentId: $environmentId, serviceId: $serviceId) {
    serviceName
    source { repo image }
    numReplicas
    region
    restartPolicyType
    restartPolicyMaxRetries
    domains {
      customDomains { domain targetPort }
      serviceDomains { domain targetPort }
    }
    activeDeployments { id status meta }
    latestDeployment { id status meta }
  }
  renderedVariables: variables(
    projectId: $projectId,
    environmentId: $environmentId,
    serviceId: $serviceId
  )
  unrenderedVariables: variables(
    projectId: $projectId,
    environmentId: $environmentId,
    serviceId: $serviceId,
    unrendered: true
  )
}
"""


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _sorted_domains(domains: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return {
        key: sorted(
            (
                {"domain": row.get("domain"), "targetPort": row.get("targetPort")}
                for row in domains.get(key, [])
            ),
            key=lambda row: (row["domain"] or "", row["targetPort"] or 0),
        )
        for key in ("customDomains", "serviceDomains")
    }


def _active_regions(instance: dict[str, Any]) -> list[str]:
    regions: set[str] = set()
    for deployment in instance.get("activeDeployments", []):
        if deployment.get("status") != "SUCCESS":
            continue
        config = (
            deployment.get("meta", {})
            .get("serviceManifest", {})
            .get("deploy", {})
            .get("multiRegionConfig", {})
        )
        for region, value in config.items():
            if (value or {}).get("numReplicas", 0) > 0:
                regions.add(region)
    return sorted(regions)


def normalized_configuration(response: dict[str, Any]) -> dict[str, Any]:
    data = response["data"]
    instance = data["serviceInstance"]
    unrendered = {
        key: value
        for key, value in data["unrenderedVariables"].items()
        if not key.startswith("RAILWAY_")
    }
    return {
        "variables": unrendered,
        "runtime": {
            "numReplicas": instance.get("numReplicas"),
            "region": instance.get("region"),
            "activeRegions": _active_regions(instance),
            "restartPolicyType": instance.get("restartPolicyType"),
            "restartPolicyMaxRetries": instance.get("restartPolicyMaxRetries"),
            "domains": _sorted_domains(instance.get("domains", {})),
        },
    }


def configuration_fingerprint(response: dict[str, Any]) -> str:
    canonical = json.dumps(
        normalized_configuration(response), sort_keys=True, separators=(",", ":")
    )
    return _sha256(canonical)


def sanitized_summary(response: dict[str, Any]) -> dict[str, Any]:
    data = response["data"]
    instance = data["serviceInstance"]
    rendered = data["renderedVariables"]
    unrendered = data["unrenderedVariables"]
    deployment = instance.get("latestDeployment") or {}
    meta = deployment.get("meta") or {}
    return {
        "service_id": SERVICES[instance["serviceName"]],
        "source": instance.get("source"),
        "deployment_id": deployment.get("id"),
        "deployment_status": deployment.get("status"),
        "image_digest": meta.get("imageDigest"),
        "commit_hash": meta.get("commitHash"),
        "service_role": rendered.get("SERVICE_ROLE"),
        "configuration_sha256": configuration_fingerprint(response),
        "database_url_sha256": _sha256(unrendered.get("DATABASE_URL", "")),
        "redis_url_sha256": _sha256(unrendered.get("REDIS_URL", "")),
        "runtime": normalized_configuration(response)["runtime"],
    }


def _query_service(service_id: str) -> dict[str, Any]:
    variables = json.dumps(
        {
            "projectId": PROJECT_ID,
            "environmentId": ENVIRONMENT_ID,
            "serviceId": service_id,
        },
        separators=(",", ":"),
    )
    result = subprocess.run(
        ["railway", "api", _QUERY, "--variables", variables],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError("railway_snapshot_query_failed")
    response = json.loads(result.stdout)
    if response.get("errors"):
        raise RuntimeError("railway_snapshot_graphql_errors")
    return response


def capture(path: Path) -> None:
    evidence = {
        "captured_at": datetime.now(UTC).isoformat(),
        "project_id": PROJECT_ID,
        "environment_id": ENVIRONMENT_ID,
        "services": {
            service: sanitized_summary(_query_service(service_id))
            for service, service_id in SERVICES.items()
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")


def compare(before_path: Path, after_path: Path) -> None:
    before = json.loads(before_path.read_text())
    after = json.loads(after_path.read_text())
    errors: list[str] = []
    for service in SERVICES:
        old = before["services"][service]
        new = after["services"][service]
        for key in (
            "configuration_sha256",
            "database_url_sha256",
            "redis_url_sha256",
            "service_role",
        ):
            if old.get(key) != new.get(key):
                errors.append(f"{service}:{key}_changed")
    if errors:
        raise RuntimeError("railway_source_configuration_changed:" + ",".join(errors))


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    fingerprint_parser = subparsers.add_parser("fingerprint")
    fingerprint_parser.add_argument("--input", default="-")
    capture_parser = subparsers.add_parser("capture")
    capture_parser.add_argument("path", type=Path)
    compare_parser = subparsers.add_parser("compare")
    compare_parser.add_argument("before", type=Path)
    compare_parser.add_argument("after", type=Path)
    args = parser.parse_args()

    if args.command == "fingerprint":
        if args.input == "-":
            response = json.load(sys.stdin)
        else:
            response = json.loads(Path(args.input).read_text())
        print(configuration_fingerprint(response))
    elif args.command == "capture":
        capture(args.path)
    else:
        compare(args.before, args.after)


if __name__ == "__main__":
    main()
