import logging
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.connectors.chatwoot.client import get_chatwoot_client
from social_reply.connectors.registry import get_platform_sender
from social_reply.domain.platform_accounts import is_active_account_status
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 5
_DIRECT_CAPABILITY = {
    "telegram_dm": "dm",
    "meta_messenger_dm": "dm",
    "meta_instagram_dm": "dm",
    "meta_public_comment": "comments",
    "meta_private_reply": "comments",
    "whatsapp_session_message": "session_messages",
    "x_dm": "dm",
    "x_post_reply": "mentions",
}


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "transport timeout"
    if isinstance(exc, httpx.TransportError):
        return "transport error"
    if isinstance(exc, ValueError):
        return str(exc)[:500]
    return exc.__class__.__name__


async def _resolve_target(
    session: AsyncSession, conversation_id: uuid.UUID
) -> tuple[int, int] | None:
    row = (
        await session.execute(
            select(
                models.ConversationMapping.chatwoot_account_id,
                models.ConversationMapping.chatwoot_conversation_id,
            ).where(models.ConversationMapping.conversation_id == conversation_id)
        )
    ).first()
    return (row.chatwoot_account_id, row.chatwoot_conversation_id) if row else None


async def _record_outcome(
    session: AsyncSession,
    outbox_id: uuid.UUID,
    status: str,
    *,
    attempt_no: int,
    error_code: str | None = None,
    error_message: str | None = None,
    chatwoot_message_id: int | None = None,
    platform_message_id: str | None = None,
    next_attempt_at: datetime | None = None,
    require_sending: bool = True,
) -> str:
    values: dict = {
        "status": status,
        "last_error_code": error_code,
        "last_error_message": error_message,
        "next_attempt_at": next_attempt_at,
    }
    if chatwoot_message_id is not None:
        values["chatwoot_message_id"] = chatwoot_message_id
    if platform_message_id is not None:
        values["platform_message_id"] = platform_message_id
    if status == "SENT":
        values["sent_at"] = datetime.now(UTC)
    statement = update(models.OutboxMessage).where(models.OutboxMessage.id == outbox_id)
    if require_sending:
        statement = statement.where(models.OutboxMessage.status == "SENDING")
    result = await session.execute(statement.values(**values))
    actual_status = status
    actual_error_code = error_code
    actual_error_message = error_message
    if result.rowcount == 0:
        current_status = await session.scalar(
            select(models.OutboxMessage.status).where(models.OutboxMessage.id == outbox_id)
        )
        logger.warning(
            "outbox %s outcome %s skipped because current status is %s",
            outbox_id,
            status,
            current_status,
        )
        actual_status = "STALE_FINALIZE"
        actual_error_code = "STALE_FINALIZE"
        actual_error_message = f"requested={status}; current={current_status}"
    await session.execute(
        insert(models.DeliveryAttempt).values(
            outbox_id=outbox_id,
            attempt_no=attempt_no,
            outcome=actual_status,
            error_code=actual_error_code,
            error_message=actual_error_message,
            chatwoot_message_id=chatwoot_message_id,
        )
    )
    await session.commit()
    return actual_status


async def _finalize(outbox_id: uuid.UUID, status: str, **kwargs) -> str:
    async with get_session_factory()() as session:
        return await _record_outcome(session, outbox_id, status, **kwargs)


async def _stop_before_send(
    session: AsyncSession,
    outbox_id: uuid.UUID,
    status: str,
    error_code: str,
    attempt_no: int,
) -> str:
    return await _record_outcome(
        session,
        outbox_id,
        status,
        attempt_no=attempt_no,
        error_code=error_code,
    )


async def _validate_direct_send(
    session: AsyncSession,
    row: models.OutboxMessage,
    payload: dict,
    attempt_no: int,
) -> tuple[str | None, models.PlatformAccount | None]:
    account = (
        await session.execute(
            select(models.PlatformAccount).where(
                models.PlatformAccount.id == row.platform_account_id
            )
        )
    ).scalar_one()
    conversation = (
        await session.execute(
            select(models.Conversation).where(models.Conversation.id == row.conversation_id)
        )
    ).scalar_one()
    if (
        account.tenant_id != row.tenant_id
        or conversation.tenant_id != row.tenant_id
        or conversation.platform_account_id != row.platform_account_id
    ):
        return await _stop_before_send(
            session, row.id, "NEEDS_REVIEW", "TENANT_SCOPE_MISMATCH", attempt_no
        ), None
    if not is_active_account_status(account.status):
        return await _stop_before_send(
            session, row.id, "NEEDS_REVIEW", "ACCOUNT_NOT_ACTIVE", attempt_no
        ), None
    capability = dict(account.capability or {})
    required = _DIRECT_CAPABILITY.get(row.destination_type)
    if required is None or not capability.get(required, False):
        return await _stop_before_send(
            session, row.id, "NEEDS_REVIEW", "CAPABILITY_NOT_ALLOWED", attempt_no
        ), None
    if row.valid_until is not None and row.valid_until <= datetime.now(UTC):
        return await _stop_before_send(
            session, row.id, "NEEDS_REVIEW", "DELIVERY_WINDOW_EXPIRED", attempt_no
        ), None
    if len(str(payload.get("text", ""))) > int(capability.get("max_text_length", 4096)):
        return await _stop_before_send(
            session, row.id, "NEEDS_REVIEW", "CAPABILITY_TEXT_TOO_LONG", attempt_no
        ), None
    return None, account


async def deliver_outbox(outbox_id: str) -> str:
    """Claim, validate, send without a DB transaction, then durably record the outcome."""
    oid = uuid.UUID(outbox_id)
    async with get_session_factory()() as session:
        claimed = (
            await session.execute(
                update(models.OutboxMessage)
                .where(
                    models.OutboxMessage.id == oid,
                    models.OutboxMessage.status.in_(["PENDING", "FAILED"]),
                )
                .values(status="SENDING", locked_at=datetime.now(UTC), locked_by="deliver")
                .returning(models.OutboxMessage.id)
            )
        ).first()
        if claimed is None:
            await session.commit()
            return "SKIPPED_NOT_CLAIMABLE"
        row = (
            await session.execute(
                select(models.OutboxMessage).where(models.OutboxMessage.id == oid)
            )
        ).scalar_one()
        payload = dict(row.payload)
        attempt_no = row.attempt_count + 1
        is_direct = row.destination_type != "chatwoot_conversation"
        is_public = row.message_type != "private_note"

        if is_direct and not is_public:
            return await _stop_before_send(
                session, oid, "CANCELLED", "DIRECT_DRAFT_BLOCKED", attempt_no
            )
        state = (
            await session.execute(
                select(models.AutomationState.state).where(
                    models.AutomationState.conversation_id == row.conversation_id
                )
            )
        ).scalar_one_or_none()
        admin_approved_draft = payload.get("approval") == "admin" and state == "BOT_DRAFT_ONLY"
        if is_public and state != "BOT_ACTIVE" and not admin_approved_draft:
            return await _stop_before_send(
                session, oid, "CANCELLED", "TAKEOVER_AT_SEND", attempt_no
            )
        if is_direct:
            stopped, _account = await _validate_direct_send(session, row, payload, attempt_no)
            if stopped is not None:
                return stopped
        target = None if is_direct else await _resolve_target(session, row.conversation_id)
        await session.execute(
            update(models.OutboxMessage)
            .where(models.OutboxMessage.id == oid)
            .values(attempt_count=attempt_no)
        )
        await session.commit()

    if not is_direct and target is None:
        return await _finalize(
            oid,
            "NEEDS_REVIEW",
            attempt_no=attempt_no,
            error_code="NO_MAPPING",
            error_message="no chatwoot mapping",
        )

    async def fail_retryable(exc: Exception) -> str:
        if attempt_no >= _MAX_ATTEMPTS:
            return await _finalize(
                oid,
                "NEEDS_REVIEW",
                attempt_no=attempt_no,
                error_code="SEND_ERROR",
                error_message=_safe_error(exc),
            )
        next_at = datetime.now(UTC) + timedelta(seconds=min(30 * 2**attempt_no, 3600))
        return await _finalize(
            oid,
            "FAILED",
            attempt_no=attempt_no,
            error_code="SEND_ERROR",
            error_message=_safe_error(exc),
            next_attempt_at=next_at,
        )

    async def ambiguous(exc: Exception) -> str:
        return await _finalize(
            oid,
            "NEEDS_REVIEW",
            attempt_no=attempt_no,
            error_code="AMBIGUOUS_SEND",
            error_message=_safe_error(exc),
        )

    direct_target: dict | None = None
    if is_direct:
        try:
            direct_target = dict(payload.get("target") or {})
            if not direct_target and row.destination_type in {"telegram_chat", "telegram_dm"}:
                direct_target = {"chat_id": int(row.destination_id.rsplit(":", 1)[-1])}
            if not direct_target:
                raise ValueError(f"direct_reply_target_missing:{row.destination_type}")
        except (TypeError, ValueError) as exc:
            return await fail_retryable(exc)

    sender = None
    if is_direct:
        try:
            sender = await get_platform_sender(row.platform_account_id)
        except Exception as exc:  # noqa: BLE001 - sender resolution happens before dispatch
            return await fail_retryable(exc)

    try:
        if is_direct:
            assert direct_target is not None and sender is not None
            platform_message_id = await sender.send_text(target=direct_target, text=payload["text"])
            chatwoot_message_id = None
        else:
            assert target is not None
            account_id, chatwoot_conv_id = target
            chatwoot_message_id = await get_chatwoot_client().create_message(
                account_id=account_id,
                conversation_id=chatwoot_conv_id,
                content=payload["text"],
                private=(row.message_type == "private_note"),
            )
            platform_message_id = None
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        return await fail_retryable(exc)
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        return await ambiguous(exc)
    except httpx.HTTPStatusError as exc:
        return (
            await ambiguous(exc) if exc.response.status_code >= 500 else await fail_retryable(exc)
        )
    except Exception as exc:  # noqa: BLE001 - unknown post-dispatch failures are ambiguous
        return await ambiguous(exc)

    return await _finalize(
        oid,
        "SENT",
        attempt_no=attempt_no,
        chatwoot_message_id=chatwoot_message_id,
        platform_message_id=platform_message_id,
    )
