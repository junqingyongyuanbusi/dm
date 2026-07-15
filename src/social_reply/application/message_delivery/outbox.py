import logging
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.connectors.chatwoot.client import get_chatwoot_client
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory

logger = logging.getLogger(__name__)

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
        # SENDING 守卫：行可能已被 sweep 转 NEEDS_REVIEW 等终态，迟到的 finalize 不得覆盖
        result = await session.execute(
            update(models.OutboxMessage)
            .where(models.OutboxMessage.id == outbox_id,
                   models.OutboxMessage.status == "SENDING")
            .values(**values))
        if result.rowcount == 0:
            logger.warning(
                "outbox %s 终态更新被跳过：行已非 SENDING（可能被 sweep 接管），目标状态=%s",
                outbox_id, status)
        # DeliveryAttempt 审计行始终写入——记录"本次尝试发生了什么"的事实，与 outbox 状态解耦
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

    async def _fail_retryable(e: Exception) -> str:
        """明确未送达的失败：FAILED 可重试（指数退避），超阈值转人工。"""
        if attempt_no >= _MAX_ATTEMPTS:
            return await _finalize(oid, "NEEDS_REVIEW", attempt_no=attempt_no,
                                   error_code="SEND_ERROR", error_message=repr(e))
        # 指数退避：30s * 2^attempt_count，上限 1h
        next_at = datetime.now(UTC) + timedelta(
            seconds=min(30 * 2 ** attempt_no, 3600))
        return await _finalize(oid, "FAILED", attempt_no=attempt_no,
                               error_code="SEND_ERROR", error_message=repr(e),
                               next_attempt_at=next_at)

    try:
        chatwoot_message_id = await client.create_message(
            account_id=account_id, conversation_id=chatwoot_conv_id,
            content=payload["text"], private=(message_type == "private_note"))
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        # 连接阶段失败（TCP/TLS 未建立或连接超时）：请求必然未发出，Chatwoot 侧不可能已建消息
        # → 明确未送达，FAILED 可重试。注意 ConnectError 是 TransportError 子类、
        # ConnectTimeout 是 TimeoutException 子类，本分支必须排在歧义分支之前。
        return await _fail_retryable(e)
    except (httpx.TimeoutException, httpx.TransportError) as e:
        # 其余传输失败（读超时、写中断等）：请求可能已到达服务端，无客户端幂等键
        # → 歧义失败，不重试，转人工（PLAN §十一）
        return await _finalize(oid, "NEEDS_REVIEW", attempt_no=attempt_no,
                               error_code="AMBIGUOUS_SEND", error_message=repr(e))
    except httpx.HTTPStatusError as e:
        if e.response.status_code >= 500:
            # 5xx：服务端可能已创建消息后才出错 → 同样歧义，转人工，避免盲目重试造成重复发送
            return await _finalize(oid, "NEEDS_REVIEW", attempt_no=attempt_no,
                                   error_code="AMBIGUOUS_SEND", error_message=repr(e))
        # 4xx：服务端明确拒绝，未建消息 → 可重试，超阈值转人工
        return await _fail_retryable(e)
    except Exception as e:  # noqa: BLE001 其它未知错误：按明确失败处理，可重试
        return await _fail_retryable(e)

    # 4) 成功：SENT + 存 chatwoot_message_id（闭合 Plan 1 回声断路器）
    return await _finalize(oid, "SENT", attempt_no=attempt_no,
                           chatwoot_message_id=chatwoot_message_id)
