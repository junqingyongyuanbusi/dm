"""Fail-closed startup gate for an explicitly stopped development queue cutover."""

import argparse
import asyncio
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from apps.cli.retire_workspace_queue import AUDIT_ACTION, CutoverRequest, run_cutover
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.shared.config import Settings, get_settings

ENVIRONMENT_VARIABLE = "WORKSPACE_QUEUE_CUTOVER"
ENVELOPE_FIELDS = frozenset({
    "cutover_id", "before", "tenant", "restore_admin_ids", "namespace", "processes_stopped",
})


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("cutover_duplicate_fields")
    return result


def parse_cutover(raw: str, *, role: str, mode: str, namespace: str) -> CutoverRequest:
    if role not in {"api", "worker", "scheduler"} or mode not in {
        "validate", "apply" if role == "api" else "check",
    }:
        raise ValueError("cutover_role_operation_invalid")
    envelope = json.loads(raw, object_pairs_hook=_unique_object)
    if not isinstance(envelope, dict) or set(envelope) != ENVELOPE_FIELDS:
        raise ValueError("cutover_schema_invalid")
    if envelope["processes_stopped"] is not True or envelope["tenant"] != "default":
        raise ValueError("cutover_stopped_default_tenant_required")
    if any(not isinstance(envelope[field], str) for field in (
        "cutover_id", "before", "namespace",
    )):
        raise ValueError("cutover_string_fields_required")
    cutover_id = uuid.UUID(envelope["cutover_id"])
    if cutover_id.version != 4:
        raise ValueError("cutover_random_uuid_required")
    expected_namespace = f"dramatiq-cutover-{cutover_id.hex}"
    if envelope["namespace"] != expected_namespace or namespace != expected_namespace:
        raise ValueError("cutover_namespace_mismatch")
    admin_values = envelope["restore_admin_ids"]
    if not isinstance(admin_values, list) or any(
        not isinstance(value, str) for value in admin_values
    ):
        raise ValueError("cutover_admin_ids_invalid")
    admin_ids = tuple(uuid.UUID(value) for value in admin_values)
    if len(admin_ids) != 2 or len(set(admin_ids)) != 2:
        raise ValueError("cutover_two_distinct_admin_ids_required")
    before = datetime.fromisoformat(envelope["before"])
    if before.utcoffset() is None or before > datetime.now(UTC):
        raise ValueError("cutover_before_invalid")
    return CutoverRequest(
        tenant="default", cutover_id=cutover_id, before=before.astimezone(UTC),
        restore_admin_ids=admin_ids, apply=True, confirm_processes_stopped=True,
        startup_namespace=expected_namespace,
    )


async def assert_namespace_unused(settings: Settings) -> None:
    namespace = settings.dramatiq_namespace
    async with asyncio.timeout(15):
        async with Redis.from_url(
            settings.redis_url, socket_timeout=5, socket_connect_timeout=5,
        ) as client:
            if await client.exists(namespace):
                raise ValueError("cutover_namespace_already_used")
            async for _key in client.scan_iter(match=f"{namespace}:*", count=500):
                raise ValueError("cutover_namespace_already_used")


async def ensure_cutover(
    session: AsyncSession, request: CutoverRequest, *, role: str, settings: Settings,
) -> None:
    if role not in {"api", "worker", "scheduler"}:
        raise ValueError("cutover_role_invalid")
    if request.startup_namespace != f"dramatiq-cutover-{request.cutover_id.hex}" or (
        request.startup_namespace != settings.dramatiq_namespace
    ):
        raise ValueError("cutover_startup_namespace_required")
    audit = await session.get(models.AuditLog, request.cutover_id)
    if audit is not None:
        if (
            audit.action != AUDIT_ACTION or audit.tenant_id != request.tenant
            or audit.detail.get("parameters") != request.parameters()
        ):
            raise ValueError("cutover_id_parameter_conflict")
        # Only startup-specific audit parameters prove this namespace passed the first-use check.
        return
    if role != "api":
        raise ValueError("cutover_audit_required")
    await session.rollback()
    await assert_namespace_unused(settings)
    # The existing CLI owns the atomic, advisory-locked transaction and idempotency check.
    await run_cutover(session, request)


async def _run(request: CutoverRequest, *, role: str, settings: Settings) -> None:
    request.validate()
    async with get_session_factory()() as session:
        await ensure_cutover(session, request, role=role, settings=settings)


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    operation = parser.add_mutually_exclusive_group(required=True)
    for mode in ("validate", "apply", "check"):
        operation.add_argument(f"--{mode}", dest="mode", action="store_const", const=mode)
    parsed = parser.parse_args(arguments)
    if ENVIRONMENT_VARIABLE not in os.environ:
        return 0
    try:
        settings = get_settings()
        role = os.environ.get("SERVICE_ROLE", "")
        request = parse_cutover(
            os.environ[ENVIRONMENT_VARIABLE], role=role, mode=parsed.mode,
            namespace=settings.dramatiq_namespace,
        )
        request.validate()
        if parsed.mode != "validate":
            asyncio.run(_run(request, role=role, settings=settings))
    except Exception:
        # Never print configuration/connection errors: URLs may contain credentials.
        print(
            "startup cutover refused; no normal-start fallback. Inspect the release manifest "
            "and audit; retain the SAME cutover ID and namespace (details redacted).",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
