import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select

from social_reply.application.handoff_notifications.projection import (
    render_current_handoff_card,
)
from social_reply.application.handoff_notifications.service import (
    refresh_handoff_notification_route,
)
from social_reply.connectors.errors import PermanentSendError, RetryableSendError
from social_reply.connectors.feishu.client import FeishuClient, FeishuClientError
from social_reply.connectors.registry import get_platform_sender
from social_reply.domain.platform_accounts import AccountPlatform, is_active_account_status
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.advisory_locks import (
    acquire_conversation_delivery_xact_lock,
)
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.queue.dispatch import dispatch_actor
from social_reply.shared.config import get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClaimedNotification:
    intent_id: uuid.UUID
    claim_token: uuid.UUID
    sending_revision: int
    attempt_count: int
    feishu_platform_account_id: uuid.UUID
    destination_chat_id: str
    provider_uuid: str
    provider_message_id: str | None
    card: dict[str, object]


def _retry_at(attempt_count: int) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=min(30 * 2**attempt_count, 3600))


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, (PermanentSendError, RetryableSendError, FeishuClientError)):
        return exc.code
    if isinstance(exc, httpx.HTTPStatusError):
        return f"FEISHU_HTTP_{exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "FEISHU_TIMEOUT"
    if isinstance(exc, httpx.TransportError):
        return "FEISHU_TRANSPORT_ERROR"
    return exc.__class__.__name__


def _work_card_state(work_status: str) -> str:
    if work_status in {"WAITING", "CLAIMED", "RESOLVED", "CANCELLED"}:
        return work_status
    raise ValueError("human_work_item_status_invalid")


async def _claim_notification(intent_id: uuid.UUID) -> ClaimedNotification | str:
    settings = get_settings()
    if not settings.feishu_enabled or not settings.feishu_handoff_notifications_enabled:
        return "SKIPPED_DISABLED"
    now = datetime.now(UTC)
    async with get_session_factory()() as session:
        identity = (
            await session.execute(
                select(
                    models.HandoffNotificationIntent.conversation_id,
                    models.HandoffNotificationIntent.human_work_item_id,
                ).where(models.HandoffNotificationIntent.id == intent_id)
            )
        ).one_or_none()
        if identity is None:
            return "SKIPPED_NOT_FOUND"
        await acquire_conversation_delivery_xact_lock(session, identity.conversation_id)
        conversation = (
            await session.execute(
                select(models.Conversation)
                .where(models.Conversation.id == identity.conversation_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        work = (
            await session.execute(
                select(models.HumanWorkItem)
                .where(models.HumanWorkItem.id == identity.human_work_item_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        state = await session.get(
            models.AutomationState,
            identity.conversation_id,
            with_for_update=True,
        )
        intent = (
            await session.execute(
                select(models.HandoffNotificationIntent)
                .where(models.HandoffNotificationIntent.id == intent_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if conversation is None or work is None or state is None or intent is None:
            await session.commit()
            return "SKIPPED_SCOPE_MISSING"
        if not (
            conversation.tenant_id == work.tenant_id == intent.tenant_id
            and work.conversation_id == conversation.id
            and intent.conversation_id == conversation.id
            and intent.human_work_item_id == work.id
        ):
            intent.status = "NEEDS_REVIEW"
            intent.last_error_code = "TENANT_SCOPE_MISMATCH"
            await session.commit()
            return "NEEDS_REVIEW"

        current_card_state = _work_card_state(work.status)
        if current_card_state != intent.desired_card_state:
            intent.desired_card_state = current_card_state
            intent.desired_revision += 1
            intent.action_nonce = uuid.uuid4()
            if intent.provider_message_id is not None:
                intent.status = "PENDING"
                intent.next_attempt_at = None
        if intent.provider_message_id is None and work.status in {"RESOLVED", "CANCELLED"}:
            intent.status = "CANCELLED"
            intent.next_attempt_at = None
            await session.commit()
            return "CANCELLED_BEFORE_CREATE"

        claimable = intent.status == "PENDING" or (
            intent.status == "FAILED"
            and intent.next_attempt_at is not None
            and intent.next_attempt_at <= now
        )
        if not claimable:
            await session.commit()
            return "SKIPPED_NOT_CLAIMABLE"

        if intent.provider_message_id is None:
            config = (
                await session.get(
                    models.TenantFeishuHandoffConfig,
                    intent.notification_config_id,
                )
                if intent.notification_config_id is not None
                else None
            )
            route_changed = (
                config is None
                or not config.enabled
                or config.tenant_id != intent.tenant_id
                or config.config_version != intent.config_version
                or config.feishu_platform_account_id != intent.feishu_platform_account_id
                or config.destination_chat_id != intent.destination_chat_id
            )
            if route_changed and not await refresh_handoff_notification_route(
                session,
                intent=intent,
            ):
                await session.commit()
                return "BLOCKED_CONFIG"

        if intent.feishu_platform_account_id is None or not intent.destination_chat_id:
            intent.status = "BLOCKED_CONFIG"
            intent.last_error_code = "FEISHU_HANDOFF_ROUTE_INVALID"
            await session.commit()
            return "BLOCKED_CONFIG"
        account = await session.get(models.PlatformAccount, intent.feishu_platform_account_id)
        if (
            account is None
            or account.tenant_id != intent.tenant_id
            or account.platform != AccountPlatform.FEISHU
            or not is_active_account_status(account.status)
        ):
            intent.status = "BLOCKED_CONFIG"
            intent.last_error_code = "FEISHU_HANDOFF_ACCOUNT_INVALID"
            await session.commit()
            return "BLOCKED_CONFIG"
        if str((account.config or {}).get("feishu_health_status") or "") != "READY":
            intent.status = "FAILED"
            intent.next_attempt_at = now + timedelta(seconds=60)
            intent.last_error_code = "FEISHU_ACCOUNT_NOT_READY"
            intent.last_error_message = None
            await session.commit()
            return "PAUSED_ACCOUNT_NOT_READY"

        claim_token = uuid.uuid4()
        attempt_count = intent.attempt_count + 1
        sending_revision = intent.desired_revision
        card = await render_current_handoff_card(
            session,
            intent=intent,
            conversation=conversation,
            work=work,
            state=state,
        )
        intent.status = "SENDING"
        intent.claim_token = claim_token
        intent.claim_expires_at = now + timedelta(
            seconds=settings.feishu_handoff_sender_lease_seconds
        )
        intent.sending_revision = sending_revision
        intent.attempt_count = attempt_count
        intent.next_attempt_at = None
        intent.last_error_code = None
        intent.last_error_message = None
        claimed = ClaimedNotification(
            intent_id=intent.id,
            claim_token=claim_token,
            sending_revision=sending_revision,
            attempt_count=attempt_count,
            feishu_platform_account_id=intent.feishu_platform_account_id,
            destination_chat_id=intent.destination_chat_id,
            provider_uuid=str(intent.provider_uuid),
            provider_message_id=intent.provider_message_id,
            card=card,
        )
        await session.commit()
        return claimed


async def _finalize_success(
    claimed: ClaimedNotification,
    *,
    provider_message_id: str | None,
) -> str:
    async with get_session_factory()() as session:
        intent = (
            await session.execute(
                select(models.HandoffNotificationIntent)
                .where(
                    models.HandoffNotificationIntent.id == claimed.intent_id,
                    models.HandoffNotificationIntent.status == "SENDING",
                    models.HandoffNotificationIntent.claim_token == claimed.claim_token,
                    models.HandoffNotificationIntent.sending_revision
                    == claimed.sending_revision,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if intent is None:
            await session.commit()
            return "STALE_FINALIZE"
        if provider_message_id is not None:
            if intent.provider_message_id not in {None, provider_message_id}:
                intent.status = "NEEDS_REVIEW"
                intent.last_error_code = "FEISHU_CARD_MESSAGE_ID_CONFLICT"
                intent.claim_token = None
                intent.claim_expires_at = None
                intent.sending_revision = None
                await session.commit()
                return "NEEDS_REVIEW"
            intent.provider_message_id = provider_message_id
        intent.delivered_revision = claimed.sending_revision
        intent.synced_at = datetime.now(UTC)
        intent.claim_token = None
        intent.claim_expires_at = None
        intent.sending_revision = None
        if intent.desired_revision > claimed.sending_revision:
            intent.status = "PENDING"
            intent.next_attempt_at = None
            result = "PENDING_NEWER_REVISION"
        else:
            intent.status = "SYNCED"
            result = "SYNCED"
        await session.commit()
        return result


async def _finalize_failure(
    claimed: ClaimedNotification,
    *,
    status: str,
    error_code: str,
) -> str:
    settings = get_settings()
    async with get_session_factory()() as session:
        intent = (
            await session.execute(
                select(models.HandoffNotificationIntent)
                .where(
                    models.HandoffNotificationIntent.id == claimed.intent_id,
                    models.HandoffNotificationIntent.status == "SENDING",
                    models.HandoffNotificationIntent.claim_token == claimed.claim_token,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if intent is None:
            await session.commit()
            return "STALE_FINALIZE"
        if status == "FAILED" and claimed.attempt_count >= settings.feishu_handoff_max_attempts:
            status = "NEEDS_REVIEW"
            error_code = "FEISHU_HANDOFF_RETRY_EXHAUSTED"
        intent.status = status
        intent.last_error_code = error_code
        intent.last_error_message = None
        intent.next_attempt_at = _retry_at(claimed.attempt_count) if status == "FAILED" else None
        intent.claim_token = None
        intent.claim_expires_at = None
        intent.sending_revision = None
        await session.commit()
        return status


async def deliver_handoff_notification(intent_id: str) -> str:
    try:
        parsed_id = uuid.UUID(intent_id)
    except ValueError:
        return "SKIPPED_INVALID_ID"
    claimed = await _claim_notification(parsed_id)
    if isinstance(claimed, str):
        return claimed
    try:
        sender = await get_platform_sender(claimed.feishu_platform_account_id)
        if not isinstance(sender, FeishuClient):
            raise PermanentSendError("FEISHU_HANDOFF_SENDER_INVALID")
        if claimed.provider_message_id is None:
            provider_message_id = await sender.create_interactive_card(
                chat_id=claimed.destination_chat_id,
                card=claimed.card,
                provider_uuid=claimed.provider_uuid,
            )
            return await _finalize_success(
                claimed,
                provider_message_id=provider_message_id,
            )
        await sender.update_interactive_card(
            message_id=claimed.provider_message_id,
            card=claimed.card,
        )
        return await _finalize_success(claimed, provider_message_id=None)
    except PermanentSendError as exc:
        return await _finalize_failure(
            claimed,
            status="NEEDS_REVIEW",
            error_code=exc.code,
        )
    except (httpx.ConnectError, httpx.ConnectTimeout, RetryableSendError) as exc:
        return await _finalize_failure(
            claimed,
            status="FAILED",
            error_code=_safe_error(exc),
        )
    except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as exc:
        if claimed.provider_message_id is None:
            return await _finalize_failure(
                claimed,
                status="NEEDS_REVIEW",
                error_code="AMBIGUOUS_CARD_CREATE",
            )
        return await _finalize_failure(
            claimed,
            status="FAILED",
            error_code=_safe_error(exc),
        )
    except FeishuClientError as exc:
        if claimed.provider_message_id is None:
            return await _finalize_failure(
                claimed,
                status="NEEDS_REVIEW",
                error_code="AMBIGUOUS_CARD_CREATE",
            )
        return await _finalize_failure(
            claimed,
            status="FAILED" if exc.retryable else "NEEDS_REVIEW",
            error_code=exc.code,
        )
    except Exception as exc:  # noqa: BLE001 - unknown create outcomes are ambiguous
        logger.exception("Feishu handoff notification failed intent_id=%s", claimed.intent_id)
        return await _finalize_failure(
            claimed,
            status="NEEDS_REVIEW",
            error_code=(
                "AMBIGUOUS_CARD_CREATE"
                if claimed.provider_message_id is None
                else _safe_error(exc)
            ),
        )


async def dispatch_handoff_notification(intent_id: uuid.UUID) -> None:
    from social_reply.application.handoff_notifications.actors import (
        deliver_handoff_notification_actor,
    )

    await dispatch_actor(
        deliver_handoff_notification_actor,
        str(intent_id),
        inline=lambda: deliver_handoff_notification(str(intent_id)),
    )
