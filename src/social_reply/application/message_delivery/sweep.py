import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import insert, or_, select, update

from social_reply.domain.platform_accounts import (
    LEGACY_ACTIVE_ACCOUNT_STATUSES,
    CapabilityKey,
)
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.shared.config import get_settings

logger = logging.getLogger(__name__)

# SENDING 滞留阈值：超过视为 worker 崩溃/丢失，转人工（不自动重发，防歧义重复）
_STALE_SENDING = timedelta(minutes=10)


async def sweep_outbox() -> list[uuid.UUID]:
    """补扫：滞留 SENDING 转 NEEDS_REVIEW（不自动重发，防重复）；
    PENDING / 退避到期 FAILED 重新入队。返回本轮入队的 outbox id。"""
    now = datetime.now(UTC)
    settings = get_settings()
    recoverable_routes = []
    if settings.chatwoot_enabled:
        recoverable_routes.append(("chatwoot_conversation", "CHATWOOT_DISABLED", None, None))
    if settings.x_legacy_dm_enabled:
        recoverable_routes.append(("x_dm", "X_LEGACY_DM_DISABLED", CapabilityKey.DM.value, "x"))
    if settings.xchat_enabled:
        recoverable_routes.append(
            ("x_chat_message", "XCHAT_DISABLED", CapabilityKey.X_CHAT.value, "x")
        )
    if getattr(settings, "facebook_messenger_enabled", True):
        recoverable_routes.extend(
            (
                ("meta_messenger_dm", "FACEBOOK_MESSENGER_DISABLED", "dm", "facebook"),
                ("meta_public_comment", "FACEBOOK_MESSENGER_DISABLED", "comments", "facebook"),
                ("meta_private_reply", "FACEBOOK_MESSENGER_DISABLED", "comments", "facebook"),
            )
        )
    if getattr(settings, "instagram_messaging_enabled", True):
        recoverable_routes.extend(
            (
                ("meta_instagram_dm", "INSTAGRAM_MESSAGING_DISABLED", "dm", "instagram"),
                ("meta_public_comment", "INSTAGRAM_MESSAGING_DISABLED", "comments", "instagram"),
                ("meta_private_reply", "INSTAGRAM_MESSAGING_DISABLED", "comments", "instagram"),
            )
        )
    if getattr(settings, "whatsapp_enabled", True):
        recoverable_routes.append(
            ("whatsapp_session_message", "WHATSAPP_DISABLED", "session_messages", "whatsapp")
        )
    async with get_session_factory()() as session:
        for destination_type, error_code, capability_key, platform in recoverable_routes:
            statement = update(models.OutboxMessage).where(
                models.OutboxMessage.status == "NEEDS_REVIEW",
                models.OutboxMessage.destination_type == destination_type,
                models.OutboxMessage.last_error_code == error_code,
            )
            if capability_key is not None or platform is not None:
                capable_accounts = select(models.PlatformAccount.id)
                if platform is not None:
                    capable_accounts = capable_accounts.where(
                        models.PlatformAccount.platform == platform
                    )
                if capability_key is not None:
                    capable_accounts = capable_accounts.where(
                        models.PlatformAccount.capability[capability_key].as_boolean().is_(True)
                    )
                statement = statement.where(
                    models.OutboxMessage.platform_account_id.in_(capable_accounts)
                )
            await session.execute(
                statement.values(
                    status="PENDING",
                    next_attempt_at=None,
                    locked_at=None,
                    locked_by=None,
                    last_error_code=None,
                    last_error_message=None,
                )
            )
        ready_meta_accounts = select(models.PlatformAccount.id).where(
            models.PlatformAccount.platform.in_(("facebook", "instagram")),
            models.PlatformAccount.status.in_(LEGACY_ACTIVE_ACCOUNT_STATUSES),
            models.PlatformAccount.config["meta_health_status"].astext == "READY",
        )
        await session.execute(
            update(models.OutboxMessage)
            .where(
                models.OutboxMessage.status == "NEEDS_REVIEW",
                models.OutboxMessage.last_error_code == "META_ACCOUNT_NOT_READY",
                models.OutboxMessage.destination_type.in_(
                    (
                        "meta_messenger_dm",
                        "meta_instagram_dm",
                        "meta_public_comment",
                        "meta_private_reply",
                    )
                ),
                models.OutboxMessage.platform_account_id.in_(ready_meta_accounts),
            )
            .values(
                status="PENDING",
                next_attempt_at=None,
                locked_at=None,
                locked_by=None,
                last_error_code=None,
                last_error_message=None,
            )
        )
        stale_rows = (
            await session.execute(
                update(models.OutboxMessage)
                .where(
                    models.OutboxMessage.status == "SENDING",
                    models.OutboxMessage.locked_at < now - _STALE_SENDING,
                )
                .values(status="NEEDS_REVIEW", last_error_code="STALE_SENDING")
                .returning(models.OutboxMessage.id, models.OutboxMessage.attempt_count)
            )
        ).all()
        # 与 deliver_outbox 的终态一致：每条转 NEEDS_REVIEW 的行补一条审计
        for sid, attempt_count in stale_rows:
            await session.execute(
                insert(models.DeliveryAttempt).values(
                    outbox_id=sid,
                    attempt_no=attempt_count,
                    outcome="NEEDS_REVIEW",
                    error_code="STALE_SENDING",
                    error_message="stale SENDING swept (worker lost)",
                )
            )
        rows = (
            (
                await session.execute(
                    select(models.OutboxMessage.id).where(
                        or_(
                            models.OutboxMessage.status == "PENDING",
                            (models.OutboxMessage.status == "FAILED")
                            & (models.OutboxMessage.next_attempt_at <= now),
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        enqueued = list(rows)
        await session.commit()

    # 延迟导入：避免模块加载时初始化 broker
    from social_reply.application.message_delivery.actors import deliver_outbox_message

    dispatched: list[uuid.UUID] = []
    for oid in enqueued:
        try:
            deliver_outbox_message.send(str(oid))
        except Exception:  # noqa: BLE001 - the durable row remains eligible for recovery
            logger.exception("outbox dispatch failed outbox_id=%s", oid)
        else:
            dispatched.append(oid)
    return dispatched
