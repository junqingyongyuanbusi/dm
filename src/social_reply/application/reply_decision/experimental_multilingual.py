import json
import logging
from dataclasses import replace

from social_reply.application.knowledge.retrieval import KnowledgeHit
from social_reply.application.reply_decision.pipeline import DecisionSnapshot, run_decision_pipeline
from social_reply.domain.reply.decision import ReplyAction, ReplyDecision
from social_reply.domain.reply.llm import LLMClient
from social_reply.domain.reply.voice import VoicePreferences

logger = logging.getLogger(__name__)

EXPERIMENTAL_MULTILINGUAL_CONTRACT_VERSION = "multilingual-experimental-runtime-v1"


def _knowledge_evidence(hit: KnowledgeHit) -> str:
    return json.dumps(
        {"question": hit.question, "approved_answer": hit.reply},
        ensure_ascii=False,
        separators=(",", ":"),
    )


async def generate_experimental_multilingual_reply(
    snapshot: DecisionSnapshot,
    *,
    selected: KnowledgeHit,
    target_language: str,
    history: tuple[tuple[str, str], ...],
    killswitch,
    llm: LLMClient,
    voice_preferences: VoicePreferences,
    email_auto_reply_allowed: bool,
) -> ReplyDecision:
    """Generate a guarded same-language reply for an explicitly allowlisted test account.

    This path intentionally uses the existing published corpus even when it has not completed the
    verified-English review lifecycle. It is test-only provenance, never proof of a reviewed
    localization artifact.
    """
    try:
        decision = await run_decision_pipeline(
            snapshot,
            llm=llm,
            killswitch=killswitch,
            knowledge=(_knowledge_evidence(selected),),
            require_knowledge=False,
            approved_knowledge_reply=selected.reply,
            target_language=target_language,
            apply_legacy_rules=False,
            history=history,
            voice_preferences=voice_preferences,
            email_auto_reply_allowed=email_auto_reply_allowed,
        )
    except Exception:
        logger.exception("experimental multilingual generation failed; forcing handoff")
        decision = ReplyDecision(
            action=ReplyAction.HANDOFF,
            reason_codes=("EXPERIMENTAL_LLM_FAILED",),
            source="rule",
        )
    reason_codes = (
        ("EXPERIMENTAL_UNVERIFIED_CORPUS", *decision.reason_codes)
        if decision.action is ReplyAction.HANDOFF
        else (*decision.reason_codes, "EXPERIMENTAL_UNVERIFIED_CORPUS")
    )
    return replace(
        decision,
        resolved_locale=target_language,
        multilingual_contract_version=EXPERIMENTAL_MULTILINGUAL_CONTRACT_VERSION,
        reason_codes=reason_codes,
    )
