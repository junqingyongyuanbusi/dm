"""多语言运行时生成路径（live 模式的非英语默认路径）。

英语知识库是唯一事实源：命中发布后，把英语 question/approved_answer 作为证据交给
LLM，由 contract 强制 target_language 输出；管线内置语言守卫与 grounding verifier。
"""

import json
import logging
from dataclasses import replace

from social_reply.application.knowledge.retrieval import KnowledgeHit
from social_reply.application.reply_decision.pipeline import DecisionSnapshot, run_decision_pipeline
from social_reply.domain.reply.decision import ReplyAction, ReplyDecision
from social_reply.domain.reply.llm import LLMClient
from social_reply.domain.reply.voice import VoicePreferences

logger = logging.getLogger(__name__)

MULTILINGUAL_GENERATION_CONTRACT_VERSION = "multilingual-runtime-generation-v1"


def _knowledge_evidence(hit: KnowledgeHit) -> str:
    return json.dumps(
        {"question": hit.question, "approved_answer": hit.reply},
        ensure_ascii=False,
        separators=(",", ":"),
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
    fallback_reason_codes: tuple[str, ...] = (),
) -> ReplyDecision:
    """Generate a guarded same-language reply from the canonical English knowledge hit."""
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
        logger.exception("multilingual generation failed; forcing handoff")
        return ReplyDecision(
            action=ReplyAction.HANDOFF,
            reason_codes=("MULTILINGUAL_GENERATION_FAILED",),
            source="rule",
            resolved_locale=target_language,
            multilingual_contract_version=MULTILINGUAL_GENERATION_CONTRACT_VERSION,
        )
    if decision.action is ReplyAction.HANDOFF:
        return replace(
            decision,
            resolved_locale=target_language,
            multilingual_contract_version=MULTILINGUAL_GENERATION_CONTRACT_VERSION,
        )
    return replace(
        decision,
        resolved_locale=target_language,
        multilingual_contract_version=MULTILINGUAL_GENERATION_CONTRACT_VERSION,
        reason_codes=(
            *decision.reason_codes,
            *fallback_reason_codes,
            "MULTILINGUAL_RUNTIME_GENERATION",
        ),
    )
