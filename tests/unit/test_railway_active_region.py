import pytest
from scripts.railway_active_region import active_region


def _status(*, regions: dict, service_count: int = 1, deployments: list | None = None) -> dict:
    if deployments is None:
        deployments = [
            {
                "status": "SUCCESS",
                "meta": {
                    "serviceManifest": {
                        "deploy": {"multiRegionConfig": regions},
                    }
                },
            }
        ]
    services = [
        {
            "node": {
                "serviceName": "worker",
                "activeDeployments": deployments,
            }
        }
        for _ in range(service_count)
    ]
    return {
        "environments": {
            "edges": [
                {
                    "node": {
                        "name": "production",
                        "serviceInstances": {"edges": services},
                    }
                }
            ]
        }
    }


def test_active_region_requires_one_positive_replica_region():
    status = _status(
        regions={
            "us-east4-eqdc4a": {"numReplicas": 1},
            "us-west2": None,
        }
    )
    assert active_region(status, environment="production", service="worker") == ("us-east4-eqdc4a")


@pytest.mark.parametrize(
    "regions",
    [
        {},
        {"us-east4-eqdc4a": {"numReplicas": 0}},
        {
            "us-east4-eqdc4a": {"numReplicas": 1},
            "us-west2": {"numReplicas": 1},
        },
    ],
)
def test_active_region_rejects_zero_or_multiple_regions(regions: dict):
    with pytest.raises(ValueError, match="railway_active_region_count:worker"):
        active_region(_status(regions=regions), environment="production", service="worker")


def test_active_region_rejects_missing_or_duplicate_service_nodes():
    for service_count in (0, 2):
        with pytest.raises(ValueError, match="railway_service_node_count:worker"):
            active_region(
                _status(regions={}, service_count=service_count),
                environment="production",
                service="worker",
            )


def test_active_region_rejects_missing_successful_deployment():
    status = _status(regions={}, deployments=[{"status": "REMOVED"}])
    with pytest.raises(ValueError, match="railway_active_deployment_missing:worker"):
        active_region(status, environment="production", service="worker")
