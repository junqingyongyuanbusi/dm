"""多语言运行时生成路径（live 模式的非英语默认路径）。

英语知识库是唯一事实源：命中发布后，把英语 question/approved_answer 作为证据交给
LLM，由 contract 强制 target_language 输出；管线内置语言守卫与 grounding verifier。
"""

import json
import logging
import time
from dataclasses import dataclass, replace

from social_reply.application.knowledge.retrieval import KnowledgeHit
from social_reply.application.reply_decision.pipeline import DecisionSnapshot, run_decision_pipeline
from social_reply.domain.reply.business_prompt import BusinessPromptInstructions
from social_reply.domain.reply.decision import ReplyAction, ReplyDecision
from social_reply.domain.reply.guard import LANGUAGE_VERIFICATION_STRICT, redact_pii
from social_reply.domain.reply.llm import (
    KnowledgeAmbiguityOutcome,
    KnowledgeAmbiguityResolution,
    LLMClient,
    LLMContext,
    RAGCandidate,
    normalize_knowledge_ambiguity_resolution,
)
from social_reply.domain.reply.voice import VoicePreferences

logger = logging.getLogger(__name__)

MULTILINGUAL_GENERATION_CONTRACT_VERSION = "multilingual-runtime-generation-v1"
KNOWLEDGE_MATCH_ONLY_CONTRACT_VERSION = "knowledge-match-only-reply-v1"
KNOWLEDGE_MATCH_AMBIGUITY_CONTRACT_VERSION = "knowledge-match-ambiguity-v1"
KNOWLEDGE_MATCH_AMBIGUITY_GATE_VERSION = "ambiguity-resolution-gate-v1"


@dataclass(frozen=True)
class KnowledgeAmbiguityReplyResult:
    decision: ReplyDecision
    outcome: KnowledgeAmbiguityOutcome
    used_candidate_ids: tuple[str, ...]
    resolver_version: str | None
    latency_ms: float


def _knowledge_evidence(hit: KnowledgeHit) -> str:
    return json.dumps(
        {"question": hit.question, "approved_answer": hit.reply},
        ensure_ascii=False,
        separators=(",", ":"),
    )


async def resolve_knowledge_ambiguity_reply(
    snapshot: DecisionSnapshot,
    *,
    candidates: tuple[RAGCandidate, ...],
    target_language: str,
    history: tuple[tuple[str, str], ...],
    killswitch,
    llm: LLMClient,
    voice_preferences: VoicePreferences,
    email_auto_reply_allowed: bool,
    business_prompt: BusinessPromptInstructions | None = None,
    fallback_reason_codes: tuple[str, ...] = (),
) -> KnowledgeAmbiguityReplyResult:
    allowed_candidate_ids = frozenset(
        candidate.candidate_id for candidate in candidates
    )
    raw_resolver_version = getattr(llm, "knowledge_ambiguity_resolver_id", None)
    resolver_version = (
        raw_resolver_version.strip()
        if isinstance(raw_resolver_version, str) and raw_resolver_version.strip()
        else None
    )
    started = time.perf_counter()
    try:
        resolver = getattr(llm, "resolve_knowledge_ambiguity", None)
        resolution = (
            await resolver(
                LLMContext(
                    text=redact_pii(snapshot.text or ""),
                    conversation_key=snapshot.conversation_key,
                    history=tuple(
                        (role, redact_pii(text))
                        for role, text in history
                        if role in {"user", "assistant"} and text
                    ),
                    voice_preferences=voice_preferences,
                    business_prompt=business_prompt,
                    target_language=target_language,
                ),
                candidates=candidates,
            )
            if resolver is not None
            and resolver_version is not None
            and len(candidates) == 2
            and len(allowed_candidate_ids) == 2
            else None
        )
    except Exception:
        logger.exception("knowledge ambiguity resolution failed; forcing handoff")
        resolution = None
    latency_ms = (time.perf_counter() - started) * 1000
    validated = (
        normalize_knowledge_ambiguity_resolution(
            resolution,
            allowed_candidate_ids=allowed_candidate_ids,
        )
        if resolution is not None
        else None
    )
    if validated is None:
        validated = KnowledgeAmbiguityResolution(
            outcome=KnowledgeAmbiguityOutcome.ABSTAIN,
            reply_text="",
            used_candidate_ids=(),
        )
        forced_decision = ReplyDecision(
            action=ReplyAction.HANDOFF,
            reason_codes=("KNOWLEDGE_AMBIGUITY_RESOLUTION_FAILED",),
            source="rule",
        )
    elif validated.outcome is KnowledgeAmbiguityOutcome.ABSTAIN:
        forced_decision = ReplyDecision(
            action=ReplyAction.HANDOFF,
            reason_codes=("KNOWLEDGE_AMBIGUITY_UNRESOLVED",),
            source="rule",
        )
    else:
        is_clarification = validated.outcome is KnowledgeAmbiguityOutcome.CLARIFY
        forced_decision = ReplyDecision(
            action=ReplyAction.AUTO_REPLY,
            reply_text=validated.reply_text,
            intent=(
                "knowledge_ambiguity_clarification"
                if is_clarification
                else "knowledge_multi_candidate_reply"
            ),
            confidence=1.0,
            reason_codes=(
                "KNOWLEDGE_AMBIGUITY_CLARIFICATION"
                if is_clarification
                else "KNOWLEDGE_MULTI_CANDIDATE_REPLY",
            ),
            source="knowledge",
            reply_language=target_language,
            resolved_locale=target_language,
        )
    decision = await run_decision_pipeline(
        snapshot,
        llm=None,
        killswitch=killswitch,
        forced_decision=forced_decision,
        target_language=target_language,
        apply_legacy_rules=False,
        history=history,
        voice_preferences=voice_preferences,
        business_prompt=business_prompt,
        email_auto_reply_allowed=email_auto_reply_allowed,
        knowledge_match_only_reply=True,
    )
    if decision.action is not ReplyAction.HANDOFF:
        decision = replace(
            decision,
            reason_codes=(*decision.reason_codes, *fallback_reason_codes),
        )
    return KnowledgeAmbiguityReplyResult(
        decision=replace(
            decision,
            resolved_locale=target_language,
            multilingual_contract_version=KNOWLEDGE_MATCH_AMBIGUITY_CONTRACT_VERSION,
        ),
        outcome=validated.outcome,
        used_candidate_ids=validated.used_candidate_ids,
        resolver_version=resolver_version,
        latency_ms=latency_ms,
    )


async def generate_multilingual_reply(
    snapshot: DecisionSnapshot,
    *,
    selected: KnowledgeHit,
    target_language: str,
    history: tuple[tuple[str, str], ...],
    killswitch,
    llm: LLMClient,
    voice_preferences: VoicePreferences,
    email_auto_reply_allowed: bool,
    business_prompt: BusinessPromptInstructions | None = None,
    fallback_reason_codes: tuple[str, ...] = (),
    language_verification: str = LANGUAGE_VERIFICATION_STRICT,
    language_policy: str = "review",
    approved_knowledge_protected_values: tuple[str, ...] = (),
    knowledge_match_only_reply: bool = False,
) -> ReplyDecision:
    """Generate a guarded same-language reply from the canonical English knowledge hit."""
    contract_version = (
        KNOWLEDGE_MATCH_ONLY_CONTRACT_VERSION
        if knowledge_match_only_reply
        else MULTILINGUAL_GENERATION_CONTRACT_VERSION
    )
    try:
        decision = await run_decision_pipeline(
            snapshot,
            llm=llm,
            killswitch=killswitch,
            knowledge=(_knowledge_evidence(selected),),
            require_knowledge=False,
            approved_knowledge_reply=selected.reply,
            approved_knowledge_protected_values=approved_knowledge_protected_values,
            target_language=target_language,
            apply_legacy_rules=False,
            history=history,
            voice_preferences=voice_preferences,
            business_prompt=business_prompt,
            email_auto_reply_allowed=email_auto_reply_allowed,
            language_verification=language_verification,
            language_policy=language_policy,
            knowledge_match_only_reply=knowledge_match_only_reply,
        )
    except Exception:
        logger.exception("multilingual generation failed; forcing handoff")
        return ReplyDecision(
            action=ReplyAction.HANDOFF,
            reason_codes=("MULTILINGUAL_GENERATION_FAILED",),
            source="rule",
            resolved_locale=target_language,
            multilingual_contract_version=contract_version,
        )
    if decision.action is ReplyAction.HANDOFF:
        return replace(
            decision,
            resolved_locale=target_language,
            multilingual_contract_version=contract_version,
        )
    return replace(
        decision,
        resolved_locale=target_language,
        multilingual_contract_version=contract_version,
        reason_codes=(
            *decision.reason_codes,
            *fallback_reason_codes,
            (
                "KNOWLEDGE_MATCH_ONLY_RUNTIME_GENERATION"
                if knowledge_match_only_reply
                else "MULTILINGUAL_RUNTIME_GENERATION"
            ),
        ),
    )
