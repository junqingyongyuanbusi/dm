"""Fail-closed state validation for migration-compatible Railway rollback."""

from __future__ import annotations

import argparse
import re
from collections.abc import Mapping

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


class RollbackStateError(RuntimeError):
    pass


def validate_rollback_state(
    *,
    release_status: str,
    latest_digest: str,
    service_digests: Mapping[str, str],
    previous_digest: str,
    target_digest: str,
    compatibility_digest: str,
) -> None:
    if release_status not in {"deploying", "completed"}:
        raise RollbackStateError(f"release status is not rollback-eligible: {release_status}")
    named_digests = {
        "latest": latest_digest,
        "previous": previous_digest,
        "target": target_digest,
        "compatibility": compatibility_digest,
        **service_digests,
    }
    for name, digest in named_digests.items():
        if not _DIGEST.fullmatch(digest):
            raise RollbackStateError(f"invalid {name} digest: {digest or 'missing'}")
    allowed = {previous_digest, target_digest, compatibility_digest}
    current = {latest_digest, *service_digests.values()}
    unrelated = current - allowed
    if unrelated:
        raise RollbackStateError(
            f"rollback state contains unrelated digest: {sorted(unrelated)[0]}"
        )
    if release_status == "deploying" and current == {previous_digest}:
        raise RollbackStateError(
            "release has not mutated production; compatibility rollback is not required"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", required=True)
    parser.add_argument("--latest", required=True)
    parser.add_argument("--api", required=True)
    parser.add_argument("--worker", required=True)
    parser.add_argument("--scheduler", required=True)
    parser.add_argument("--previous", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--compatibility", required=True)
    args = parser.parse_args()
    try:
        validate_rollback_state(
            release_status=args.status,
            latest_digest=args.latest,
            service_digests={
                "api": args.api,
                "worker": args.worker,
                "scheduler": args.scheduler,
            },
            previous_digest=args.previous,
            target_digest=args.target,
            compatibility_digest=args.compatibility,
        )
    except RollbackStateError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
