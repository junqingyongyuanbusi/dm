import pytest
from scripts.rollback_state_guard import RollbackStateError, validate_rollback_state

_PREVIOUS = "sha256:" + "1" * 64
_TARGET = "sha256:" + "2" * 64
_COMPAT = "sha256:" + "3" * 64


def _validate(
    *, status="deploying", latest=_TARGET, api=_TARGET, worker=_PREVIOUS, scheduler=_PREVIOUS
):
    validate_rollback_state(
        release_status=status,
        latest_digest=latest,
        service_digests={"api": api, "worker": worker, "scheduler": scheduler},
        previous_digest=_PREVIOUS,
        target_digest=_TARGET,
        compatibility_digest=_COMPAT,
    )


def test_all_previous_state_is_not_rollback_eligible() -> None:
    with pytest.raises(RollbackStateError, match="has not mutated production"):
        _validate(latest=_PREVIOUS, api=_PREVIOUS, worker=_PREVIOUS, scheduler=_PREVIOUS)


def test_completed_release_with_all_previous_requires_compatibility_recovery() -> None:
    _validate(
        status="completed",
        latest=_PREVIOUS,
        api=_PREVIOUS,
        worker=_PREVIOUS,
        scheduler=_PREVIOUS,
    )


def test_partial_target_rollout_is_rollback_eligible() -> None:
    _validate(latest=_TARGET, api=_TARGET, worker=_PREVIOUS, scheduler=_PREVIOUS)
    _validate(latest=_TARGET, api=_PREVIOUS, worker=_PREVIOUS, scheduler=_PREVIOUS)


def test_unrelated_digest_and_prepared_manifest_fail_closed() -> None:
    with pytest.raises(RollbackStateError, match="unrelated digest"):
        _validate(worker="sha256:" + "4" * 64)
    with pytest.raises(RollbackStateError, match="not rollback-eligible"):
        _validate(status="prepared")
