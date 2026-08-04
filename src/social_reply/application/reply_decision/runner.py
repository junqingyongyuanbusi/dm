import logging
import time
import uuid
from datetime import UTC, datetime

import redis.asyncio as aioredis
from sqlalchemy import select, update

from social_reply.application.knowledge.retrieval import (
    KnowledgeHit,
    retrieve_exact_knowledge,
    retrieve_hybrid_knowledge,
)
from social_reply.application.reply_decision.persist import persist_decision
from social_reply.application.reply_decision.persona import (
    ResolvedPersona,
    load_persona,
    prompt_version_label,
)
from social_reply.application.reply_decision.pipeline import (
    DecisionSnapshot,
    run_decision_pipeline,
)
from social_reply.domain.knowledge.embeddings import EmbeddingClient, OpenAIEmbeddingClient
from social_reply.domain.reply.decision import ReplyAction, ReplyDecision
from social_reply.domain.reply.guard import redact_pii
from social_reply.domain.reply.llm import LLMClient, StubLLMClient
from social_reply.domain.reply.openai_client import OpenAILLMClient
from social_reply.domain.reply.rules import apply_rules
from social_reply.domain.reply.voice import DEFAULT_PERSONA
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.advisory_locks import (
    acquire_conversation_delivery_xact_lock,
)
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.killswitch import KillSwitchChecker
from social_reply.shared.config import get_settings

logger = logging.getLogger(__name__)

_DEFAULT_PERSONA = ResolvedPersona(text=DEFAULT_PERSONA, revision=None)

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
            query_embedding = (await embedder.embed([redact_pii(snapshot.text or "")]))[0]
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


class DecisionContextScopeError(RuntimeError):
    pass


class DecisionSuperseded(RuntimeError):
    pass


async def _validate_decision_scope(
    snapshot: DecisionSnapshot,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    account_id: uuid.UUID,
) -> int:
    """Validate every durable identifier before any customer text leaves the process."""
    if snapshot.account_id != str(account_id):
        raise DecisionContextScopeError("snapshot_account_mismatch")
    async with get_session_factory()() as session:
        current = (
            await session.execute(
                select(models.Message.history_seq, models.Message.text)
                .join(
                    models.Conversation,
                    models.Message.conversation_id == models.Conversation.id,
                )
                .join(
                    models.PlatformAccount,
                    models.Conversation.platform_account_id == models.PlatformAccount.id,
                )
                .where(
                    models.Message.id == message_id,
                    models.Message.conversation_id == conversation_id,
                    models.Message.direction == "inbound",
                    models.Message.private.is_(False),
                    models.Conversation.id == conversation_id,
                    models.Conversation.tenant_id == snapshot.tenant_id,
                    models.Conversation.brand_id == snapshot.brand_id,
                    models.Conversation.platform == snapshot.platform,
                    models.Conversation.platform_account_id == account_id,
                    models.Conversation.conversation_key == snapshot.conversation_key,
                    models.Conversation.channel_type == snapshot.channel_type,
                    models.PlatformAccount.id == account_id,
                    models.PlatformAccount.tenant_id == snapshot.tenant_id,
                    models.PlatformAccount.brand_id == snapshot.brand_id,
                    models.PlatformAccount.platform == snapshot.platform,
                )
            )
        ).one_or_none()
    if current is None or current.text != snapshot.text:
        raise DecisionContextScopeError("decision_context_scope_mismatch")
    return int(current.history_seq)


async def _fetch_history(
    conversation_id: uuid.UUID, cutoff_seq: int
) -> tuple[tuple[str, str], ...]:
    """Read the immutable message prefix before the current inbound message."""
    settings = get_settings()
    limit = settings.conversation_history_limit
    max_chars = settings.conversation_history_max_chars
    if limit <= 0 or max_chars <= 0:
        return ()
    try:
        async with get_session_factory()() as session:
            rows = (
                await session.execute(
                    select(models.Message.direction, models.Message.text)
                    .where(
                        models.Message.conversation_id == conversation_id,
                        models.Message.history_seq < cutoff_seq,
                        models.Message.private.is_(False),
                        models.Message.text.isnot(None),
                    )
                    .order_by(models.Message.history_seq.desc())
                    .limit(limit)
                )
            ).all()
    except Exception:
        logger.warning(
            "会话历史读取失败，按无历史继续: conversation_id=%s",
            conversation_id,
            exc_info=True,
        )
        return ()

    remaining = max_chars
    newest_first: list[tuple[str, str]] = []
    truncated = False
    for direction, text in rows:
        if direction == "inbound":
            role = "user"
        elif direction == "outbound":
            role = "assistant"
        else:
            logger.warning(
                "忽略未知消息方向: conversation_id=%s direction=%s",
                conversation_id,
                direction,
            )
            continue
        if remaining <= 0:
            truncated = True
            break
        if not text:
            continue
        safe_text = redact_pii(text)
        selected = safe_text[:remaining]
        if len(selected) < len(safe_text):
            truncated = True
        newest_first.append((role, selected))
        remaining -= len(selected)
    if truncated:
        logger.info(
            "会话历史按字符预算截断: conversation_id=%s max_chars=%d",
            conversation_id,
            max_chars,
        )
    return tuple(reversed(newest_first))


async def run_and_persist_decision(
    snapshot: DecisionSnapshot,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    account_id: uuid.UUID,
    *,
    decision_job_id: uuid.UUID | None = None,
    decision_generation: int | None = None,
    claim_token: uuid.UUID | None = None,
    raw_event_id: uuid.UUID | None = None,
) -> uuid.UUID | None:
    """Run the pipeline without a database transaction, then persist atomically.

    The optional job arguments fence durable workers. Omitting them preserves the direct-call
    behavior used by tests and administrative callers.
    """
    started = time.perf_counter()
    settings = get_settings()
    cutoff_seq = await _validate_decision_scope(snapshot, conversation_id, message_id, account_id)
    # 先给默认值：fail-closed 分支不走管线，但下面落库时仍要拿它拼 prompt_version。
    persona = _DEFAULT_PERSONA
    try:
        killswitch = _make_killswitch()
    except Exception:
        # redis_url 配置错误等构造期异常也必须 fail-closed：
        # 不得逃逸为静默决策丢失；与管线内部急停不可用同路，降级为草稿而非放行外发。
        decision = ReplyDecision(
            action=ReplyAction.DRAFT, reason_codes=("KILLSWITCH_UNAVAILABLE",), source="rule"
        )
    else:
        deterministic_rule = apply_rules(snapshot.text)
        should_retrieve = snapshot.automation_state != "HUMAN_ACTIVE" and deterministic_rule is None
        hits = await _fetch_knowledge(snapshot) if should_retrieve else ()
        # 模板直答：必须基于「相似度最高」的命中判断，不能用 RRF 序的 hits[0]——
        # RRF 分最高 ≠ 相似度最高，否则会误发词法命中的错模板，或漏发真正强命中的向量项。
        # 仅精确匹配或达阈值向量命中（verbatim_safe）才原文外发；词法-only/低相似度只作 LLM 上下文。
        selected_verbatim_hit: KnowledgeHit | None = None
        if hits and settings.knowledge_verbatim_reply:
            top_by_similarity = max(hits, key=lambda h: h.similarity)
            if top_by_similarity.verbatim_safe:
                selected_verbatim_hit = top_by_similarity
        verbatim = selected_verbatim_hit.reply if selected_verbatim_hit is not None else None
        approved_official_contact_reply = (
            selected_verbatim_hit.reply
            if selected_verbatim_hit is not None and selected_verbatim_hit.is_official_contact
            else None
        )
        require_knowledge = settings.knowledge_retrieval_enabled and settings.require_knowledge
        needs_llm_history = (
            should_retrieve and verbatim is None and not (require_knowledge and not hits)
        )
        history = await _fetch_history(conversation_id, cutoff_seq) if needs_llm_history else ()
        # 人设只在真要调 LLM 时才读：确定性规则与模板直答都不经过提示词。
        if should_retrieve and verbatim is None and not (require_knowledge and not hits):
            async with get_session_factory()() as session:
                persona = await load_persona(session, snapshot.tenant_id, snapshot.brand_id)
        decision = await run_decision_pipeline(
            snapshot,
            llm=_get_llm(),
            killswitch=killswitch,
            knowledge=tuple(h.content for h in hits),
            require_knowledge=require_knowledge,
            verbatim_reply=verbatim,
            approved_official_contact_reply=approved_official_contact_reply,
            history=history,
            voice_preferences=persona.preferences,
        )
    async with get_session_factory()() as session:
        if decision_job_id is not None:
            if decision_generation is None or claim_token is None:
                raise ValueError("decision_job_fence_required")
            await acquire_conversation_delivery_xact_lock(session, conversation_id)
            current_generation = await session.scalar(
                select(models.Conversation.decision_generation)
                .where(models.Conversation.id == conversation_id)
                .with_for_update()
            )
            job = (
                await session.execute(
                    select(models.DecisionJob)
                    .where(models.DecisionJob.id == decision_job_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if (
                job is None
                or job.status != "PROCESSING"
                or job.claim_token != claim_token
                or job.decision_generation != decision_generation
                or current_generation != decision_generation
            ):
                if (
                    job is not None
                    and job.status == "PROCESSING"
                    and job.claim_token == claim_token
                ):
                    job.status = "SUPERSEDED"
                    job.claim_token = None
                    job.locked_at = None
                    job.next_attempt_at = None
                    job.completed_at = datetime.now(UTC)
                    job.last_error = "superseded before decision finalization"
                    if raw_event_id is not None:
                        from social_reply.application.reply_decision.jobs import (
                            aggregate_raw_event_decisions,
                        )

                        await aggregate_raw_event_decisions(session, raw_event_id)
                await session.commit()
                raise DecisionSuperseded("decision_generation_superseded")
        outbox_id = await persist_decision(
            session,
            snapshot,
            conversation_id,
            message_id,
            account_id,
            decision,
            prompt_version_label(settings.prompt_version, persona),
            decision_job_id=decision_job_id,
            decision_generation=decision_generation,
            decision_claim_token=claim_token,
        )
        if decision_job_id is not None:
            completed = await session.execute(
                update(models.DecisionJob)
                .where(
                    models.DecisionJob.id == decision_job_id,
                    models.DecisionJob.status == "PROCESSING",
                    models.DecisionJob.claim_token == claim_token,
                    models.DecisionJob.decision_generation == decision_generation,
                )
                .values(
                    status="COMPLETED",
                    completed_at=datetime.now(UTC),
                    next_attempt_at=None,
                    locked_at=None,
                    claim_token=None,
                    last_error=None,
                )
            )
            if completed.rowcount != 1:
                await session.rollback()
                raise DecisionSuperseded("decision_claim_lost")
            if raw_event_id is not None:
                from social_reply.application.reply_decision.jobs import (
                    aggregate_raw_event_decisions,
                )

                await aggregate_raw_event_decisions(session, raw_event_id)
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
