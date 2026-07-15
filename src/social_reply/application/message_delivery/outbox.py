import uuid
from datetime import UTC, datetime

import httpx
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.connectors.chatwoot.client import get_chatwoot_client
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory

_MAX_ATTEMPTS = 5


async def _resolve_target(
    session: AsyncSession, conversation_id: uuid.UUID
) -> tuple[int, int] | None:
    row = (await session.execute(
        select(models.ConversationMapping.chatwoot_account_id,
               models.ConversationMapping.chatwoot_conversation_id)
        .where(models.ConversationMapping.conversation_id == conversation_id)
    )).first()
    return (row.chatwoot_account_id, row.chatwoot_conversation_id) if row else None


async def _finalize(
    outbox_id: uuid.UUID, status: str, *, attempt_no: int,
    error_code: str | None = None, error_message: str | None = None,
    chatwoot_message_id: int | None = None, next_attempt_at: datetime | None = None,
) -> str:
    async with get_session_factory()() as session:
        values: dict = {"status": status, "last_error_code": error_code,
                        "last_error_message": error_message}
        if chatwoot_message_id is not None:
            values["chatwoot_message_id"] = chatwoot_message_id
            values["sent_at"] = datetime.now(UTC)
        if next_attempt_at is not None:
            values["next_attempt_at"] = next_attempt_at
        await session.execute(
            update(models.OutboxMessage)
            .where(models.OutboxMessage.id == outbox_id).values(**values))
        await session.execute(insert(models.DeliveryAttempt).values(
            outbox_id=outbox_id, attempt_no=attempt_no, outcome=status,
            error_code=error_code, error_message=error_message,
            chatwoot_message_id=chatwoot_message_id))
        await session.commit()
    return status


async def deliver_outbox(outbox_id: str) -> str:
    """认领 → defense 2 发送前复检 → 发送 → 落库。返回终态字符串。"""
    oid = uuid.UUID(outbox_id)

    # 1) 原子认领：仅 PENDING/FAILED 可认领 → SENDING（防重复认领、跳过已取消/已发送）
    async with get_session_factory()() as session:
        claimed = (await session.execute(
            update(models.OutboxMessage)
            .where(models.OutboxMessage.id == oid,
                   models.OutboxMessage.status.in_(["PENDING", "FAILED"]))
            .values(status="SENDING", locked_at=datetime.now(UTC), locked_by="deliver")
            .returning(models.OutboxMessage.id))).first()
        if claimed is None:
            await session.commit()
            return "SKIPPED_NOT_CLAIMABLE"
        row = (await session.execute(
            select(models.OutboxMessage).where(models.OutboxMessage.id == oid))).scalar_one()
        conversation_id = row.conversation_id
        message_type = row.message_type
        payload = dict(row.payload)
        attempt_no = row.attempt_count + 1

        # 2) defense 2：公开回复（非私有备注）发送前必须 state==BOT_ACTIVE（PLAN §六 权威闸门）。
        # 判据用"是否私有备注"而非 message_type 名——非私有备注均为客户可见，杜绝新增公开类型漏门。
        state = (await session.execute(
            select(models.AutomationState.state)
            .where(models.AutomationState.conversation_id == conversation_id)
        )).scalar_one_or_none()
        is_public = message_type != "private_note"
        if is_public and state != "BOT_ACTIVE":
            await session.execute(
                update(models.OutboxMessage).where(models.OutboxMessage.id == oid)
                .values(status="CANCELLED", last_error_code="TAKEOVER_AT_SEND"))
            await session.execute(insert(models.DeliveryAttempt).values(
                outbox_id=oid, attempt_no=attempt_no, outcome="CANCELLED",
                error_code="TAKEOVER_AT_SEND"))
            await session.commit()
            return "CANCELLED"

        target = await _resolve_target(session, conversation_id)
        await session.execute(
            update(models.OutboxMessage).where(models.OutboxMessage.id == oid)
            .values(attempt_count=attempt_no))
        await session.commit()

    if target is None:
        return await _finalize(oid, "NEEDS_REVIEW", attempt_no=attempt_no,
                               error_code="NO_MAPPING", error_message="no chatwoot mapping")

    account_id, chatwoot_conv_id = target

    # 3) 发送（不持 DB 事务，避免网络 I/O 期间持锁）
    client = get_chatwoot_client()
    try:
        chatwoot_message_id = await client.create_message(
            account_id=account_id, conversation_id=chatwoot_conv_id,
            content=payload["text"], private=(message_type == "private_note"))
    except (httpx.TimeoutException, httpx.TransportError) as e:
        # 歧义失败：不确定 Chatwoot 是否已创建消息，无客户端幂等键——不重试，转人工（PLAN §十一）
        return await _finalize(oid, "NEEDS_REVIEW", attempt_no=attempt_no,
                               error_code="AMBIGUOUS_SEND", error_message=repr(e))
    except Exception as e:  # noqa: BLE001 明确失败（如 4xx/5xx HTTPStatusError）：可重试，超阈值转人工
        if attempt_no >= _MAX_ATTEMPTS:
            return await _finalize(oid, "NEEDS_REVIEW", attempt_no=attempt_no,
                                   error_code="SEND_ERROR", error_message=repr(e))
        # 指数退避（简化：立即到期；生产可换真正退避）
        next_at = datetime.now(UTC)
        return await _finalize(oid, "FAILED", attempt_no=attempt_no,
                               error_code="SEND_ERROR", error_message=repr(e),
                               next_attempt_at=next_at)

    # 4) 成功：SENT + 存 chatwoot_message_id（闭合 Plan 1 回声断路器）
    return await _finalize(oid, "SENT", attempt_no=attempt_no,
                           chatwoot_message_id=chatwoot_message_id)
