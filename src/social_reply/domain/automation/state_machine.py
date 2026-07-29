import uuid
from enum import StrEnum

from sqlalchemy import insert, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.advisory_locks import (
    acquire_conversation_delivery_xact_lock,
)


class AutomationStateEnum(StrEnum):
    BOT_ACTIVE = "BOT_ACTIVE"
    BOT_DRAFT_ONLY = "BOT_DRAFT_ONLY"
    HANDOFF_PENDING = "HANDOFF_PENDING"
    HUMAN_ACTIVE = "HUMAN_ACTIVE"
    BOT_COOLDOWN = "BOT_COOLDOWN"
    CLOSED = "CLOSED"


_ALLOWED: dict[AutomationStateEnum, set[AutomationStateEnum]] = {
    AutomationStateEnum.BOT_ACTIVE: {
        AutomationStateEnum.HANDOFF_PENDING,
        AutomationStateEnum.BOT_DRAFT_ONLY,
        AutomationStateEnum.HUMAN_ACTIVE,
        AutomationStateEnum.CLOSED,
    },
    AutomationStateEnum.BOT_DRAFT_ONLY: {
        AutomationStateEnum.HUMAN_ACTIVE,
        AutomationStateEnum.BOT_ACTIVE,
        AutomationStateEnum.CLOSED,
    },
    AutomationStateEnum.HANDOFF_PENDING: {
        AutomationStateEnum.HUMAN_ACTIVE,
        AutomationStateEnum.BOT_ACTIVE,
        AutomationStateEnum.CLOSED,
    },
    AutomationStateEnum.HUMAN_ACTIVE: {
        AutomationStateEnum.BOT_COOLDOWN,
        AutomationStateEnum.CLOSED,
    },
    AutomationStateEnum.BOT_COOLDOWN: {
        AutomationStateEnum.BOT_ACTIVE,
        AutomationStateEnum.HUMAN_ACTIVE,
        AutomationStateEnum.CLOSED,
    },
    AutomationStateEnum.CLOSED: {
        AutomationStateEnum.BOT_ACTIVE,
        AutomationStateEnum.HUMAN_ACTIVE,
    },
}


def can_transition(src: AutomationStateEnum, dst: AutomationStateEnum) -> bool:
    return dst in _ALLOWED.get(src, set()) and src is not dst


async def ensure_state(
    session: AsyncSession, conversation_id: uuid.UUID, default_state: str
) -> None:
    """会话首次出现时初始化状态行；已存在则不动（幂等）"""
    stmt = (
        pg_insert(models.AutomationState)
        .values(conversation_id=conversation_id, state=default_state)
        .on_conflict_do_nothing(index_elements=["conversation_id"])
    )
    await session.execute(stmt)


async def flip_to_human_active(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    agent_id: str | None,
    reason: str,
    *,
    expected_state: AutomationStateEnum | None = None,
) -> bool:
    """人工接管翻转：非 HUMAN_ACTIVE 才更新（幂等），state_version 自增（CAS 基础）。
    返回是否发生了翻转。"""
    await acquire_conversation_delivery_xact_lock(session, conversation_id)
    stmt = update(models.AutomationState).where(
        models.AutomationState.conversation_id == conversation_id,
        models.AutomationState.state != AutomationStateEnum.HUMAN_ACTIVE,
    )
    if expected_state is not None:
        stmt = stmt.where(models.AutomationState.state == expected_state)
    stmt = stmt.values(
        state=AutomationStateEnum.HUMAN_ACTIVE,
        state_version=models.AutomationState.state_version + 1,
        human_agent_id=agent_id,
        state_changed_reason=reason,
    )
    result = await session.execute(stmt)
    flipped = result.rowcount > 0
    if flipped:
        await session.execute(
            insert(models.AuditLog).values(
                category="state_transition",
                actor=f"agent:{agent_id}" if agent_id else "system",
                action="HUMAN_ACTIVE",
                subject_type="conversation",
                subject_id=str(conversation_id),
                detail={"reason": reason},
            )
        )
        # The shared advisory lock serializes this cancellation with external delivery.
        await session.execute(
            update(models.OutboxMessage)
            .where(
                models.OutboxMessage.conversation_id == conversation_id,
                models.OutboxMessage.status.in_(["PENDING", "FAILED"]),
            )
            .values(status="CANCELLED", last_error_code="TAKEOVER")
        )
    return flipped
