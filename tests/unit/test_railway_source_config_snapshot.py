import json

import pytest
from scripts.railway_source_config_snapshot import (
    compare,
    configuration_fingerprint,
    sanitized_summary,
)


def _response(*, deployment_id: str = "dep-1", railway_sha: str = "one"):
    return {
        "data": {
            "serviceInstance": {
                "serviceName": "api",
                "source": {"repo": "junqingyongyuanbusi/dm", "image": None},
                "numReplicas": None,
                "region": None,
                "restartPolicyType": "ON_FAILURE",
                "restartPolicyMaxRetries": 10,
                "domains": {
                    "customDomains": [{"domain": "relay.nexory.top", "targetPort": 8080}],
                    "serviceDomains": [],
                },
                "activeDeployments": [
                    {
                        "id": deployment_id,
                        "status": "SUCCESS",
                        "meta": {
                            "commitHash": "a" * 40,
                            "serviceManifest": {
                                "deploy": {
                                    "multiRegionConfig": {
                                        "us-east4-eqdc4a": {"numReplicas": 1},
                                        "us-west2": {"numReplicas": 0},
                                    }
                                }
                            },
                        },
                    }
                ],
                "latestDeployment": {
                    "id": deployment_id,
                    "status": "SUCCESS",
                    "meta": {"commitHash": "a" * 40},
                },
            },
            "renderedVariables": {"SERVICE_ROLE": "api"},
            "unrenderedVariables": {
                "SERVICE_ROLE": "api",
                "DATABASE_URL": "${{Postgres.DATABASE_URL}}",
                "REDIS_URL": "${{Redis.REDIS_URL}}",
                "RAILWAY_DEPLOYMENT_ID": deployment_id,
                "RAILWAY_GIT_COMMIT_SHA": railway_sha,
            },
        }
    }


def test_fingerprint_excludes_railway_generated_values():
    assert configuration_fingerprint(_response()) == configuration_fingerprint(
        _response(deployment_id="dep-2", railway_sha="two")
    )


def test_summary_keeps_only_sanitized_evidence():
    summary = sanitized_summary(_response())
    assert summary["service_role"] == "api"
    assert summary["runtime"]["activeRegions"] == ["us-east4-eqdc4a"]
    assert "DATABASE_URL" not in json.dumps(summary)
    assert "database_url_sha256" in summary


def test_compare_rejects_configuration_drift(tmp_path):
    before = {"services": {"api": sanitized_summary(_response())}}
    before["services"]["worker"] = {**before["services"]["api"], "service_role": "worker"}
    before["services"]["scheduler"] = {
        **before["services"]["api"],
        "service_role": "scheduler",
    }
    after = json.loads(json.dumps(before))
    after["services"]["worker"]["configuration_sha256"] = "changed"
    before_path = tmp_path / "before.json"
    after_path = tmp_path / "after.json"
    before_path.write_text(json.dumps(before))
    after_path.write_text(json.dumps(after))
    with pytest.raises(RuntimeError, match="worker:configuration_sha256_changed"):
        compare(before_path, after_path)
