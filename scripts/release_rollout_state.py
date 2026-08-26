#!/usr/bin/env python3
"""Pure state validation for a migration-compatible forward rollout."""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_SERVICES = ("api", "worker", "scheduler")


class RolloutStateError(RuntimeError):
    pass


class RolloutPhase(StrEnum):
    PREVIOUS = "previous"
    COMPATIBILITY_LATEST = "compatibility_latest"
    COMPATIBILITY_API = "compatibility_api"
    COMPATIBILITY_WORKER = "compatibility_worker"
    COMPATIBILITY_SCHEDULER = "compatibility_scheduler"
    TARGET_LATEST = "target_latest"
    TARGET_WORKER = "target_worker"
    TARGET_API = "target_api"
    TARGET_SCHEDULER = "target_scheduler"
    COMPLETE = "complete"


class RolloutAction(StrEnum):
    PROMOTE_COMPATIBILITY_LATEST = "promote_compatibility_latest"
    DEPLOY_COMPATIBILITY_API = "deploy_compatibility_api"
    DEPLOY_COMPATIBILITY_WORKER = "deploy_compatibility_worker"
    DEPLOY_COMPATIBILITY_SCHEDULER = "deploy_compatibility_scheduler"
    PROMOTE_TARGET_LATEST = "promote_target_latest"
    DEPLOY_TARGET_WORKER = "deploy_target_worker"
    DEPLOY_TARGET_API = "deploy_target_api"
    DEPLOY_TARGET_SCHEDULER = "deploy_target_scheduler"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True)
class RolloutDirective:
    observed_phase: RolloutPhase
    next_phase: RolloutPhase
    action: RolloutAction


@dataclass(frozen=True, slots=True)
class _Checkpoint:
    phase: RolloutPhase
    state: tuple[str, str, str, str]
    next_phase: RolloutPhase
    action: RolloutAction


def _checkpoints(
    *, previous: str, compatibility: str, target: str
) -> tuple[_Checkpoint, ...]:
    return (
        _Checkpoint(
            RolloutPhase.PREVIOUS,
            (previous, previous, previous, previous),
            RolloutPhase.COMPATIBILITY_LATEST,
            RolloutAction.PROMOTE_COMPATIBILITY_LATEST,
        ),
        _Checkpoint(
            RolloutPhase.COMPATIBILITY_LATEST,
            (compatibility, previous, previous, previous),
            RolloutPhase.COMPATIBILITY_API,
            RolloutAction.DEPLOY_COMPATIBILITY_API,
        ),
        _Checkpoint(
            RolloutPhase.COMPATIBILITY_API,
            (compatibility, compatibility, previous, previous),
            RolloutPhase.COMPATIBILITY_WORKER,
            RolloutAction.DEPLOY_COMPATIBILITY_WORKER,
        ),
        _Checkpoint(
            RolloutPhase.COMPATIBILITY_WORKER,
            (compatibility, compatibility, compatibility, previous),
            RolloutPhase.COMPATIBILITY_SCHEDULER,
            RolloutAction.DEPLOY_COMPATIBILITY_SCHEDULER,
        ),
        _Checkpoint(
            RolloutPhase.COMPATIBILITY_SCHEDULER,
            (compatibility, compatibility, compatibility, compatibility),
            RolloutPhase.TARGET_LATEST,
            RolloutAction.PROMOTE_TARGET_LATEST,
        ),
        _Checkpoint(
            RolloutPhase.TARGET_LATEST,
            (target, compatibility, compatibility, compatibility),
            RolloutPhase.TARGET_WORKER,
            RolloutAction.DEPLOY_TARGET_WORKER,
        ),
        _Checkpoint(
            RolloutPhase.TARGET_WORKER,
            (target, compatibility, target, compatibility),
            RolloutPhase.TARGET_API,
            RolloutAction.DEPLOY_TARGET_API,
        ),
        _Checkpoint(
            RolloutPhase.TARGET_API,
            (target, target, target, compatibility),
            RolloutPhase.TARGET_SCHEDULER,
            RolloutAction.DEPLOY_TARGET_SCHEDULER,
        ),
        _Checkpoint(
            RolloutPhase.TARGET_SCHEDULER,
            (target, target, target, target),
            RolloutPhase.COMPLETE,
            RolloutAction.COMPLETE,
        ),
    )


def validate_rollout_state(
    *,
    latest_digest: str,
    service_digests: Mapping[str, str],
    previous_digest: str,
    target_digest: str,
    compatibility_digest: str,
    recorded_phase: RolloutPhase | str | None = None,
) -> RolloutDirective:
    service_names = set(service_digests)
    expected_services = set(_SERVICES)
    if service_names != expected_services:
        missing = ",".join(sorted(expected_services - service_names)) or "none"
        extra = ",".join(sorted(service_names - expected_services)) or "none"
        raise RolloutStateError(
            "rollout services must be exactly api,worker,scheduler: "
            f"missing={missing}; extra={extra}"
        )

    named_digests = {
        "latest": latest_digest,
        "api": service_digests["api"],
        "worker": service_digests["worker"],
        "scheduler": service_digests["scheduler"],
        "previous": previous_digest,
        "target": target_digest,
        "compatibility": compatibility_digest,
    }
    for name, digest in named_digests.items():
        if not _DIGEST.fullmatch(digest):
            raise RolloutStateError(f"invalid {name} digest: {digest or 'missing'}")

    release_digests = {previous_digest, compatibility_digest, target_digest}
    if len(release_digests) != 3:
        raise RolloutStateError("previous, compatibility, and target digests must be distinct")

    current_state = (
        latest_digest,
        service_digests["api"],
        service_digests["worker"],
        service_digests["scheduler"],
    )
    unrelated = set(current_state) - release_digests
    if unrelated:
        raise RolloutStateError(
            f"rollout state contains unrelated digest: {sorted(unrelated)[0]}"
        )

    checkpoints = _checkpoints(
        previous=previous_digest,
        compatibility=compatibility_digest,
        target=target_digest,
    )
    checkpoint_by_state = {checkpoint.state: checkpoint for checkpoint in checkpoints}
    checkpoint = checkpoint_by_state.get(current_state)
    if checkpoint is None:
        digest_names = {
            previous_digest: "previous",
            compatibility_digest: "compatibility",
            target_digest: "target",
        }
        state_summary = ", ".join(
            f"{name}={digest_names[digest]}"
            for name, digest in zip(("latest", *_SERVICES), current_state, strict=True)
        )
        raise RolloutStateError(
            f"rollout phase regression or out-of-order mutation: {state_summary}"
        )

    phase_indexes = {item.phase: index for index, item in enumerate(checkpoints)}
    phase_indexes[RolloutPhase.COMPLETE] = len(checkpoints) - 1
    if recorded_phase is not None:
        try:
            normalized_recorded_phase = RolloutPhase(recorded_phase)
        except ValueError as exc:
            raise RolloutStateError(f"unknown recorded rollout phase: {recorded_phase}") from exc
        if phase_indexes[checkpoint.phase] < phase_indexes[normalized_recorded_phase]:
            raise RolloutStateError(
                "rollout phase regression: "
                f"observed={checkpoint.phase}; recorded={normalized_recorded_phase}"
            )

    return RolloutDirective(
        observed_phase=checkpoint.phase,
        next_phase=checkpoint.next_phase,
        action=checkpoint.action,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--latest", required=True)
    parser.add_argument("--api", required=True)
    parser.add_argument("--worker", required=True)
    parser.add_argument("--scheduler", required=True)
    parser.add_argument("--previous", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--compatibility", required=True)
    parser.add_argument("--recorded-phase")
    args = parser.parse_args(argv)
    try:
        directive = validate_rollout_state(
            latest_digest=args.latest,
            service_digests={
                "api": args.api,
                "worker": args.worker,
                "scheduler": args.scheduler,
            },
            previous_digest=args.previous,
            target_digest=args.target,
            compatibility_digest=args.compatibility,
            recorded_phase=args.recorded_phase,
        )
    except RolloutStateError as exc:
        parser.error(str(exc))
    print(json.dumps(asdict(directive), separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
