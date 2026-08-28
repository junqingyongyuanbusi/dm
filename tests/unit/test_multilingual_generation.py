import uuid

import pytest

from social_reply.application.knowledge.retrieval import KnowledgeHit
from social_reply.application.reply_decision.multilingual_generation import (
    KNOWLEDGE_MATCH_AMBIGUITY_CONTRACT_VERSION,
    KNOWLEDGE_MATCH_ONLY_CONTRACT_VERSION,
    MULTILINGUAL_GENERATION_CONTRACT_VERSION,
    generate_multilingual_reply,
    resolve_knowledge_ambiguity_reply,
)
from social_reply.application.reply_decision.pipeline import DecisionSnapshot
from social_reply.domain.reply.decision import ReplyAction, ReplyDecision, Visibility
from social_reply.domain.reply.llm import (
    KnowledgeAmbiguityOutcome,
    KnowledgeAmbiguityResolution,
    RAGCandidate,
)
from social_reply.domain.reply.voice import DEFAULT_VOICE_PREFERENCES


class _KillSwitch:
    async def is_disabled(self, *args) -> bool:
        return False


class _LLM:
    def __init__(self, reply: str, *, faithful: bool = True, raises: bool = False):
        self.reply = reply
        self.faithful = faithful
        self.raises = raises
        self.target_language = None

    async def decide(self, context):
        if self.raises:
            raise RuntimeError("provider unavailable")
        self.target_language = context.target_language
        return ReplyDecision(
            action=ReplyAction.AUTO_REPLY,
            reply_text=self.reply,
            confidence=0.99,
        )

    async def verify_grounding(self, **kwargs):
        return self.faithful


def _snapshot() -> DecisionSnapshot:
    return DecisionSnapshot(
        text="返金はいつ反映されますか？",
        platform="telegram",
        tenant_id="default",
        brand_id="b1",
        account_id=str(uuid.uuid4()),
        conversation_key="telegram:test",
        automation_state="BOT_ACTIVE",
        state_version=1,
    )


def _hit() -> KnowledgeHit:
    return KnowledgeHit(
        content=(
            "Question: How long does a refund take?\n"
            "Approved answer: Refunds take 3 to 5 business days."
        ),
        question="How long does a refund take?",
        reply="Refunds take 3 to 5 business days.",
        similarity=0.95,
        document_id=uuid.uuid4(),
        chunk_id=uuid.uuid4(),
        content_hash="a" * 64,
        source_language="en",
        language_verified=True,
    )


def _second_hit() -> KnowledgeHit:
    return KnowledgeHit(
        content=(
            "Question: How do I report an incorrect refund status?\n"
            "Approved answer: Submit a correction through the support form."
        ),
        question="How do I report an incorrect refund status?",
        reply="Submit a correction through the support form.",
        similarity=0.92,
        document_id=uuid.uuid4(),
        chunk_id=uuid.uuid4(),
        content_hash="b" * 64,
        source_language="en",
        language_verified=True,
    )


def _ambiguity_candidates() -> tuple[RAGCandidate, RAGCandidate]:
    first = _hit()
    second = _second_hit()
    return (
        RAGCandidate(
            candidate_id="candidate-1",
            question=first.question,
            approved_answer=first.reply,
            similarity=first.similarity,
        ),
        RAGCandidate(
            candidate_id="candidate-2",
            question=second.question,
            approved_answer=second.reply,
            similarity=second.similarity,
        ),
    )


@pytest.mark.asyncio
async def test_runtime_generation_returns_same_language_contract() -> None:
    llm = _LLM("返金には通常3〜5営業日かかります。")

    decision = await generate_multilingual_reply(
        _snapshot(),
        selected=_hit(),
        target_language="ja",
        history=(),
        killswitch=_KillSwitch(),
        llm=llm,
        voice_preferences=DEFAULT_VOICE_PREFERENCES,
        email_auto_reply_allowed=True,
    )

    assert decision.action is ReplyAction.AUTO_REPLY
    assert decision.reply_language == "ja"
    assert decision.resolved_locale == "ja"
    assert decision.grounding_verified is True
    assert decision.multilingual_contract_version == MULTILINGUAL_GENERATION_CONTRACT_VERSION
    assert llm.target_language == "ja"


@pytest.mark.asyncio
async def test_wrong_language_generation_becomes_private_draft() -> None:
    decision = await generate_multilingual_reply(
        _snapshot(),
        selected=_hit(),
        target_language="ja",
        history=(),
        killswitch=_KillSwitch(),
        llm=_LLM("Refunds take 3 to 5 business days."),
        voice_preferences=DEFAULT_VOICE_PREFERENCES,
        email_auto_reply_allowed=True,
    )

    assert decision.action is ReplyAction.DRAFT
    assert decision.reply_text == "Refunds take 3 to 5 business days."
    assert decision.reply_visibility is Visibility.PRIVATE
    assert "GUARD_LANGUAGE_MISMATCH" in decision.reason_codes


@pytest.mark.asyncio
async def test_generation_failure_handoffs_with_contract_provenance() -> None:
    decision = await generate_multilingual_reply(
        _snapshot(),
        selected=_hit(),
        target_language="ja",
        history=(),
        killswitch=_KillSwitch(),
        llm=_LLM("", raises=True),
        voice_preferences=DEFAULT_VOICE_PREFERENCES,
        email_auto_reply_allowed=True,
    )

    assert decision.action is ReplyAction.HANDOFF
    assert decision.reason_codes == ("MULTILINGUAL_GENERATION_FAILED",)
    assert decision.multilingual_contract_version == MULTILINGUAL_GENERATION_CONTRACT_VERSION


@pytest.mark.asyncio
async def test_match_only_generation_uses_text_contract_without_content_guards() -> None:
    class _MatchOnlyLLM:
        async def decide(self, context):
            raise AssertionError("the action decision contract must remain unused")

        async def generate_knowledge_reply_text(self, context):
            assert context.target_language == "ja"
            return "Email support@example.com. Refunds take 99 days."

        async def verify_grounding(self, **kwargs):
            raise AssertionError("grounding must remain disabled in match-only mode")

        async def detect_language_tag(self, text):
            raise AssertionError("reply-language observation must remain disabled")

    decision = await generate_multilingual_reply(
        _snapshot(),
        selected=_hit(),
        target_language="ja",
        history=(),
        killswitch=_KillSwitch(),
        llm=_MatchOnlyLLM(),
        voice_preferences=DEFAULT_VOICE_PREFERENCES,
        email_auto_reply_allowed=True,
        knowledge_match_only_reply=True,
    )

    assert decision.action is ReplyAction.AUTO_REPLY
    assert decision.reply_text == "Email support@example.com. Refunds take 99 days."
    assert decision.reply_language == "ja"
    assert decision.grounding_verified is None
    assert decision.multilingual_contract_version == KNOWLEDGE_MATCH_ONLY_CONTRACT_VERSION
    assert "KNOWLEDGE_MATCH_ONLY_RUNTIME_GENERATION" in decision.reason_codes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resolution", "expected_intent", "expected_reason"),
    [
        (
            KnowledgeAmbiguityResolution(
                outcome=KnowledgeAmbiguityOutcome.ANSWER,
                reply_text="返金には3〜5営業日かかり、誤りはフォームから報告できます。",
                used_candidate_ids=("candidate-1", "candidate-2"),
            ),
            "knowledge_multi_candidate_reply",
            "KNOWLEDGE_MULTI_CANDIDATE_REPLY",
        ),
        (
            KnowledgeAmbiguityResolution(
                outcome=KnowledgeAmbiguityOutcome.CLARIFY,
                reply_text="返金の所要時間と、誤った状態の報告方法のどちらですか？",
                used_candidate_ids=("candidate-1", "candidate-2"),
            ),
            "knowledge_ambiguity_clarification",
            "KNOWLEDGE_AMBIGUITY_CLARIFICATION",
        ),
    ],
    ids=["answer", "clarify"],
)
async def test_ambiguity_resolution_maps_bounded_outcomes_to_public_reply(
    resolution,
    expected_intent,
    expected_reason,
) -> None:
    class _AmbiguityLLM:
        knowledge_ambiguity_resolver_id = "resolver-test-v1"

        async def resolve_knowledge_ambiguity(self, context, *, candidates):
            assert context.target_language == "ja"
            assert [candidate.candidate_id for candidate in candidates] == [
                "candidate-1",
                "candidate-2",
            ]
            assert candidates[0].approved_answer == _hit().reply
            assert candidates[1].approved_answer == _second_hit().reply
            return resolution

    result = await resolve_knowledge_ambiguity_reply(
        _snapshot(),
        candidates=_ambiguity_candidates(),
        target_language="ja",
        history=(),
        killswitch=_KillSwitch(),
        llm=_AmbiguityLLM(),
        voice_preferences=DEFAULT_VOICE_PREFERENCES,
        email_auto_reply_allowed=True,
    )

    assert result.decision.action is ReplyAction.AUTO_REPLY
    assert result.decision.intent == expected_intent
    assert expected_reason in result.decision.reason_codes
    assert result.decision.reply_language == "ja"
    assert result.decision.multilingual_contract_version == (
        KNOWLEDGE_MATCH_AMBIGUITY_CONTRACT_VERSION
    )
    assert result.outcome is resolution.outcome
    assert result.used_candidate_ids == resolution.used_candidate_ids
    assert result.resolver_version == "resolver-test-v1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resolution",
    [
        KnowledgeAmbiguityResolution(
            outcome=KnowledgeAmbiguityOutcome.ABSTAIN,
            reply_text="",
            used_candidate_ids=(),
        ),
        KnowledgeAmbiguityResolution(
            outcome=KnowledgeAmbiguityOutcome.ANSWER,
            reply_text="",
            used_candidate_ids=("candidate-1",),
        ),
        KnowledgeAmbiguityResolution(
            outcome=KnowledgeAmbiguityOutcome.CLARIFY,
            reply_text="Which one?",
            used_candidate_ids=("candidate-1",),
        ),
    ],
    ids=["abstain", "blank-answer", "partial-clarify"],
)
async def test_ambiguity_resolution_failures_handoff(resolution) -> None:
    class _AmbiguityLLM:
        knowledge_ambiguity_resolver_id = "resolver-test-v1"

        async def resolve_knowledge_ambiguity(self, context, *, candidates):
            return resolution

    result = await resolve_knowledge_ambiguity_reply(
        _snapshot(),
        candidates=_ambiguity_candidates(),
        target_language="ja",
        history=(),
        killswitch=_KillSwitch(),
        llm=_AmbiguityLLM(),
        voice_preferences=DEFAULT_VOICE_PREFERENCES,
        email_auto_reply_allowed=True,
    )

    assert result.decision.action is ReplyAction.HANDOFF
    assert result.decision.reply_text is None
    assert result.decision.multilingual_contract_version == (
        KNOWLEDGE_MATCH_AMBIGUITY_CONTRACT_VERSION
    )
