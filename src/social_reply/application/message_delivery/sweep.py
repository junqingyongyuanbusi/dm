import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import insert, or_, select, update

from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory

# SENDING 滞留阈值：超过视为 worker 崩溃/丢失，转人工（不自动重发，防歧义重复）
_STALE_SENDING = timedelta(minutes=10)


async def sweep_outbox() -> list[uuid.UUID]:
    """补扫：滞留 SENDING 转 NEEDS_REVIEW（不自动重发，防重复）；
    PENDING / 退避到期 FAILED 重新入队。返回本轮入队的 outbox id。"""
    now = datetime.now(UTC)
    async with get_session_factory()() as session:
        stale_rows = (await session.execute(
            update(models.OutboxMessage)
            .where(models.OutboxMessage.status == "SENDING",
                   models.OutboxMessage.locked_at < now - _STALE_SENDING)
            .values(status="NEEDS_REVIEW", last_error_code="STALE_SENDING")
            .returning(models.OutboxMessage.id, models.OutboxMessage.attempt_count))).all()
        # 与 deliver_outbox 的终态一致：每条转 NEEDS_REVIEW 的行补一条审计
        for sid, attempt_count in stale_rows:
            await session.execute(insert(models.DeliveryAttempt).values(
                outbox_id=sid, attempt_no=attempt_count + 1, outcome="NEEDS_REVIEW",
                error_code="STALE_SENDING",
                error_message="stale SENDING swept (worker lost)"))
        rows = (await session.execute(
            select(models.OutboxMessage.id)
            .where(or_(
                models.OutboxMessage.status == "PENDING",
                (models.OutboxMessage.status == "FAILED")
                & (models.OutboxMessage.next_attempt_at <= now)))
        )).scalars().all()
        enqueued = list(rows)
        await session.commit()

    # 延迟导入：避免模块加载时初始化 broker
    from social_reply.application.message_delivery.actors import deliver_outbox_message

    for oid in enqueued:
        deliver_outbox_message.send(str(oid))
    return enqueued
