import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import and_, or_, select

from social_reply.application.handoff_notifications.sender import (
    dispatch_handoff_notification,
)
from social_reply.application.handoff_notifications.service import (
    refresh_handoff_notification_route,
)
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.shared.config import get_settings

logger = logging.getLogger(__name__)


async def sweep_handoff_notifications() -> list[uuid.UUID]:
    settings = get_settings()
    if not settings.feishu_enabled or not settings.feishu_handoff_notifications_enabled:
        return []
    now = datetime.now(UTC)
    async with get_session_factory()() as session:
        stale = (
            await session.execute(
                select(models.HandoffNotificationIntent)
                .where(
                    models.HandoffNotificationIntent.status == "SENDING",
                    models.HandoffNotificationIntent.claim_expires_at <= now,
                )
                .with_for_update(skip_locked=True)
                .limit(100)
            )
        ).scalars()
        for intent in stale:
            if intent.provider_message_id is None:
                intent.status = "NEEDS_REVIEW"
                intent.last_error_code = "AMBIGUOUS_CARD_CREATE"
                intent.next_attempt_at = None
            else:
                intent.status = "FAILED"
                intent.last_error_code = "STALE_CARD_UPDATE"
                intent.next_attempt_at = now
            intent.claim_token = None
            intent.claim_expires_at = None
            intent.sending_revision = None

        blocked = (
            await session.execute(
                select(models.HandoffNotificationIntent)
                .where(
                    models.HandoffNotificationIntent.status == "BLOCKED_CONFIG",
                    models.HandoffNotificationIntent.provider_message_id.is_(None),
                )
                .order_by(models.HandoffNotificationIntent.created_at)
                .with_for_update(skip_locked=True)
                .limit(100)
            )
        ).scalars()
        for intent in blocked:
            work_status = await session.scalar(
                select(models.HumanWorkItem.status).where(
                    models.HumanWorkItem.id == intent.human_work_item_id,
                    models.HumanWorkItem.tenant_id == intent.tenant_id,
                )
            )
            if work_status in {"RESOLVED", "CANCELLED"}:
                intent.status = "CANCELLED"
                intent.last_error_code = None
                continue
            await refresh_handoff_notification_route(session, intent=intent)

        due_ids = list(
            (
                await session.execute(
                    select(models.HandoffNotificationIntent.id)
                    .where(
                        or_(
                            models.HandoffNotificationIntent.status == "PENDING",
                            and_(
                                models.HandoffNotificationIntent.status == "FAILED",
                                models.HandoffNotificationIntent.next_attempt_at <= now,
                            ),
                        )
                    )
                    .order_by(models.HandoffNotificationIntent.created_at)
                    .limit(200)
                )
            ).scalars()
        )
        await session.commit()

    dispatched: list[uuid.UUID] = []
    for intent_id in due_ids:
        try:
            await dispatch_handoff_notification(intent_id)
        except Exception:  # noqa: BLE001 - durable intent remains eligible for recovery
            logger.exception(
                "Feishu handoff notification dispatch failed intent_id=%s",
                intent_id,
            )
        else:
            dispatched.append(intent_id)
    return dispatched
