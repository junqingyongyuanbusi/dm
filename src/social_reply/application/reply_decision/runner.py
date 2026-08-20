import json
import logging
import time
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime

import redis.asyncio as aioredis
from sqlalchemy import select, update

from social_reply.application.knowledge.localizations import load_approved_localization
from social_reply.application.knowledge.retrieval import (
    KnowledgeHit,
    KnowledgeRetrievalResult,
    retrieve_exact_knowledge_result,
    retrieve_hybrid_knowledge_result,
)
from social_reply.application.reply_decision.experimental_multilingual import (
    EXPERIMENTAL_MULTILINGUAL_CONTRACT_VERSION,
    generate_experimental_multilingual_reply,
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
from social_reply.domain.platform_accounts import LEGACY_ACTIVE_ACCOUNT_STATUSES
from social_reply.domain.reply.decision import ReplyAction, ReplyDecision
from social_reply.domain.reply.guard import redact_pii
from social_reply.domain.reply.language import assess_knowledge_language, detect_customer_language
from social_reply.domain.reply.llm import LLMClient, StubLLMClient
from social_reply.domain.reply.openai_client import OpenAILLMClient
from social_reply.domain.reply.rules import apply_multilingual_rules, apply_rules
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
_MULTILINGUAL_CONTRACT_VERSION = "multilingual-v2-reviewed-localization"
_MULTILINGUAL_SENTINEL_VERSION = "approved-verbatim-v1"
_MULTILINGUAL_GATE_VERSION = "strong-gate-v1"

_llm: LLMClient | None = None


def _get_llm() -> LLMClient:
    # 惰性单例（模仿 _get_redis）：按 settings.llm_provider 切换 Stub/OpenAI。
    # 构造仅拼参数不联网，配置校验已在 Settings 层完成，故无需与 killswitch 同路 fail-closed。
    global _llm
    if _llm is None:
        settings = get_settings()
        if settings.llm_provider == "openai":
            _llm = OpenAILLMClient(
                api_key=settings.openai_api_key.get_secret_value(),
                base_url=settings.openai_base_url,
                model=settings.openai_model,
                timeout=settings.openai_timeout_seconds,
                grounding_model=settings.openai_grounding_model or None,
                grounding_timeout=settings.grounding_verifier_timeout_seconds,
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


@dataclass(frozen=True)
class _StaticKillSwitch:
    disabled: bool

    async def is_disabled(self, brand_id: str, account_id: str, tenant_id: str = "default") -> bool:
        return self.disabled


_embedder: EmbeddingClient | None = None


def _get_embedder() -> EmbeddingClient:
    # 惰性单例（模仿 _get_llm）：测试可直接替换 runner._embedder 注入 Fake
    global _embedder
    if _embedder is None:
        settings = get_settings()
        _embedder = OpenAIEmbeddingClient(
            api_key=settings.openai_api_key.get_secret_value(),
            base_url=settings.openai_base_url,
            model=settings.openai_embedding_model,
            timeout=settings.openai_timeout_seconds,
            expected_dimensions=settings.openai_embedding_dimensions,
        )
    return _embedder


async def _fetch_knowledge(
    snapshot: DecisionSnapshot,
    *,
    verified_english_only: bool = False,
    query_embedding: tuple[float, ...] | None = None,
    force_enabled: bool = False,
) -> KnowledgeRetrievalResult:
    """Retrieve hybrid context plus pure-vector evidence for deterministic relevance gating."""
    settings = get_settings()
    if not settings.knowledge_retrieval_enabled and not force_enabled:
        return KnowledgeRetrievalResult()
    try:
        # 短事务：先做确定性精确匹配。命中时无需调用 embedding API，
        # 对 True/Hi/Thanks 等短模板既更准也更快。
        async with get_session_factory()() as session:
            exact_result = await retrieve_exact_knowledge_result(
                session,
                snapshot.text or "",
                tenant_id=snapshot.tenant_id,
                brand_id=snapshot.brand_id,
                platform=snapshot.platform,
                verified_english_only=verified_english_only,
            )
        if exact_result.exact_match or exact_result.exact_ambiguous:
            result = replace(
                exact_result,
                embedding_version=settings.openai_embedding_model,
                retrieval_mode="exact",
            )
        else:
            embedder = _get_embedder()
            embedding_values = (
                list(query_embedding)
                if query_embedding is not None
                else (await embedder.embed([redact_pii(snapshot.text or "")]))[0]
            )
            async with get_session_factory()() as session:
                # 混合检索：向量 + 词法 RRF 融合扩大召回；verbatim_safe 标记保护原文直答闸门
                result = await retrieve_hybrid_knowledge_result(
                    session,
                    embedding_values,
                    snapshot.text or "",
                    tenant_id=snapshot.tenant_id,
                    brand_id=snapshot.brand_id,
                    platform=snapshot.platform,
                    embedding_version=embedder.version,
                    top_k=settings.knowledge_top_k,
                    min_similarity=settings.knowledge_min_similarity,
                    verified_english_only=verified_english_only,
                )
            result = replace(
                result,
                query_embedding=tuple(embedding_values),
                embedding_version=embedder.version,
                retrieval_mode="vector_hybrid",
            )
    except Exception:
        logger.warning(
            "知识检索失败，强制 HANDOFF: conversation=%s",
            snapshot.conversation_key,
            exc_info=True,
        )
        return KnowledgeRetrievalResult(error_code="KNOWLEDGE_RETRIEVAL_FAILED")
    if result.hits:
        logger.info(
            "知识命中 %d 条: conversation=%s chunks=%s",
            len(result.hits),
            snapshot.conversation_key,
            [
                (str(hit.chunk_id), hit.content_hash[:12], round(hit.similarity, 3))
                for hit in result.hits
            ],
        )
    return result


@dataclass(frozen=True)
class KnowledgeMatchAssessment:
    selected: KnowledgeHit | None = None
    second: KnowledgeHit | None = None
    margin: float | None = None
    strong: bool = False
    status: str = "none"


def _assess_knowledge_match(
    result: KnowledgeRetrievalResult,
    *,
    min_similarity: float,
    min_margin: float,
) -> KnowledgeMatchAssessment:
    if result.error_code:
        return KnowledgeMatchAssessment(status="error")
    if result.exact_ambiguous:
        ordered_exact = sorted(result.hits, key=lambda hit: hit.content_hash)
        return KnowledgeMatchAssessment(
            selected=ordered_exact[0] if ordered_exact else None,
            second=ordered_exact[1] if len(ordered_exact) > 1 else None,
            margin=0.0,
            status="ambiguous",
        )
    if result.exact_match and result.hits:
        return KnowledgeMatchAssessment(selected=result.hits[0], strong=True, status="strong")
    ordered = sorted(result.vector_hits, key=lambda hit: hit.similarity, reverse=True)
    if not ordered:
        return KnowledgeMatchAssessment()
    top1 = ordered[0]
    top2 = ordered[1] if len(ordered) > 1 else None
    margin = top1.similarity - top2.similarity if top2 is not None else None
    strong = top1.similarity >= min_similarity and (margin is None or margin >= min_margin)
    status = "strong" if strong else ("weak" if top1.similarity < min_similarity else "ambiguous")
    return KnowledgeMatchAssessment(
        selected=top1,
        second=top2,
        margin=margin,
        strong=strong,
        status=status,
    )


def _knowledge_evidence_block(hit: KnowledgeHit) -> str:
    return json.dumps(
        {
            "question": hit.question,
            "approved_answer": hit.reply,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


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
                    models.PlatformAccount.status.in_(LEGACY_ACTIVE_ACCOUNT_STATUSES),
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
    prompt_contract_suffix = ""
    try:
        if snapshot.automation_state in {"BOT_ACTIVE", "BOT_DRAFT_ONLY"}:
            checker = _make_killswitch()
            killswitch_disabled = await checker.is_disabled(
                snapshot.brand_id,
                snapshot.account_id,
                snapshot.tenant_id,
            )
        else:
            killswitch_disabled = False
        killswitch = _StaticKillSwitch(killswitch_disabled)
    except Exception:
        logger.exception(
            "kill switch initialization failed; decision downgraded to draft",
            extra={
                "tenant_id": snapshot.tenant_id,
                "brand_id": snapshot.brand_id,
                "account_id": snapshot.account_id,
            },
        )
        # redis_url 配置错误等构造期异常也必须 fail-closed：
        # 不得逃逸为静默决策丢失；与管线内部急停不可用同路，降级为草稿而非放行外发。
        decision = ReplyDecision(
            action=ReplyAction.DRAFT, reason_codes=("KILLSWITCH_UNAVAILABLE",), source="rule"
        )
    else:
        experimental_multilingual = (
            settings.multilingual_experimental_reply_enabled
            and snapshot.account_id.casefold() in settings.multilingual_experimental_account_id_set
        )
        use_multilingual_path = (
            settings.multilingual_knowledge_reply_enabled or experimental_multilingual
        )
        deterministic_rule = (
            apply_multilingual_rules(snapshot.text)
            if use_multilingual_path
            else apply_rules(snapshot.text)
        )
        legacy_should_retrieve = (
            not killswitch_disabled
            and snapshot.automation_state != "HUMAN_ACTIVE"
            and deterministic_rule is None
        )
        live_should_retrieve = (
            not killswitch_disabled
            and snapshot.automation_state in {"BOT_ACTIVE", "BOT_DRAFT_ONLY"}
            and deterministic_rule is None
            and not snapshot.has_unsupported_attachment
        )
        should_retrieve = live_should_retrieve if use_multilingual_path else legacy_should_retrieve
        shadow_should_retrieve = (
            not killswitch_disabled
            and snapshot.automation_state != "HUMAN_ACTIVE"
            and bool(snapshot.text and snapshot.text.strip())
            and not snapshot.has_unsupported_attachment
        )
        language_should_detect = (
            not killswitch_disabled
            and snapshot.automation_state in {"BOT_ACTIVE", "BOT_DRAFT_ONLY"}
            and bool(snapshot.text and snapshot.text.strip())
        )
        if use_multilingual_path:
            history = (
                await _fetch_history(conversation_id, cutoff_seq) if language_should_detect else ()
            )
            language = detect_customer_language(snapshot.text, history)
            language_supported = (
                language.tag.split("-", 1)[0].casefold()
                in settings.multilingual_supported_language_set
            )
            knowledge_result = (
                await _fetch_knowledge(
                    snapshot,
                    verified_english_only=(
                        settings.english_knowledge_only_enabled and not experimental_multilingual
                    ),
                )
                if should_retrieve and language.is_known and language_supported
                else KnowledgeRetrievalResult()
            )
            gate_min_similarity = (
                settings.multilingual_experimental_min_similarity
                if experimental_multilingual
                else settings.knowledge_auto_reply_min_similarity
            )
            gate_min_margin = (
                settings.multilingual_experimental_min_margin
                if experimental_multilingual
                else settings.knowledge_auto_reply_min_margin
            )
            assessment = _assess_knowledge_match(
                knowledge_result,
                min_similarity=gate_min_similarity,
                min_margin=gate_min_margin,
            )
            gate_evaluated = (
                should_retrieve
                and language.is_known
                and language_supported
                and not knowledge_result.error_code
            )
            selected = assessment.selected
            experimental_source_is_english = True
            if experimental_multilingual and selected is not None:
                source_language, source_status = assess_knowledge_language(
                    selected.question, selected.reply
                )
                experimental_source_is_english = (
                    source_language == "en" and source_status == "english"
                )
            is_english_request = language.tag.split("-", 1)[0].casefold() == "en"
            approved_localization = None
            localization_error = False
            if (
                should_retrieve
                and language.is_known
                and language_supported
                and not is_english_request
                and not experimental_multilingual
                and assessment.strong
                and selected is not None
                and not knowledge_result.error_code
            ):
                try:
                    async with get_session_factory()() as session:
                        approved_localization = await load_approved_localization(
                            session,
                            tenant_id=snapshot.tenant_id,
                            document_id=selected.document_id,
                            source_content_hash=selected.content_hash,
                            detected_language=language.tag,
                            pinned_release_id=settings.knowledge_localization_release,
                            live_locales=set(settings.multilingual_live_locale_set),
                        )
                except Exception:
                    localization_error = True
                    logger.exception(
                        "approved localization lookup failed; forcing handoff",
                        extra={
                            "tenant_id": snapshot.tenant_id,
                            "document_id": str(selected.document_id),
                            "language": language.tag,
                        },
                    )

            forced_decision: ReplyDecision | None = deterministic_rule
            if should_retrieve and knowledge_result.error_code:
                forced_decision = ReplyDecision(
                    action=ReplyAction.HANDOFF,
                    reason_codes=(knowledge_result.error_code,),
                    source="rule",
                )
            elif should_retrieve and not language.is_known:
                forced_decision = ReplyDecision(
                    action=ReplyAction.HANDOFF,
                    reason_codes=("UNKNOWN_LANGUAGE",),
                    source="rule",
                )
            elif should_retrieve and not language_supported:
                forced_decision = ReplyDecision(
                    action=ReplyAction.HANDOFF,
                    reason_codes=("UNSUPPORTED_LANGUAGE",),
                    source="rule",
                )
            elif should_retrieve and (not assessment.strong or selected is None):
                forced_decision = ReplyDecision(
                    action=ReplyAction.HANDOFF,
                    reason_codes=("NO_STRONG_KNOWLEDGE_MATCH",),
                    source="rule",
                )
            elif (
                should_retrieve and experimental_multilingual and not experimental_source_is_english
            ):
                forced_decision = ReplyDecision(
                    action=ReplyAction.HANDOFF,
                    reason_codes=("EXPERIMENTAL_KNOWLEDGE_NOT_ENGLISH",),
                    source="rule",
                )
            elif should_retrieve and is_english_request and not settings.knowledge_verbatim_reply:
                forced_decision = ReplyDecision(
                    action=ReplyAction.HANDOFF,
                    reason_codes=("ENGLISH_CANONICAL_VERBATIM_DISABLED",),
                    source="rule",
                )
            elif should_retrieve and localization_error:
                forced_decision = ReplyDecision(
                    action=ReplyAction.HANDOFF,
                    reason_codes=("LOCALIZATION_LOOKUP_FAILED",),
                    source="rule",
                )
            elif (
                should_retrieve
                and not experimental_multilingual
                and not is_english_request
                and approved_localization is None
            ):
                forced_decision = ReplyDecision(
                    action=ReplyAction.HANDOFF,
                    reason_codes=("NO_APPROVED_LOCALIZATION",),
                    source="rule",
                )
            elif (
                not is_english_request
                and selected is not None
                and selected.is_official_contact
                and (
                    experimental_multilingual
                    or approved_localization is None
                    or not approved_localization.official_contact_authorized
                )
            ):
                forced_decision = ReplyDecision(
                    action=ReplyAction.HANDOFF,
                    reason_codes=("MULTILINGUAL_OFFICIAL_CONTACT_REVIEW",),
                    source="rule",
                )

            experimental_generate = (
                experimental_multilingual
                and not is_english_request
                and forced_decision is None
                and selected is not None
            )
            if experimental_multilingual and not is_english_request:
                prompt_contract_suffix = f"+{EXPERIMENTAL_MULTILINGUAL_CONTRACT_VERSION}"
            elif should_retrieve and not experimental_multilingual:
                prompt_contract_suffix = f"+{_MULTILINGUAL_CONTRACT_VERSION}"

            if experimental_generate:
                try:
                    async with get_session_factory()() as session:
                        persona = await load_persona(session, snapshot.tenant_id, snapshot.brand_id)
                    decision = await generate_experimental_multilingual_reply(
                        snapshot,
                        selected=selected,
                        target_language=language.tag,
                        history=history,
                        killswitch=killswitch,
                        llm=_get_llm(),
                        voice_preferences=persona.preferences,
                        email_auto_reply_allowed=(
                            snapshot.platform != "email"
                            or (settings.email_enabled and settings.email_auto_reply_enabled)
                        ),
                    )
                except Exception:
                    logger.exception("experimental multilingual runtime failed; forcing handoff")
                    decision = ReplyDecision(
                        action=ReplyAction.HANDOFF,
                        reason_codes=("EXPERIMENTAL_RUNTIME_FAILED",),
                        source="rule",
                        resolved_locale=language.tag,
                        multilingual_contract_version=(EXPERIMENTAL_MULTILINGUAL_CONTRACT_VERSION),
                    )
            else:
                decision = await run_decision_pipeline(
                    snapshot,
                    llm=None,
                    killswitch=killswitch,
                    approved_localization=(
                        approved_localization if forced_decision is None else None
                    ),
                    verbatim_reply=(
                        selected.reply
                        if (
                            forced_decision is None
                            and is_english_request
                            and selected is not None
                            and selected.verbatim_safe
                        )
                        else None
                    ),
                    approved_official_contact_reply=(
                        selected.reply
                        if (
                            forced_decision is None
                            and is_english_request
                            and selected is not None
                            and selected.is_official_contact
                        )
                        else None
                    ),
                    forced_decision=forced_decision,
                    target_language=(
                        approved_localization.locale
                        if approved_localization is not None
                        else (language.tag if should_retrieve else "und")
                    ),
                    apply_legacy_rules=False,
                    history=history,
                    email_auto_reply_allowed=(
                        snapshot.platform != "email"
                        or (settings.email_enabled and settings.email_auto_reply_enabled)
                    ),
                )
            decision = replace(
                decision,
                request_language=language.tag if language_should_detect else "und",
                resolved_locale=(
                    approved_localization.locale
                    if approved_localization is not None
                    else (
                        language.tag
                        if experimental_multilingual and language.is_known
                        else ("en" if is_english_request and assessment.strong else "und")
                    )
                ),
                multilingual_contract_version=(
                    prompt_contract_suffix.lstrip("+").replace("+", "/")
                    if prompt_contract_suffix
                    else None
                ),
                request_language_confidence=(
                    language.confidence if language_should_detect else None
                ),
                request_language_source=(language.source if language_should_detect else None),
                knowledge_content_hash=(
                    selected.content_hash if gate_evaluated and selected is not None else None
                ),
                knowledge_similarity=(
                    selected.similarity if gate_evaluated and selected is not None else None
                ),
                knowledge_top2_content_hash=(
                    assessment.second.content_hash
                    if gate_evaluated and assessment.second is not None
                    else None
                ),
                knowledge_top2_similarity=(
                    assessment.second.similarity
                    if gate_evaluated and assessment.second is not None
                    else None
                ),
                knowledge_similarity_margin=(assessment.margin if gate_evaluated else None),
                knowledge_match_status=(assessment.status if gate_evaluated else None),
                knowledge_gate_version=(_MULTILINGUAL_GATE_VERSION if gate_evaluated else None),
                knowledge_min_similarity_threshold=(
                    gate_min_similarity if gate_evaluated else None
                ),
                knowledge_min_margin_threshold=(gate_min_margin if gate_evaluated else None),
            )
        else:
            knowledge_result = (
                await _fetch_knowledge(snapshot, verified_english_only=False)
                if should_retrieve
                else KnowledgeRetrievalResult()
            )
            hits = knowledge_result.hits
            if knowledge_result.error_code:
                legacy_forced_decision = ReplyDecision(
                    action=ReplyAction.HANDOFF,
                    reason_codes=(knowledge_result.error_code,),
                    source="rule",
                )
            elif knowledge_result.exact_ambiguous:
                legacy_forced_decision = ReplyDecision(
                    action=ReplyAction.HANDOFF,
                    reason_codes=("AMBIGUOUS_EXACT_KNOWLEDGE",),
                    source="rule",
                )
            else:
                legacy_forced_decision = None
            if (
                settings.multilingual_knowledge_shadow_enabled
                and shadow_should_retrieve
                and not knowledge_result.error_code
            ):
                shadow_result = await _fetch_knowledge(
                    snapshot,
                    verified_english_only=True,
                    query_embedding=knowledge_result.query_embedding,
                    force_enabled=True,
                )
                shadow_history = await _fetch_history(conversation_id, cutoff_seq)
                shadow_language = detect_customer_language(snapshot.text, shadow_history)
                shadow_assessment = _assess_knowledge_match(
                    shadow_result,
                    min_similarity=settings.knowledge_auto_reply_min_similarity,
                    min_margin=settings.knowledge_auto_reply_min_margin,
                )
                logger.info(
                    "multilingual knowledge shadow: conversation=%s language=%s strong=%s "
                    "top1_hash=%s top1_similarity=%s margin=%s",
                    snapshot.conversation_key,
                    shadow_language.tag,
                    shadow_assessment.strong,
                    (
                        shadow_assessment.selected.content_hash[:12]
                        if shadow_assessment.selected is not None
                        else None
                    ),
                    (
                        round(shadow_assessment.selected.similarity, 4)
                        if shadow_assessment.selected is not None
                        else None
                    ),
                    (
                        round(shadow_assessment.margin, 4)
                        if shadow_assessment.margin is not None
                        else None
                    ),
                )
            selected_verbatim_hit: KnowledgeHit | None = None
            if hits and settings.knowledge_verbatim_reply and legacy_forced_decision is None:
                top_by_similarity = max(hits, key=lambda hit: hit.similarity)
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
                should_retrieve
                and legacy_forced_decision is None
                and verbatim is None
                and not (require_knowledge and not hits)
            )
            history = await _fetch_history(conversation_id, cutoff_seq) if needs_llm_history else ()
            if needs_llm_history:
                async with get_session_factory()() as session:
                    persona = await load_persona(session, snapshot.tenant_id, snapshot.brand_id)
            decision = await run_decision_pipeline(
                snapshot,
                llm=_get_llm() if needs_llm_history else None,
                killswitch=killswitch,
                knowledge=tuple(hit.content for hit in hits),
                require_knowledge=require_knowledge,
                verbatim_reply=verbatim,
                approved_official_contact_reply=approved_official_contact_reply,
                forced_decision=legacy_forced_decision,
                history=history,
                voice_preferences=persona.preferences,
                email_auto_reply_allowed=(
                    snapshot.platform != "email"
                    or (settings.email_enabled and settings.email_auto_reply_enabled)
                ),
            )
            if (
                settings.multilingual_knowledge_shadow_enabled
                and shadow_should_retrieve
                and not knowledge_result.error_code
            ):
                shadow_selected = shadow_assessment.selected
                decision = replace(
                    decision,
                    multilingual_shadow=True,
                    multilingual_shadow_evidence={
                        "contract_version": _MULTILINGUAL_CONTRACT_VERSION,
                        "renderer_version": "reviewed-localization-v1",
                        "localization_release": settings.knowledge_localization_release,
                        "gate_version": _MULTILINGUAL_GATE_VERSION,
                        "corpus_version": settings.knowledge_corpus_version,
                        "embedding_version": shadow_result.embedding_version,
                        "retrieval_mode": shadow_result.retrieval_mode,
                        "error_code": shadow_result.error_code,
                        "language": {
                            "tag": shadow_language.tag,
                            "confidence": shadow_language.confidence,
                            "source": shadow_language.source,
                        },
                        "top1": (
                            {
                                "content_hash": shadow_selected.content_hash,
                                "question": shadow_selected.question,
                                "approved_reply": shadow_selected.reply,
                                "similarity": shadow_selected.similarity,
                            }
                            if shadow_selected is not None
                            else None
                        ),
                        "top2": (
                            {
                                "content_hash": shadow_assessment.second.content_hash,
                                "question": shadow_assessment.second.question,
                                "approved_reply": shadow_assessment.second.reply,
                                "similarity": shadow_assessment.second.similarity,
                            }
                            if shadow_assessment.second is not None
                            else None
                        ),
                        "margin": shadow_assessment.margin,
                        "match_status": shadow_assessment.status,
                        "min_similarity": settings.knowledge_auto_reply_min_similarity,
                        "min_margin": settings.knowledge_auto_reply_min_margin,
                    },
                )
    handoff_notification_ids: list[uuid.UUID] = []
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
            prompt_version_label(settings.prompt_version, persona) + prompt_contract_suffix,
            decision_job_id=decision_job_id,
            decision_generation=decision_generation,
            decision_claim_token=claim_token,
            handoff_notification_ids=handoff_notification_ids,
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
    if settings.feishu_handoff_notifications_enabled:
        from social_reply.application.handoff_notifications.sender import (
            dispatch_handoff_notification,
        )

        for notification_id in handoff_notification_ids:
            try:
                await dispatch_handoff_notification(notification_id)
            except Exception:  # noqa: BLE001 - Scheduler recovers the durable PENDING intent
                logger.exception(
                    "Feishu handoff notification dispatch failed intent_id=%s",
                    notification_id,
                )
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
