import uuid

import redis.asyncio as aioredis

from social_reply.application.reply_decision.persist import persist_decision
from social_reply.application.reply_decision.pipeline import (
    DecisionSnapshot,
    run_decision_pipeline,
)
from social_reply.domain.reply.decision import ReplyAction, ReplyDecision
from social_reply.domain.reply.llm import LLMClient, StubLLMClient
from social_reply.domain.reply.openai_client import OpenAILLMClient
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.killswitch import KillSwitchChecker
from social_reply.shared.config import get_settings

_llm: LLMClient | None = None


def _get_llm() -> LLMClient:
    # 惰性单例（模仿 _get_redis）：按 settings.llm_provider 切换 Stub/OpenAI。
    # 构造仅拼参数不联网，配置校验已在 Settings 层完成，故无需与 killswitch 同路 fail-closed。
    global _llm
    if _llm is None:
        settings = get_settings()
        if settings.llm_provider == "openai":
            _llm = OpenAILLMClient(
                api_key=settings.openai_api_key,
                base_url=settings.openai_base_url,
                model=settings.openai_model,
                timeout=settings.openai_timeout_seconds,
            )
        elif settings.llm_provider == "stub":
            _llm = StubLLMClient()
        else:
            raise ValueError(f"未知 LLM_PROVIDER: {settings.llm_provider}（仅支持 stub/openai）")
    return _llm


_redis = None


def _get_redis():
    # 模块级共享 client（惰性初始化）：避免每次决策 from_url 新建连接池（Plan 2a 评审 M1）
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(get_settings().redis_url)
    return _redis


def _make_killswitch() -> KillSwitchChecker:
    return KillSwitchChecker(_get_redis())


async def run_and_persist_decision(
    snapshot: DecisionSnapshot, conversation_id: uuid.UUID,
    message_id: uuid.UUID, account_id: uuid.UUID,
) -> uuid.UUID | None:
    """tx1 提交后调用：跑纯管线（不持事务），再在 tx2 写决策+outbox。
    返回 outbox_id（供 Plan 2b enqueue 投递）。"""
    settings = get_settings()
    try:
        killswitch = _make_killswitch()
    except Exception:
        # redis_url 配置错误等构造期异常也必须 fail-closed（Task 9 评审 I1）：
        # 不得逃逸为静默决策丢失；与管线内部急停不可用同路，降级为草稿而非放行外发。
        decision = ReplyDecision(action=ReplyAction.DRAFT,
                                 reason_codes=("KILLSWITCH_UNAVAILABLE",), source="rule")
    else:
        decision = await run_decision_pipeline(snapshot, llm=_get_llm(), killswitch=killswitch)
    async with get_session_factory()() as session:
        outbox_id = await persist_decision(
            session, snapshot, conversation_id, message_id, account_id,
            decision, settings.prompt_version,
        )
        await session.commit()
    if outbox_id is not None:
        # 函数内延迟 import，避免 web 进程 import broker 副作用扩散
        from social_reply.application.message_delivery.actors import deliver_outbox_message

        deliver_outbox_message.send(str(outbox_id))
    return outbox_id
