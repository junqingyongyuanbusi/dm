import json

import pytest
from scripts.release_rollout_state import (
    RolloutAction,
    RolloutDirective,
    RolloutPhase,
    RolloutStateError,
    main,
    validate_rollout_state,
)

_PREVIOUS = "sha256:" + "1" * 64
_TARGET = "sha256:" + "2" * 64
_COMPATIBILITY = "sha256:" + "3" * 64


def _validate(
    state: tuple[str, str, str, str],
    *,
    recorded_phase: RolloutPhase | str | None = None,
) -> RolloutDirective:
    latest, api, worker, scheduler = state
    return validate_rollout_state(
        latest_digest=latest,
        service_digests={"api": api, "worker": worker, "scheduler": scheduler},
        previous_digest=_PREVIOUS,
        target_digest=_TARGET,
        compatibility_digest=_COMPATIBILITY,
        recorded_phase=recorded_phase,
    )


@pytest.mark.parametrize(
    ("state", "observed_phase", "next_phase", "action"),
    [
        (
            (_PREVIOUS, _PREVIOUS, _PREVIOUS, _PREVIOUS),
            RolloutPhase.PREVIOUS,
            RolloutPhase.COMPATIBILITY_LATEST,
            RolloutAction.PROMOTE_COMPATIBILITY_LATEST,
        ),
        (
            (_COMPATIBILITY, _PREVIOUS, _PREVIOUS, _PREVIOUS),
            RolloutPhase.COMPATIBILITY_LATEST,
            RolloutPhase.COMPATIBILITY_API,
            RolloutAction.DEPLOY_COMPATIBILITY_API,
        ),
        (
            (_COMPATIBILITY, _COMPATIBILITY, _PREVIOUS, _PREVIOUS),
            RolloutPhase.COMPATIBILITY_API,
            RolloutPhase.COMPATIBILITY_WORKER,
            RolloutAction.DEPLOY_COMPATIBILITY_WORKER,
        ),
        (
            (_COMPATIBILITY, _COMPATIBILITY, _COMPATIBILITY, _PREVIOUS),
            RolloutPhase.COMPATIBILITY_WORKER,
            RolloutPhase.COMPATIBILITY_SCHEDULER,
            RolloutAction.DEPLOY_COMPATIBILITY_SCHEDULER,
        ),
        (
            (_COMPATIBILITY, _COMPATIBILITY, _COMPATIBILITY, _COMPATIBILITY),
            RolloutPhase.COMPATIBILITY_SCHEDULER,
            RolloutPhase.TARGET_LATEST,
            RolloutAction.PROMOTE_TARGET_LATEST,
        ),
        (
            (_TARGET, _COMPATIBILITY, _COMPATIBILITY, _COMPATIBILITY),
            RolloutPhase.TARGET_LATEST,
            RolloutPhase.TARGET_WORKER,
            RolloutAction.DEPLOY_TARGET_WORKER,
        ),
        (
            (_TARGET, _COMPATIBILITY, _TARGET, _COMPATIBILITY),
            RolloutPhase.TARGET_WORKER,
            RolloutPhase.TARGET_API,
            RolloutAction.DEPLOY_TARGET_API,
        ),
        (
            (_TARGET, _TARGET, _TARGET, _COMPATIBILITY),
            RolloutPhase.TARGET_API,
            RolloutPhase.TARGET_SCHEDULER,
            RolloutAction.DEPLOY_TARGET_SCHEDULER,
        ),
        (
            (_TARGET, _TARGET, _TARGET, _TARGET),
            RolloutPhase.TARGET_SCHEDULER,
            RolloutPhase.COMPLETE,
            RolloutAction.COMPLETE,
        ),
    ],
)
def test_returns_only_the_next_safe_rollout_action(
    state: tuple[str, str, str, str],
    observed_phase: RolloutPhase,
    next_phase: RolloutPhase,
    action: RolloutAction,
) -> None:
    assert _validate(state) == RolloutDirective(observed_phase, next_phase, action)


@pytest.mark.parametrize(
    "state",
    [
        (_TARGET, _PREVIOUS, _PREVIOUS, _PREVIOUS),
        (_COMPATIBILITY, _COMPATIBILITY, _TARGET, _PREVIOUS),
        (_TARGET, _TARGET, _COMPATIBILITY, _COMPATIBILITY),
        (_TARGET, _COMPATIBILITY, _TARGET, _TARGET),
        (_COMPATIBILITY, _COMPATIBILITY, _TARGET, _COMPATIBILITY),
    ],
)
def test_rejects_skipped_reordered_and_regressed_states(
    state: tuple[str, str, str, str],
) -> None:
    with pytest.raises(RolloutStateError, match="phase regression or out-of-order"):
        _validate(state)


def test_rejects_regression_behind_the_recorded_phase() -> None:
    state = (_COMPATIBILITY, _COMPATIBILITY, _COMPATIBILITY, _COMPATIBILITY)

    with pytest.raises(
        RolloutStateError,
        match="phase regression: observed=compatibility_scheduler; recorded=target_worker",
    ):
        _validate(state, recorded_phase=RolloutPhase.TARGET_WORKER)


def test_allows_recovery_ahead_of_the_recorded_phase() -> None:
    state = (_TARGET, _COMPATIBILITY, _TARGET, _COMPATIBILITY)

    directive = _validate(state, recorded_phase=RolloutPhase.COMPATIBILITY_SCHEDULER)

    assert directive.observed_phase is RolloutPhase.TARGET_WORKER
    assert directive.action is RolloutAction.DEPLOY_TARGET_API


def test_rejects_unrelated_malformed_and_ambiguous_digests() -> None:
    unrelated = "sha256:" + "4" * 64
    with pytest.raises(RolloutStateError, match="unrelated digest"):
        _validate((_PREVIOUS, _PREVIOUS, unrelated, _PREVIOUS))

    with pytest.raises(RolloutStateError, match="invalid latest digest"):
        _validate(("not-a-digest", _PREVIOUS, _PREVIOUS, _PREVIOUS))

    with pytest.raises(RolloutStateError, match="must be distinct"):
        validate_rollout_state(
            latest_digest=_PREVIOUS,
            service_digests={
                "api": _PREVIOUS,
                "worker": _PREVIOUS,
                "scheduler": _PREVIOUS,
            },
            previous_digest=_PREVIOUS,
            target_digest=_TARGET,
            compatibility_digest=_TARGET,
        )


def test_rejects_missing_extra_and_unknown_phase_inputs() -> None:
    with pytest.raises(RolloutStateError, match="missing=scheduler"):
        validate_rollout_state(
            latest_digest=_PREVIOUS,
            service_digests={"api": _PREVIOUS, "worker": _PREVIOUS},
            previous_digest=_PREVIOUS,
            target_digest=_TARGET,
            compatibility_digest=_COMPATIBILITY,
        )

    with pytest.raises(RolloutStateError, match="extra=beat"):
        validate_rollout_state(
            latest_digest=_PREVIOUS,
            service_digests={
                "api": _PREVIOUS,
                "worker": _PREVIOUS,
                "scheduler": _PREVIOUS,
                "beat": _PREVIOUS,
            },
            previous_digest=_PREVIOUS,
            target_digest=_TARGET,
            compatibility_digest=_COMPATIBILITY,
        )

    with pytest.raises(RolloutStateError, match="unknown recorded rollout phase"):
        _validate(
            (_PREVIOUS, _PREVIOUS, _PREVIOUS, _PREVIOUS),
            recorded_phase="target_database",
        )


def test_cli_emits_a_machine_readable_directive(capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        main(
            [
                "--latest",
                _TARGET,
                "--api",
                _COMPATIBILITY,
                "--worker",
                _TARGET,
                "--scheduler",
                _COMPATIBILITY,
                "--previous",
                _PREVIOUS,
                "--target",
                _TARGET,
                "--compatibility",
                _COMPATIBILITY,
                "--recorded-phase",
                RolloutPhase.TARGET_LATEST,
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == {
        "action": "deploy_target_api",
        "next_phase": "target_api",
        "observed_phase": "target_worker",
    }
