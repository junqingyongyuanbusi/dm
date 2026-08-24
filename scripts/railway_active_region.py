#!/usr/bin/env python3
import json
import sys
from collections.abc import Sequence
from typing import Any


def _active_deployment(service_node: dict[str, Any]) -> dict[str, Any] | None:
    for deployment in service_node.get("activeDeployments") or []:
        if deployment.get("status") == "SUCCESS":
            return deployment
    return None


def active_region(status: dict[str, Any], *, environment: str, service: str) -> str:
    environments = status.get("environments", {}).get("edges") or []
    matches = []
    for environment_edge in environments:
        environment_node = environment_edge.get("node") or {}
        if environment_node.get("name") != environment:
            continue
        instances = environment_node.get("serviceInstances", {}).get("edges") or []
        matches.extend(
            instance_edge.get("node") or {}
            for instance_edge in instances
            if (instance_edge.get("node") or {}).get("serviceName") == service
        )
    if len(matches) != 1:
        raise ValueError(f"railway_service_node_count:{service}:{len(matches)}")
    deployment = _active_deployment(matches[0])
    if deployment is None:
        raise ValueError(f"railway_active_deployment_missing:{service}")
    regions = (
        deployment.get("meta", {})
        .get("serviceManifest", {})
        .get("deploy", {})
        .get("multiRegionConfig", {})
    )
    active = sorted(
        name
        for name, config in regions.items()
        if isinstance(config, dict) and int(config.get("numReplicas") or 0) > 0
    )
    if len(active) != 1:
        raise ValueError(f"railway_active_region_count:{service}:{len(active)}")
    return active[0]


def main(argv: Sequence[str]) -> int:
    if len(argv) != 3:
        print("usage: railway_active_region.py ENVIRONMENT SERVICE", file=sys.stderr)
        return 2
    try:
        status = json.load(sys.stdin)
        print(active_region(status, environment=argv[1], service=argv[2]))
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
