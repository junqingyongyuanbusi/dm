import logging
import re
import uuid
from dataclasses import dataclass

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.application.account_management.access import lock_user_authority
from social_reply.application.account_management.auth import Principal, principal_from_session_row
from social_reply.application.message_delivery.outbox import (
    _effective_origin_kind,
    materialize_sent_outbox,
)
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.advisory_locks import (
    acquire_conversation_delivery_xact_lock,
)
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.queue.dispatch import dispatch_actor

logger = logging.getLogger(__name__)

_MAX_ACTOR_LENGTH = 255
_MAX_TENANT_ID_LENGTH = 255
_MAX_REVIEW_REASON_LENGTH = 500
_MAX_PROVIDER_MESSAGE_ID_LENGTH = 255
_EMAIL_PROVIDER_MESSAGE_ID_PATTERN = re.compile(
    r"^<[A-Za-z0-9][A-Za-z0-9._+-]{0,126}@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+>$"
)
_HTML_UNSAFE_PROVIDER_MESSAGE_ID_CHARACTERS = frozenset("<>&\"'")

VERIFICATION_SOURCES = frozenset(
    {
        "ADMIN_OPERATOR_ATTESTED",
        "CUSTOMER_CONFIRMATION",
        "PROVIDER_API",
        "PROVIDER_DASHBOARD",
        "SUPERVISOR_OVERRIDE",
    }
)
DELIVERY_REVIEW_RESOLUTIONS = frozenset(
    {
        "CONFIRMED_NOT_SENT_RETRY",
        "CONFIRMED_SENT",
        "CANCEL",
    }
)

_FAILED_RETRY_RESOLUTION = "CONFIRMED_FAILURE_RETRY"
_RESOLUTION_ACTIONS = {
    _FAILED_RETRY_RESOLUTION: "RETRY_CONFIRMED_FAILURE",
    "CONFIRMED_NOT_SENT_RETRY": "RETRY_VERIFIED_NOT_SENT",
    "CONFIRMED_SENT": "CONFIRM_DELIVERY_SENT",
    "CANCEL": "CANCEL_DELIVERY_AFTER_REVIEW",
}
_RESOLUTION_STATUSES = {
    _FAILED_RETRY_RESOLUTION: "PENDING",
    "CONFIRMED_NOT_SENT_RETRY": "PENDING",
    "CONFIRMED_SENT": "SENT",
    "CANCEL": "CANCELLED",
}


class DeliveryRecoveryError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class DeliveryRecoveryNotFound(DeliveryRecoveryError):
    pass


class DeliveryRecoveryConflict(DeliveryRecoveryError):
    pass


class DeliveryRecoveryValidationError(DeliveryRecoveryError):
    pass


@dataclass(frozen=True)
class DeliveryRecoveryResult:
    outbox_id: uuid.UUID
    status: str
    resolution: str
    idempotent: bool
    dispatched: bool


@dataclass(frozen=True)
class _LockedDeliveryContext:
    outbox: models.OutboxMessage
    conversation: models.Conversation
    account: models.PlatformAccount


def _normalize_required_tenant_id(required_tenant_id: str) -> str:
    normalized_tenant_id = required_tenant_id.strip()
    if not normalized_tenant_id or len(normalized_tenant_id) > _MAX_TENANT_ID_LENGTH:
        raise DeliveryRecoveryValidationError("delivery_tenant_invalid")
    return normalized_tenant_id


def _normalize_actor(actor: str) -> str:
    normalized_actor = actor.strip()
    if not normalized_actor or len(normalized_actor) > _MAX_ACTOR_LENGTH:
        raise DeliveryRecoveryValidationError("delivery_actor_invalid")
    return normalized_actor


def _normalize_review_reason(review_reason: str) -> str:
    normalized_reason = review_reason.strip()
    if not normalized_reason:
        raise DeliveryRecoveryValidationError("delivery_review_reason_required")
    if len(normalized_reason) > _MAX_REVIEW_REASON_LENGTH:
        raise DeliveryRecoveryValidationError("delivery_review_reason_too_long")
    return normalized_reason


def _normalize_verification_source(verification_source: str) -> str:
    normalized_source = verification_source.strip().upper()
    if normalized_source not in VERIFICATION_SOURCES:
        raise DeliveryRecoveryValidationError("delivery_verification_source_invalid")
    return normalized_source


def _normalize_expected_attempt_count(expected_attempt_count: int) -> int:
    if (
        isinstance(expected_attempt_count, bool)
        or not isinstance(expected_attempt_count, int)
        or expected_attempt_count < 0
    ):
        raise DeliveryRecoveryValidationError("delivery_expected_attempt_count_invalid")
    return expected_attempt_count


def _require_expected_status(expected_status: str, required_status: str) -> str:
    normalized_status = expected_status.strip().upper()
    if normalized_status != required_status:
        raise DeliveryRecoveryConflict("delivery_status_conflict")
    return normalized_status


def _normalize_resolution(resolution: str) -> str:
    normalized_resolution = resolution.strip().upper()
    if normalized_resolution not in DELIVERY_REVIEW_RESOLUTIONS:
        raise DeliveryRecoveryValidationError("delivery_resolution_invalid")
    return normalized_resolution


def _normalize_provider_message_id(
    provider_message_id: str | None,
    *,
    platform: str,
    resolution: str,
) -> str | None:
    raw_message_id = provider_message_id or ""
    normalized_message_id = raw_message_id.strip()
    if resolution == "CONFIRMED_SENT":
        if not normalized_message_id:
            raise DeliveryRecoveryValidationError("delivery_provider_message_id_required")
        is_email_message_id = (
            platform == "email"
            and _EMAIL_PROVIDER_MESSAGE_ID_PATTERN.fullmatch(normalized_message_id) is not None
        )
        is_safe_opaque_text = all(
            character.isprintable()
            and not character.isspace()
            and character not in _HTML_UNSAFE_PROVIDER_MESSAGE_ID_CHARACTERS
            for character in normalized_message_id
        )
        contains_control_character = any(
            not character.isprintable() for character in raw_message_id
        )
        if (
            contains_control_character
            or len(normalized_message_id) > _MAX_PROVIDER_MESSAGE_ID_LENGTH
            or not (is_email_message_id or is_safe_opaque_text)
        ):
            raise DeliveryRecoveryValidationError("delivery_provider_message_id_invalid")
        return normalized_message_id
    if normalized_message_id:
        raise DeliveryRecoveryValidationError("delivery_provider_message_id_not_allowed")
    return None


async def _load_locked_delivery_context(
    session: AsyncSession,
    *,
    outbox_id: uuid.UUID,
    required_tenant_id: str,
) -> _LockedDeliveryContext:
    conversation_id = await session.scalar(
        select(models.OutboxMessage.conversation_id).where(
            models.OutboxMessage.id == outbox_id,
            models.OutboxMessage.tenant_id == required_tenant_id,
        )
    )
    if conversation_id is None:
        raise DeliveryRecoveryNotFound("outbox_not_found")

    await acquire_conversation_delivery_xact_lock(session, conversation_id)
    row = (
        await session.execute(
            select(
                models.OutboxMessage,
                models.Conversation,
                models.PlatformAccount,
            )
            .join(
                models.Conversation,
                models.Conversation.id == models.OutboxMessage.conversation_id,
            )
            .join(
                models.PlatformAccount,
                models.PlatformAccount.id == models.OutboxMessage.platform_account_id,
            )
            .where(
                models.OutboxMessage.id == outbox_id,
                models.OutboxMessage.tenant_id == required_tenant_id,
                models.Conversation.id == conversation_id,
                models.Conversation.tenant_id == required_tenant_id,
                models.Conversation.platform_account_id == models.OutboxMessage.platform_account_id,
                models.PlatformAccount.tenant_id == required_tenant_id,
            )
            .with_for_update()
        )
    ).one_or_none()
    if row is None:
        raise DeliveryRecoveryNotFound("outbox_not_found")
    outbox, conversation, account = row
    return _LockedDeliveryContext(
        outbox=outbox,
        conversation=conversation,
        account=account,
    )


def _operation_detail(
    *,
    expected_status: str,
    expected_attempt_count: int,
    review_reason: str,
    verification_source: str,
    resolution: str,
    provider_message_id: str | None,
    previous_error_code: str | None,
    final_status: str,
) -> dict[str, object]:
    return {
        "expected_status": expected_status,
        "expected_attempt_count": expected_attempt_count,
        "review_reason": review_reason,
        "verification_source": verification_source,
        "resolution": resolution,
        "provider_message_id": provider_message_id,
        "previous_error_code": previous_error_code,
        "final_status": final_status,
    }


def _operation_fingerprint(detail: dict[str, object]) -> tuple[object, ...]:
    return (
        detail.get("expected_status"),
        detail.get("expected_attempt_count"),
        detail.get("review_reason"),
        detail.get("verification_source"),
        detail.get("resolution"),
        detail.get("provider_message_id"),
    )


async def _resolve_replay(
    session: AsyncSession,
    *,
    context: _LockedDeliveryContext,
    requested_detail: dict[str, object],
) -> DeliveryRecoveryResult | None:
    audit_rows = list(
        await session.scalars(
            select(models.AuditLog)
            .where(
                models.AuditLog.tenant_id == context.outbox.tenant_id,
                models.AuditLog.category == "delivery_recovery",
                models.AuditLog.subject_type == "outbox",
                models.AuditLog.subject_id == str(context.outbox.id),
            )
            .order_by(models.AuditLog.created_at)
        )
    )
    requested_expected_fence = (
        requested_detail["expected_status"],
        requested_detail["expected_attempt_count"],
    )
    matching_fence_rows = [
        audit
        for audit in audit_rows
        if (
            dict(audit.detail or {}).get("expected_status"),
            dict(audit.detail or {}).get("expected_attempt_count"),
        )
        == requested_expected_fence
    ]
    if not matching_fence_rows:
        return None
    requested_fingerprint = _operation_fingerprint(requested_detail)
    for audit in matching_fence_rows:
        if _operation_fingerprint(dict(audit.detail or {})) == requested_fingerprint:
            return DeliveryRecoveryResult(
                outbox_id=context.outbox.id,
                status=context.outbox.status,
                resolution=str(requested_detail["resolution"]),
                idempotent=True,
                dispatched=False,
            )
    raise DeliveryRecoveryConflict("delivery_resolution_conflict")


def _require_outbox_fence(
    *,
    context: _LockedDeliveryContext,
    expected_status: str,
    expected_attempt_count: int,
) -> None:
    if context.outbox.status != expected_status:
        raise DeliveryRecoveryConflict("delivery_status_conflict")
    if context.outbox.attempt_count != expected_attempt_count:
        raise DeliveryRecoveryConflict("delivery_attempt_conflict")


async def _record_manual_resolution(
    session: AsyncSession,
    *,
    context: _LockedDeliveryContext,
    actor: str,
    action: str,
    detail: dict[str, object],
) -> None:
    await session.execute(
        insert(models.AuditLog).values(
            tenant_id=context.outbox.tenant_id,
            category="delivery_recovery",
            actor=actor,
            action=action,
            subject_type="outbox",
            subject_id=str(context.outbox.id),
            detail=detail,
        )
    )
    await session.execute(
        insert(models.DeliveryAttempt).values(
            outbox_id=context.outbox.id,
            attempt_no=context.outbox.attempt_count,
            outcome=action,
            error_code=detail.get("previous_error_code"),
            error_message=str(detail["review_reason"]),
        )
    )


async def _dispatch_pending_outbox(outbox_id: uuid.UUID) -> bool:
    from social_reply.application.message_delivery.actors import deliver_outbox_message

    try:
        await dispatch_actor(deliver_outbox_message, str(outbox_id))
    except Exception:  # noqa: BLE001 - committed PENDING remains scheduler-recoverable
        logger.exception("delivery recovery dispatch failed outbox_id=%s", outbox_id)
        return False
    return True


async def _require_human_retry_authority(
    session: AsyncSession, context: _LockedDeliveryContext
) -> None:
    outbox = context.outbox
    origin = _effective_origin_kind(outbox, dict(outbox.payload or {}))
    if outbox.actor_kind != "ADMIN_HUMAN" and origin not in {"MANUAL_REPLY", "DRAFT_APPROVAL"}:
        return
    if outbox.initiator_session_id is None:
        raise DeliveryRecoveryConflict("human_outbox_requires_reapproval")
    # This read does not acquire a late staff/Session lock while holding delivery rows.
    # Recovery never changes the original authority; delivery rechecks it before I/O.
    principal = await principal_from_session_row(session, outbox.initiator_session_id)
    if (
        principal is None
        or principal.must_change_password
        or principal.user_id != outbox.initiator_user_id
        or not principal.can_access_account(context.account)
    ):
        raise DeliveryRecoveryConflict("human_outbox_authority_revoked")
    if origin == "DRAFT_APPROVAL" and principal.is_workspace_admin:
        return
    if origin == "MANUAL_REPLY":
        work = await session.scalar(
            select(models.HumanWorkItem).where(
                models.HumanWorkItem.conversation_id == context.conversation.id,
                models.HumanWorkItem.tenant_id == context.account.tenant_id,
                models.HumanWorkItem.status == "CLAIMED",
            )
        )
        if (
            work is not None
            and work.version == outbox.human_work_item_version
            and work.assigned_user_id == principal.user_id
            and work.assigned_actor == principal.actor
        ):
            return
    raise DeliveryRecoveryConflict("human_outbox_requires_reapproval")


async def _apply_resolution(
    *,
    outbox_id: uuid.UUID,
    required_tenant_id: str,
    actor: str,
    expected_status: str,
    expected_attempt_count: int,
    review_reason: str,
    verification_source: str,
    resolution: str,
    provider_message_id: str | None,
    principal: Principal | None,
) -> DeliveryRecoveryResult:
    final_status = _RESOLUTION_STATUSES[resolution]
    action = _RESOLUTION_ACTIONS[resolution]
    should_dispatch = final_status == "PENDING"

    async with get_session_factory()() as session:
        if principal is None:
            raise DeliveryRecoveryConflict("delivery_admin_session_required")
        if principal.user_id is not None:
            await lock_user_authority(session, principal.user_id)
        current = await principal_from_session_row(session, principal.session_id, for_update=True)
        if (
            current is None
            or current.must_change_password
            or current.user_id != principal.user_id
            or not current.is_workspace_admin
            or required_tenant_id not in current.allowed_tenants
        ):
            raise DeliveryRecoveryConflict("delivery_admin_authority_revoked")
        actor = current.actor
        context = await _load_locked_delivery_context(
            session,
            outbox_id=outbox_id,
            required_tenant_id=required_tenant_id,
        )
        normalized_provider_message_id = _normalize_provider_message_id(
            provider_message_id,
            platform=context.account.platform,
            resolution=resolution,
        )
        detail = _operation_detail(
            expected_status=expected_status,
            expected_attempt_count=expected_attempt_count,
            review_reason=review_reason,
            verification_source=verification_source,
            resolution=resolution,
            provider_message_id=normalized_provider_message_id,
            previous_error_code=context.outbox.last_error_code,
            final_status=final_status,
        )
        detail["initiator_user_id"] = str(current.user_id) if current.user_id else None
        detail["initiator_session_id"] = str(current.session_id)
        replay = await _resolve_replay(
            session,
            context=context,
            requested_detail=detail,
        )
        if replay is not None:
            return replay

        _require_outbox_fence(
            context=context,
            expected_status=expected_status,
            expected_attempt_count=expected_attempt_count,
        )
        if should_dispatch:
            await _require_human_retry_authority(session, context)
        context.outbox.status = final_status
        context.outbox.next_attempt_at = None
        context.outbox.locked_at = None
        context.outbox.locked_by = None
        if resolution == "CONFIRMED_SENT":
            context.outbox.platform_message_id = normalized_provider_message_id
            context.outbox.last_error_code = None
            context.outbox.last_error_message = None
            context.outbox.sent_at = await session.scalar(select(func.clock_timestamp()))
            await session.flush([context.outbox])
            await materialize_sent_outbox(
                session,
                outbox_id=context.outbox.id,
            )
        await _record_manual_resolution(
            session,
            context=context,
            actor=actor,
            action=action,
            detail=detail,
        )
        await session.commit()

    dispatched = await _dispatch_pending_outbox(outbox_id) if should_dispatch else False
    return DeliveryRecoveryResult(
        outbox_id=outbox_id,
        status=final_status,
        resolution=resolution,
        idempotent=False,
        dispatched=dispatched,
    )


async def retry_failed_outbox(
    *,
    outbox_id: uuid.UUID,
    required_tenant_id: str,
    actor: str,
    expected_status: str,
    expected_attempt_count: int,
    review_reason: str,
    verification_source: str,
    principal: Principal | None = None,
) -> DeliveryRecoveryResult:
    normalized_tenant_id = _normalize_required_tenant_id(required_tenant_id)
    normalized_actor = _normalize_actor(actor)
    normalized_expected_status = _require_expected_status(expected_status, "FAILED")
    normalized_attempt_count = _normalize_expected_attempt_count(expected_attempt_count)
    normalized_reason = _normalize_review_reason(review_reason)
    normalized_source = _normalize_verification_source(verification_source)
    return await _apply_resolution(
        outbox_id=outbox_id,
        required_tenant_id=normalized_tenant_id,
        actor=normalized_actor,
        expected_status=normalized_expected_status,
        expected_attempt_count=normalized_attempt_count,
        review_reason=normalized_reason,
        verification_source=normalized_source,
        resolution=_FAILED_RETRY_RESOLUTION,
        provider_message_id=None,
        principal=principal,
    )


async def resolve_needs_review_outbox(
    *,
    outbox_id: uuid.UUID,
    required_tenant_id: str,
    actor: str,
    expected_status: str,
    expected_attempt_count: int,
    review_reason: str,
    verification_source: str,
    resolution: str,
    provider_message_id: str | None,
    principal: Principal | None = None,
) -> DeliveryRecoveryResult:
    normalized_tenant_id = _normalize_required_tenant_id(required_tenant_id)
    normalized_actor = _normalize_actor(actor)
    normalized_expected_status = _require_expected_status(
        expected_status,
        "NEEDS_REVIEW",
    )
    normalized_attempt_count = _normalize_expected_attempt_count(expected_attempt_count)
    normalized_reason = _normalize_review_reason(review_reason)
    normalized_source = _normalize_verification_source(verification_source)
    normalized_resolution = _normalize_resolution(resolution)
    return await _apply_resolution(
        outbox_id=outbox_id,
        required_tenant_id=normalized_tenant_id,
        actor=normalized_actor,
        expected_status=normalized_expected_status,
        expected_attempt_count=normalized_attempt_count,
        review_reason=normalized_reason,
        verification_source=normalized_source,
        resolution=normalized_resolution,
        provider_message_id=provider_message_id,
        principal=principal,
    )
