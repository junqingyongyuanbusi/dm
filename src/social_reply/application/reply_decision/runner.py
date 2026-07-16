import logging
import uuid

import redis.asyncio as aioredis

from social_reply.application.knowledge.retrieval import KnowledgeHit, retrieve_knowledge
from social_reply.application.reply_decision.persist import persist_decision
from social_reply.application.reply_decision.pipeline import (
    DecisionSnapshot,
    run_decision_pipeline,
)
from social_reply.domain.knowledge.embeddings import EmbeddingClient, OpenAIEmbeddingClient
from social_reply.domain.reply.decision import ReplyAction, ReplyDecision
from social_reply.domain.reply.llm import LLMClient, StubLLMClient
from social_reply.domain.reply.openai_client import OpenAILLMClient
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.killswitch import KillSwitchChecker
from social_reply.shared.config import get_settings

logger = logging.getLogger(__name__)

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


_embedder: EmbeddingClient | None = None


def _get_embedder() -> EmbeddingClient:
    # 惰性单例（模仿 _get_llm）：测试可直接替换 runner._embedder 注入 Fake
    global _embedder
    if _embedder is None:
        settings = get_settings()
        _embedder = OpenAIEmbeddingClient(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            model=settings.openai_embedding_model,
            timeout=settings.openai_timeout_seconds,
        )
    return _embedder


async def _fetch_knowledge(snapshot: DecisionSnapshot) -> tuple[KnowledgeHit, ...]:
    """检索开关开启时 embed 用户消息并取 top-k 模板命中；任何失败按无知识继续不阻断决策"""
    settings = get_settings()
    if not settings.knowledge_retrieval_enabled:
        return ()
    try:
        embedder = _get_embedder()
        query_embedding = (await embedder.embed([snapshot.text or ""]))[0]
        # 短事务：检索独立于决策持久化的 tx2，不与慢 LLM 调用同事务
        async with get_session_factory()() as session:
            hits = await retrieve_knowledge(
                session, query_embedding,
                brand_id=snapshot.brand_id, platform=snapshot.platform,
                embedding_version=embedder.version,
                top_k=settings.knowledge_top_k,
                min_similarity=settings.knowledge_min_similarity,
            )
    except Exception:
        logger.warning(
            "知识检索失败，按无知识继续: conversation=%s",
            snapshot.conversation_key, exc_info=True,
        )
        return ()
    if hits:
        logger.info(
            "知识命中 %d 条: conversation=%s chunks=%s",
            len(hits), snapshot.conversation_key,
            [(str(h.chunk_id), h.content_hash[:12], round(h.similarity, 3)) for h in hits],
        )
    return tuple(hits)


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
        hits = await _fetch_knowledge(snapshot)
        # 模板直答：命中且开启 verbatim 时取相似度最高一条的原文回复（hits 已按距离升序）
        verbatim = (
            hits[0].reply
            if hits and settings.knowledge_verbatim_reply
            else None
        )
        decision = await run_decision_pipeline(
            snapshot, llm=_get_llm(), killswitch=killswitch,
            knowledge=tuple(h.content for h in hits),
            # require_knowledge 仅在检索开启时生效，否则会把所有消息误降级为转人工
            require_knowledge=settings.knowledge_retrieval_enabled
            and settings.require_knowledge,
            verbatim_reply=verbatim,
        )
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
