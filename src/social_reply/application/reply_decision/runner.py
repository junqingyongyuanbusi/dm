import logging
import time
import uuid

import redis.asyncio as aioredis

from social_reply.application.knowledge.retrieval import (
    KnowledgeHit,
    retrieve_exact_knowledge,
    retrieve_hybrid_knowledge,
)
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
    # 模块级共享 client（惰性初始化）：避免每次决策 from_url 新建连接池
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
        # 短事务：先做确定性精确匹配。命中时无需调用 embedding API，
        # 对 True/Hi/Thanks 等短模板既更准也更快。
        async with get_session_factory()() as session:
            exact = await retrieve_exact_knowledge(
                session,
                snapshot.text or "",
                tenant_id=snapshot.tenant_id,
                brand_id=snapshot.brand_id,
                platform=snapshot.platform,
            )
        if exact is not None:
            hits = [exact]
        else:
            embedder = _get_embedder()
            query_embedding = (await embedder.embed([snapshot.text or ""]))[0]
            async with get_session_factory()() as session:
                # 混合检索：向量 + 词法 RRF 融合扩大召回；verbatim_safe 标记保护原文直答闸门
                hits = await retrieve_hybrid_knowledge(
                    session,
                    query_embedding,
                    snapshot.text or "",
                    tenant_id=snapshot.tenant_id,
                    brand_id=snapshot.brand_id,
                    platform=snapshot.platform,
                    embedding_version=embedder.version,
                    top_k=settings.knowledge_top_k,
                    min_similarity=settings.knowledge_min_similarity,
                )
    except Exception:
        logger.warning(
            "知识检索失败，按无知识继续: conversation=%s",
            snapshot.conversation_key,
            exc_info=True,
        )
        return ()
    if hits:
        logger.info(
            "知识命中 %d 条: conversation=%s chunks=%s",
            len(hits),
            snapshot.conversation_key,
            [(str(h.chunk_id), h.content_hash[:12], round(h.similarity, 3)) for h in hits],
        )
    return tuple(hits)


async def run_and_persist_decision(
    snapshot: DecisionSnapshot,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    account_id: uuid.UUID,
) -> uuid.UUID | None:
    """tx1 提交后调用：跑纯管线（不持事务），再在 tx2 写决策+outbox。
    返回 outbox_id（供后续投递入队）。"""
    started = time.perf_counter()
    settings = get_settings()
    try:
        killswitch = _make_killswitch()
    except Exception:
        # redis_url 配置错误等构造期异常也必须 fail-closed：
        # 不得逃逸为静默决策丢失；与管线内部急停不可用同路，降级为草稿而非放行外发。
        decision = ReplyDecision(
            action=ReplyAction.DRAFT, reason_codes=("KILLSWITCH_UNAVAILABLE",), source="rule"
        )
    else:
        hits = await _fetch_knowledge(snapshot)
        # 模板直答：仅当最高分命中是 verbatim_safe（精确匹配或达到相似度阈值的向量命中）
        # 才原文外发；词法-only/低相似度命中只作为 LLM 上下文，不直接当答案发出。
        verbatim = (
            hits[0].reply
            if hits and settings.knowledge_verbatim_reply and hits[0].verbatim_safe
            else None
        )
        decision = await run_decision_pipeline(
            snapshot,
            llm=_get_llm(),
            killswitch=killswitch,
            knowledge=tuple(h.content for h in hits),
            # require_knowledge 仅在检索开启时生效，否则会把所有消息误降级为转人工
            require_knowledge=settings.knowledge_retrieval_enabled and settings.require_knowledge,
            verbatim_reply=verbatim,
        )
        # 低风险 LLM 不确定不应静默、也不应永久锁死会话。仅把 LLM 自身给出的
        # handoff 转为公开兜底；风险词、知识不足强制转人工、Guard 失败仍保持 handoff。
        if decision.action is ReplyAction.HANDOFF and decision.source == "llm":
            decision = ReplyDecision(
                action=ReplyAction.AUTO_REPLY,
                reply_text="抱歉，我暂时无法准确回答这个问题。请换一种说法或提供更多信息。",
                reason_codes=decision.reason_codes + ("LLM_HANDOFF_FALLBACK",),
                source="rule",
            )
    async with get_session_factory()() as session:
        outbox_id = await persist_decision(
            session,
            snapshot,
            conversation_id,
            message_id,
            account_id,
            decision,
            settings.prompt_version,
        )
        await session.commit()
    decision_ms = (time.perf_counter() - started) * 1000
    if outbox_id is not None:
        # Fast Path：事务提交后立即认领并投递，绕过 Redis/Dramatiq 的排队延迟。
        # deliver_outbox 自带原子 claim、发送前状态复检与歧义失败处理；若本进程在此处
        # 崩溃，PENDING 仍由 scheduler 补扫，可靠性不因低延迟路径而下降。
        from social_reply.application.message_delivery.outbox import deliver_outbox

        delivery_started = time.perf_counter()
        result = await deliver_outbox(str(outbox_id))
        delivery_ms = (time.perf_counter() - delivery_started) * 1000
        logger.info(
            "reply_fast_path conversation=%s decision_ms=%.1f delivery_ms=%.1f "
            "total_ms=%.1f result=%s",
            snapshot.conversation_key,
            decision_ms,
            delivery_ms,
            (time.perf_counter() - started) * 1000,
            result,
        )
    else:
        logger.info(
            "reply_no_delivery conversation=%s decision_ms=%.1f action=%s",
            snapshot.conversation_key,
            decision_ms,
            decision.action,
        )
    return outbox_id
