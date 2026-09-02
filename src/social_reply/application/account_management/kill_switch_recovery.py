import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import redis.asyncio as aioredis
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.shared.config import get_settings

logger = logging.getLogger(__name__)

ACCOUNT_KILL_SWITCH_ACTION = "SET_PLATFORM_ACCOUNT_KILL_SWITCH"
ACCOUNT_KILL_SWITCH_SUBJECT_TYPE = "platform_account"

_RECONCILABLE_STATUSES = frozenset({"PENDING", "UNKNOWN"})
_DEFAULT_BATCH_SIZE = 100
_MAX_BATCH_SIZE = 500
_REDIS_OPERATION_TIMEOUT_SECONDS = 5.0
_TRANSIENT_DETAIL_FIELDS = frozenset(
    {
        "error_code",
        "error_type",
        "fail_closed",
        "superseded_by_operation_id",
    }
)


class _RedisLike(Protocol):
    async def exists(self, key: str) -> int: ...

    async def set(self, key: str, value: str) -> Any: ...

    async def delete(self, key: str) -> int: ...

    async def aclose(self) -> None: ...


@dataclass(frozen=True)
class _ScopeReconciliationResult:
    resolved_operation_ids: tuple[uuid.UUID, ...]
    requested_status: str | None
    redis_error: Exception | None


def account_kill_switch_redis_key(tenant_id: str, account_id: uuid.UUID) -> str:
    return f"killswitch:account:{tenant_id}:{account_id}"


def _account_kill_switch_lock_key(tenant_id: str, account_id: uuid.UUID) -> str:
    return f"social-reply:account-kill-switch:{tenant_id}:{account_id}"


async def acquire_account_kill_switch_lock(
    session: AsyncSession,
    *,
    tenant_id: str,
    account_id: uuid.UUID,
) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": _account_kill_switch_lock_key(tenant_id, account_id)},
    )


def _command_status(detail: dict[str, Any]) -> str:
    status = detail.get("status", detail.get("outcome", ""))
    return status if isinstance(status, str) else ""


def _command_sequence(detail: dict[str, Any]) -> int:
    sequence = detail.get("account_sequence")
    return sequence if isinstance(sequence, int) and not isinstance(sequence, bool) else 0


def _command_sort_key(audit: models.AuditLog) -> tuple[int, datetime, str]:
    created_at = audit.created_at or datetime.min.replace(tzinfo=UTC)
    return _command_sequence(dict(audit.detail or {})), created_at, str(audit.id)


def _latest_command(audits: list[models.AuditLog]) -> models.AuditLog:
    command_sequences = [_command_sequence(dict(audit.detail or {})) for audit in audits]
    all_commands_are_sequenced = all(sequence > 0 for sequence in command_sequences)
    sequences_are_unique = len(set(command_sequences)) == len(command_sequences)
    if all_commands_are_sequenced and sequences_are_unique:
        return max(audits, key=_command_sort_key)
    return max(
        audits,
        key=lambda audit: (
            audit.created_at or datetime.min.replace(tzinfo=UTC),
            str(audit.id),
        ),
    )


async def next_account_kill_switch_sequence(
    session: AsyncSession,
    *,
    tenant_id: str,
    account_id: uuid.UUID,
) -> int:
    existing_details = (
        await session.scalars(
            select(models.AuditLog.detail).where(
                models.AuditLog.tenant_id == tenant_id,
                models.AuditLog.action == ACCOUNT_KILL_SWITCH_ACTION,
                models.AuditLog.subject_type == ACCOUNT_KILL_SWITCH_SUBJECT_TYPE,
                models.AuditLog.subject_id == str(account_id),
            )
        )
    ).all()
    highest_sequence = max(
        (_command_sequence(dict(detail or {})) for detail in existing_details),
        default=0,
    )
    return highest_sequence + 1


def build_pending_account_kill_switch_detail(
    *,
    operation_id: uuid.UUID,
    tenant_id: str,
    account_id: uuid.UUID,
    target_enabled: bool,
    account_sequence: int,
    actor_role: str,
    owner_user_id: uuid.UUID | None,
) -> dict[str, Any]:
    return {
        "operation_id": str(operation_id),
        "tenant_id": tenant_id,
        "account_id": str(account_id),
        "account_sequence": account_sequence,
        "target_enabled": target_enabled,
        # Retain the original field for existing audit readers while making the target explicit.
        "enabled": target_enabled,
        "actor_role": actor_role,
        "owner_user_id": str(owner_user_id) if owner_user_id else None,
        "status": "PENDING",
        "outcome": "PENDING",
        "attempt_count": 0,
    }


def _normalized_detail(
    audit: models.AuditLog,
    *,
    tenant_id: str,
    account_id: uuid.UUID,
) -> dict[str, Any]:
    detail = dict(audit.detail or {})
    target_enabled = detail.get("target_enabled")
    legacy_enabled = detail.get("enabled")
    if not isinstance(target_enabled, bool) and isinstance(legacy_enabled, bool):
        target_enabled = legacy_enabled
    if isinstance(target_enabled, bool):
        detail = {
            **detail,
            "target_enabled": target_enabled,
            "enabled": target_enabled,
        }
    return {
        **detail,
        "operation_id": detail.get("operation_id", str(audit.id)),
        "tenant_id": detail.get("tenant_id", tenant_id),
        "account_id": detail.get("account_id", str(account_id)),
        "status": _command_status(detail) or "PENDING",
        "outcome": _command_status(detail) or "PENDING",
        "attempt_count": detail.get("attempt_count", 0),
    }


def _detail_with_status(
    detail: dict[str, Any],
    status: str,
    **updates: Any,
) -> dict[str, Any]:
    stable_detail = {
        key: value for key, value in detail.items() if key not in _TRANSIENT_DETAIL_FIELDS
    }
    return {
        **stable_detail,
        **updates,
        "status": status,
        "outcome": status,
        "last_attempt_at": datetime.now(UTC).isoformat(),
    }


def _target_enabled(detail: dict[str, Any]) -> bool | None:
    target = detail.get("target_enabled")
    if isinstance(target, bool):
        return target
    legacy_target = detail.get("enabled")
    return legacy_target if isinstance(legacy_target, bool) else None


def _scope_validation_error(
    audit: models.AuditLog,
    detail: dict[str, Any],
    account: models.PlatformAccount | None,
    *,
    tenant_id: str,
    account_id: uuid.UUID,
) -> str | None:
    if detail.get("operation_id") != str(audit.id):
        return "KILL_SWITCH_OPERATION_ID_MISMATCH"
    if detail.get("tenant_id") != tenant_id or detail.get("account_id") != str(account_id):
        return "KILL_SWITCH_SCOPE_MISMATCH"
    if account is None:
        return "ACCOUNT_SCOPE_NOT_FOUND"
    actor_role = detail.get("actor_role")
    if actor_role == "ADMIN":
        return None
    if actor_role != "USER":
        return "KILL_SWITCH_AUTHORITY_UNCERTAIN"
    expected_owner_user_id = detail.get("owner_user_id")
    current_owner_user_id = str(account.owner_user_id) if account.owner_user_id else None
    has_expected_owner = isinstance(expected_owner_user_id, str)
    if not has_expected_owner or expected_owner_user_id != current_owner_user_id:
        return "ACCOUNT_OWNERSHIP_CHANGED"
    return None


async def _run_redis_operation(operation) -> Any:
    return await asyncio.wait_for(operation, timeout=_REDIS_OPERATION_TIMEOUT_SECONDS)


async def _force_fail_closed(redis: _RedisLike, redis_key: str) -> bool:
    try:
        await _run_redis_operation(redis.set(redis_key, "1"))
        enabled = bool(await _run_redis_operation(redis.exists(redis_key)))
    except Exception:  # noqa: BLE001 - UNKNOWN must remain durable when Redis is unavailable
        return False
    return enabled


async def _apply_target(
    redis: _RedisLike,
    *,
    redis_key: str,
    target_enabled: bool,
) -> tuple[bool, bool]:
    previous_enabled = bool(await _run_redis_operation(redis.exists(redis_key)))
    if previous_enabled == target_enabled:
        return previous_enabled, False
    if target_enabled:
        await _run_redis_operation(redis.set(redis_key, "1"))
    else:
        await _run_redis_operation(redis.delete(redis_key))
    applied_enabled = bool(await _run_redis_operation(redis.exists(redis_key)))
    if applied_enabled != target_enabled:
        raise RuntimeError("kill_switch_redis_verification_failed")
    return previous_enabled, True


async def _reconcile_account_scope(
    *,
    tenant_id: str,
    account_id: uuid.UUID,
    redis: _RedisLike,
    requested_operation_id: uuid.UUID | None = None,
) -> _ScopeReconciliationResult:
    resolved_operation_ids: list[uuid.UUID] = []
    requested_status: str | None = None
    redis_error: Exception | None = None
    async with get_session_factory()() as session:
        await acquire_account_kill_switch_lock(
            session,
            tenant_id=tenant_id,
            account_id=account_id,
        )
        account = await session.scalar(
            select(models.PlatformAccount)
            .where(
                models.PlatformAccount.tenant_id == tenant_id,
                models.PlatformAccount.id == account_id,
            )
            .with_for_update()
        )
        audits = list(
            await session.scalars(
                select(models.AuditLog)
                .where(
                    models.AuditLog.tenant_id == tenant_id,
                    models.AuditLog.action == ACCOUNT_KILL_SWITCH_ACTION,
                    models.AuditLog.subject_type == ACCOUNT_KILL_SWITCH_SUBJECT_TYPE,
                    models.AuditLog.subject_id == str(account_id),
                )
                .with_for_update()
            )
        )
        if not audits:
            await session.commit()
            return _ScopeReconciliationResult((), None, None)

        latest_audit = _latest_command(audits)
        latest_detail = _normalized_detail(
            latest_audit,
            tenant_id=tenant_id,
            account_id=account_id,
        )
        latest_audit.detail = latest_detail
        for audit in audits:
            detail = _normalized_detail(audit, tenant_id=tenant_id, account_id=account_id)
            if audit.id != latest_audit.id and _command_status(detail) in _RECONCILABLE_STATUSES:
                audit.detail = _detail_with_status(
                    detail,
                    "SUPERSEDED",
                    changed=False,
                    superseded_by_operation_id=str(latest_audit.id),
                )
                resolved_operation_ids.append(audit.id)

        latest_status = _command_status(latest_detail)
        if latest_status not in _RECONCILABLE_STATUSES:
            if requested_operation_id == latest_audit.id:
                requested_status = latest_status
            await session.commit()
            return _ScopeReconciliationResult(
                tuple(resolved_operation_ids),
                requested_status,
                None,
            )

        redis_key = account_kill_switch_redis_key(tenant_id, account_id)
        scope_error = _scope_validation_error(
            latest_audit,
            latest_detail,
            account,
            tenant_id=tenant_id,
            account_id=account_id,
        )
        target_enabled = _target_enabled(latest_detail)
        if scope_error is not None or target_enabled is None:
            error_code = scope_error or "KILL_SWITCH_TARGET_UNCERTAIN"
            fail_closed = await _force_fail_closed(redis, redis_key)
            latest_audit.detail = _detail_with_status(
                latest_detail,
                "UNKNOWN",
                error_code=error_code,
                fail_closed=fail_closed,
                attempt_count=int(latest_detail.get("attempt_count", 0)) + 1,
            )
            if requested_operation_id == latest_audit.id:
                requested_status = "UNKNOWN"
            await session.commit()
            return _ScopeReconciliationResult(
                tuple(resolved_operation_ids),
                requested_status,
                None,
            )

        attempt_count = int(latest_detail.get("attempt_count", 0)) + 1
        try:
            previous_enabled, changed = await _apply_target(
                redis,
                redis_key=redis_key,
                target_enabled=target_enabled,
            )
        except Exception as exc:  # noqa: BLE001 - persist ambiguity before propagating
            fail_closed = await _force_fail_closed(redis, redis_key)
            latest_audit.detail = _detail_with_status(
                latest_detail,
                "UNKNOWN",
                error_code="REDIS_APPLY_UNCERTAIN",
                error_type=type(exc).__name__,
                fail_closed=fail_closed,
                attempt_count=attempt_count,
            )
            redis_error = exc
            if requested_operation_id == latest_audit.id:
                requested_status = "UNKNOWN"
        else:
            terminal_status = "APPLIED" if changed else "UNCHANGED"
            latest_audit.detail = _detail_with_status(
                latest_detail,
                terminal_status,
                previous_enabled=previous_enabled,
                changed=changed,
                attempt_count=attempt_count,
                applied_at=datetime.now(UTC).isoformat(),
            )
            resolved_operation_ids.append(latest_audit.id)
            if requested_operation_id == latest_audit.id:
                requested_status = terminal_status
        await session.commit()
    return _ScopeReconciliationResult(
        tuple(resolved_operation_ids),
        requested_status,
        redis_error,
    )


async def reconcile_account_kill_switch_command(
    operation_id: uuid.UUID,
    *,
    raise_on_redis_error: bool = False,
) -> str | None:
    async with get_session_factory()() as session:
        audit = await session.get(models.AuditLog, operation_id)
    if (
        audit is None
        or audit.action != ACCOUNT_KILL_SWITCH_ACTION
        or audit.subject_type != ACCOUNT_KILL_SWITCH_SUBJECT_TYPE
    ):
        raise RuntimeError("kill_switch_audit_not_found")
    try:
        account_id = uuid.UUID(audit.subject_id)
    except ValueError as exc:
        raise RuntimeError("kill_switch_account_id_invalid") from exc

    redis = aioredis.from_url(get_settings().redis_url)
    try:
        result = await _reconcile_account_scope(
            tenant_id=audit.tenant_id,
            account_id=account_id,
            redis=redis,
            requested_operation_id=operation_id,
        )
    finally:
        await redis.aclose()
    if raise_on_redis_error and result.redis_error is not None:
        raise result.redis_error
    return result.requested_status


async def sweep_account_kill_switch_commands(
    *,
    batch_size: int = _DEFAULT_BATCH_SIZE,
) -> list[uuid.UUID]:
    bounded_batch_size = max(1, min(batch_size, _MAX_BATCH_SIZE))
    status = func.coalesce(
        models.AuditLog.detail["status"].astext,
        models.AuditLog.detail["outcome"].astext,
    )
    async with get_session_factory()() as session:
        account_scopes = (
            await session.execute(
                select(
                    models.AuditLog.tenant_id,
                    models.AuditLog.subject_id,
                    func.min(models.AuditLog.created_at).label("oldest_pending_at"),
                )
                .where(
                    models.AuditLog.action == ACCOUNT_KILL_SWITCH_ACTION,
                    models.AuditLog.subject_type == ACCOUNT_KILL_SWITCH_SUBJECT_TYPE,
                    status.in_(_RECONCILABLE_STATUSES),
                )
                .group_by(models.AuditLog.tenant_id, models.AuditLog.subject_id)
                .order_by("oldest_pending_at")
                .limit(bounded_batch_size)
            )
        ).all()

    redis = aioredis.from_url(get_settings().redis_url)
    resolved_operation_ids: list[uuid.UUID] = []
    try:
        for tenant_id, subject_id, _oldest_pending_at in account_scopes:
            try:
                account_id = uuid.UUID(subject_id)
            except ValueError:
                logger.error(
                    "account kill switch audit has invalid account id",
                    extra={"tenant_id": tenant_id, "subject_id": subject_id},
                )
                continue
            try:
                result = await _reconcile_account_scope(
                    tenant_id=tenant_id,
                    account_id=account_id,
                    redis=redis,
                )
            except Exception:  # noqa: BLE001 - one account must not block the bounded sweep
                logger.exception(
                    "account kill switch reconciliation failed",
                    extra={"tenant_id": tenant_id, "account_id": str(account_id)},
                )
                continue
            resolved_operation_ids.extend(result.resolved_operation_ids)
    finally:
        await redis.aclose()
    return resolved_operation_ids
