import hashlib
import uuid

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.application.reply_decision.pipeline import DecisionSnapshot
from social_reply.domain.automation.state_machine import AutomationStateEnum
from social_reply.domain.reply.decision import ReplyAction, ReplyDecision
from social_reply.infrastructure.database import models


def _idempotency_key(account_id: uuid.UUID, conversation_id: uuid.UUID,
                     message_id: uuid.UUID, action: str) -> str:
    # PLAN.md §十二：不含 prompt_version（换版重投不得产生重复发送）
    raw = f"{account_id}:{conversation_id}:{message_id}:{action}"
    return hashlib.sha256(raw.encode()).hexdigest()


async def persist_decision(
    session: AsyncSession, snapshot: DecisionSnapshot, conversation_id: uuid.UUID,
    message_id: uuid.UUID | None, account_id: uuid.UUID, decision: ReplyDecision,
    prompt_version: str,
) -> uuid.UUID | None:
    """在调用方事务内写 reply_decisions（永远写）+ 按 action 落地副作用。
    auto_reply/draft → 写 outbox（auto_reply 受 state_version CAS 守护，defense 1）。
    返回 outbox_id 或 None。调用方负责 commit。"""
    outbox_id: uuid.UUID | None = None
    message_type: str | None = None

    if decision.action is ReplyAction.AUTO_REPLY:
        # CAS defense 1：仅当会话仍是 BOT_ACTIVE 且 version 未变时才写 outbox
        current = (await session.execute(
            select(models.AutomationState.state, models.AutomationState.state_version)
            .where(models.AutomationState.conversation_id == conversation_id)
        )).first()
        if (current is not None
                and current.state == AutomationStateEnum.BOT_ACTIVE
                and current.state_version == snapshot.state_version):
            message_type = "text"
    elif decision.action is ReplyAction.DRAFT:
        message_type = "private_note"
    elif decision.action is ReplyAction.HANDOFF:
        # 转人工：置 HANDOFF_PENDING（仅当当前非终态）
        await session.execute(
            update(models.AutomationState)
            .where(
                models.AutomationState.conversation_id == conversation_id,
                models.AutomationState.state.notin_(
                    [AutomationStateEnum.HUMAN_ACTIVE, AutomationStateEnum.CLOSED]
                ),
            )
            .values(state=AutomationStateEnum.HANDOFF_PENDING,
                    state_version=models.AutomationState.state_version + 1,
                    state_changed_reason="rule_or_guard_handoff")
        )

    if message_type is not None:
        outbox_id = uuid.uuid4()
        await session.execute(insert(models.OutboxMessage).values(
            id=outbox_id, tenant_id="default", conversation_id=conversation_id,
            platform_account_id=account_id,
            destination_type="chatwoot_conversation", destination_id=snapshot.conversation_key,
            message_type=message_type,
            payload={"text": decision.reply_text or "", "visibility": decision.reply_visibility},
            idempotency_key=_idempotency_key(account_id, conversation_id,
                                             message_id or conversation_id, decision.action),
            status="PENDING",
        ))

    await session.execute(insert(models.ReplyDecision).values(
        id=uuid.uuid4(), tenant_id="default", conversation_id=conversation_id,
        message_id=message_id, action=decision.action, intent=decision.intent,
        risk_level=decision.risk_level, confidence=decision.confidence,
        reply_text=decision.reply_text, reply_visibility=decision.reply_visibility,
        reason_codes=list(decision.reason_codes), source=decision.source,
        prompt_version=prompt_version, state_version_at_decision=snapshot.state_version,
        outbox_id=outbox_id,
    ))
    return outbox_id
