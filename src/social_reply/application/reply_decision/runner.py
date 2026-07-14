import uuid

import redis.asyncio as aioredis

from social_reply.application.reply_decision.persist import persist_decision
from social_reply.application.reply_decision.pipeline import (
    DecisionSnapshot,
    run_decision_pipeline,
)
from social_reply.domain.reply.llm import StubLLMClient
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.killswitch import KillSwitchChecker
from social_reply.shared.config import get_settings

_llm = StubLLMClient()  # 先 Stub 后接真：Plan 2b/后续按 settings.llm_provider 切换


def _make_killswitch() -> KillSwitchChecker:
    return KillSwitchChecker(aioredis.from_url(get_settings().redis_url))


async def run_and_persist_decision(
    snapshot: DecisionSnapshot, conversation_id: uuid.UUID,
    message_id: uuid.UUID, account_id: uuid.UUID,
) -> uuid.UUID | None:
    """tx1 提交后调用：跑纯管线（不持事务），再在 tx2 写决策+outbox。
    返回 outbox_id（供 Plan 2b enqueue 投递）。"""
    settings = get_settings()
    decision = await run_decision_pipeline(snapshot, llm=_llm, killswitch=_make_killswitch())
    async with get_session_factory()() as session:
        outbox_id = await persist_decision(
            session, snapshot, conversation_id, message_id, account_id,
            decision, settings.prompt_version,
        )
        await session.commit()
    # Plan 2b：if outbox_id: enqueue deliver_outbox(outbox_id)
    return outbox_id
