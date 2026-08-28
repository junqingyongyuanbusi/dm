from dataclasses import replace
from uuid import UUID

import pytest

from social_reply.application.knowledge.retrieval import KnowledgeHit, KnowledgeRetrievalResult
from social_reply.application.reply_decision import runner
from social_reply.application.reply_decision.pipeline import DecisionSnapshot
from social_reply.application.reply_decision.rag_selection import (
    MATCH_ONLY_AMBIGUITY_RESOLUTION_METHOD,
    RAGResolutionEvidence,
    build_rag_candidates,
    build_ranked_rag_candidates,
    rag_evidence,
    selector_is_enabled,
    stable_canary_bucket,
)
from social_reply.domain.reply.decision import ReplyAction, ReplyDecision, Visibility
from social_reply.domain.reply.llm import RAGSelectionResult


def _hit(
    index: int,
    reply: str,
    similarity: float,
    *,
    protected_values: tuple[str, ...] = (),
) -> KnowledgeHit:
    return KnowledgeHit(
        content=f"private body {index}",
        question=f"private question {index}",
        reply=reply,
        similarity=similarity,
        document_id=UUID(int=index),
        chunk_id=UUID(int=100 + index),
        content_hash=f"{index:064x}",
        protected_values=protected_values,
    )


def _snapshot() -> DecisionSnapshot:
    return DecisionSnapshot(
        text="refund timing",
        platform="telegram",
        tenant_id="tenant-a",
        brand_id="brand-a",
        account_id="account-a",
        conversation_key="conversation-a",
        automation_state="BOT_ACTIVE",
        state_version=1,
    )


def test_candidates_dedupe_canonical_answers_and_keep_best_similarity():
    first = _hit(1, "Three business days.", 0.72)
    duplicate = replace(_hit(2, "  three   BUSINESS days. ", 0.91), content_hash="b" * 64)
    other = _hit(3, "Contact support.", 0.80)
    result = KnowledgeRetrievalResult(
        hits=(first, other),
        vector_hits=(duplicate, other),
    )

    candidates = build_rag_candidates(result)

    assert len(candidates) == 2
    assert candidates[0].hit.similarity == 0.91
    assert candidates[0].content_hashes == (first.content_hash, duplicate.content_hash)
    assert candidates[0].hybrid_rank == 1
    assert candidates[0].vector_rank == 1


def test_candidates_keep_separate_protected_value_policies():
    reply = "Use Acme Portal with MT4."
    result = KnowledgeRetrievalResult(
        hits=(
            _hit(1, reply, 0.91, protected_values=("Acme Portal",)),
            _hit(2, reply, 0.90, protected_values=("MT4",)),
        ),
        vector_hits=(),
    )

    candidates = build_rag_candidates(result)

    assert len(candidates) == 2
    assert candidates[0].answer_hash != candidates[1].answer_hash


def test_candidates_preserve_top_result_from_each_retrieval_arm():
    hybrid_hits = tuple(
        _hit(index, f"Hybrid answer {index}.", similarity)
        for index, similarity in ((1, 0.30), (2, 0.20), (3, 0.10))
    )
    vector_hits = tuple(
        _hit(index, f"Vector answer {index}.", similarity)
        for index, similarity in ((4, 0.95), (5, 0.90), (6, 0.85))
    )

    candidates = build_rag_candidates(
        KnowledgeRetrievalResult(hits=hybrid_hits, vector_hits=vector_hits)
    )

    assert len(candidates) == 3
    assert candidates[0].hit.chunk_id == hybrid_hits[0].chunk_id
    assert candidates[1].hit.chunk_id == vector_hits[0].chunk_id
    assert {candidate.hit.chunk_id for candidate in candidates} >= {
        hybrid_hits[0].chunk_id,
        vector_hits[0].chunk_id,
    }


def test_canary_bucket_is_stable_and_bounded():
    assert stable_canary_bucket("tenant:message") == stable_canary_bucket("tenant:message")
    assert 0 <= stable_canary_bucket("tenant:message") < 10000
    enabled, bucket = selector_is_enabled(mode="live", canary_bps=10000, key="key")
    assert enabled is True
    assert 0 <= bucket < 10000
    assert selector_is_enabled(mode="off", canary_bps=10000, key="key")[0] is False


def test_evidence_never_contains_query_or_candidate_bodies():
    candidates = build_rag_candidates(
        KnowledgeRetrievalResult(
            hits=(
                _hit(1, "secret answer", 0.9),
                _hit(2, "different secret answer", 0.8),
            ),
            vector_hits=(),
        )
    )
    evidence = rag_evidence(
        candidates=candidates,
        mode="live",
        canary_bucket=1,
        selection_method="selector_live",
        selected_candidate_id="candidate-1",
        selector_candidate_id="candidate-2",
        selector_version="selector-v1",
        selector_latency_ms=12.3456,
        retrieval_mode="vector_hybrid",
        embedding_version="embedding-v1",
    )
    serialized = repr(evidence)
    assert "private body" not in serialized
    assert "private question" not in serialized
    assert "secret answer" not in serialized
    assert evidence["selected_content_hash"] == f"{1:064x}"
    assert evidence["selector_content_hash"] == f"{2:064x}"
    assert evidence["selector_answer_hash"] != evidence["selected_answer_hash"]


def test_ranked_ambiguity_candidates_and_v2_evidence_share_stable_ids():
    top1 = _hit(1, "First approved answer.", 0.90)
    top2 = _hit(2, "Second approved answer.", 0.86)
    result = KnowledgeRetrievalResult(
        hits=(top2, top1),
        vector_hits=(top1, top2),
        retrieval_mode="vector_hybrid",
        embedding_version="embedding-v1",
    )
    candidates = build_ranked_rag_candidates(
        result,
        ranked_hits=(top1, top2),
    )

    assert [candidate.candidate_id for candidate in candidates] == [
        "candidate-1",
        "candidate-2",
    ]
    assert [candidate.hit.similarity for candidate in candidates] == [0.90, 0.86]
    assert [
        candidate.to_llm_candidate().approved_answer for candidate in candidates
    ] == [top1.reply, top2.reply]

    evidence = rag_evidence(
        candidates=candidates,
        mode="off",
        canary_bucket=0,
        selection_method=MATCH_ONLY_AMBIGUITY_RESOLUTION_METHOD,
        selected_candidate_id="candidate-1",
        selector_candidate_id=None,
        selector_version="resolver-v1",
        selector_latency_ms=12.5,
        retrieval_mode=result.retrieval_mode,
        embedding_version=result.embedding_version,
        resolution=RAGResolutionEvidence(
            outcome="answer",
            used_candidate_ids=("candidate-1", "candidate-2"),
            version="resolver-v1",
            latency_ms=12.5,
        ),
    )

    assert evidence["schema_version"] == "rag-evidence-v2"
    assert [candidate["candidate_id"] for candidate in evidence["candidates"]] == [
        "candidate-1",
        "candidate-2",
    ]
    assert evidence["resolution"]["used_candidate_ids"] == [
        "candidate-1",
        "candidate-2",
    ]
    serialized = repr(evidence)
    assert top1.reply not in serialized
    assert top2.reply not in serialized
    assert top1.question not in serialized
    assert top2.question not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["shadow", "live"])
async def test_selector_assessment_records_bounded_choice(monkeypatch, mode):
    class _Selector:
        rag_selector_id = "selector-test-v1"

        def __init__(self):
            self.candidate_orders = []

        async def select_rag_answer(self, **kwargs):
            assert len(kwargs["candidates"]) == 2
            self.candidate_orders.append(
                tuple(candidate.candidate_id for candidate in kwargs["candidates"])
            )
            return RAGSelectionResult(
                selected_candidate_id="candidate-2",
                directly_answers=True,
            )

    selector = _Selector()
    monkeypatch.setattr(runner, "_get_llm_or_none", lambda: selector)
    result = KnowledgeRetrievalResult(
        hits=(
            _hit(1, "First approved answer.", 0.91),
            _hit(2, "Second approved answer.", 0.88),
        ),
        vector_hits=(),
    )

    assessment = await runner._assess_with_rag_selector(
        _snapshot(),
        result,
        mode=mode,
        canary_bps=10000,
    )

    assert assessment.enabled is True
    assert assessment.selected is not None
    assert assessment.selected.candidate_id == "candidate-2"
    assert not hasattr(assessment, "reply_text")
    assert assessment.selector_version == "selector-test-v1"
    assert assessment.method == f"selector_{mode}"
    assert selector.candidate_orders == [
        ("candidate-1", "candidate-2"),
        ("candidate-2", "candidate-1"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "results",
    [
        (
            RAGSelectionResult(selected_candidate_id="candidate-1", directly_answers=True),
            RAGSelectionResult(selected_candidate_id="candidate-2", directly_answers=True),
        ),
        (
            RAGSelectionResult(selected_candidate_id="candidate-1", directly_answers=True),
            RAGSelectionResult(selected_candidate_id=None),
        ),
        (
            RAGSelectionResult(
                selected_candidate_id="candidate-1",
                directly_answers=True,
                has_conflict=True,
            ),
            RAGSelectionResult(selected_candidate_id="candidate-1", directly_answers=True),
        ),
    ],
)
async def test_selector_requires_two_safe_matching_choices(monkeypatch, results):
    class _Selector:
        rag_selector_id = "selector-test-v2"

        def __init__(self):
            self.results = iter(results)

        async def select_rag_answer(self, **kwargs):
            return next(self.results)

    monkeypatch.setattr(runner, "_get_llm_or_none", lambda: _Selector())
    result = KnowledgeRetrievalResult(
        hits=(
            _hit(1, "First approved answer.", 0.91),
            _hit(2, "Second approved answer.", 0.88),
        ),
        vector_hits=(),
    )

    assessment = await runner._assess_with_rag_selector(
        _snapshot(),
        result,
        mode="live",
        canary_bps=10000,
    )

    assert assessment.enabled is True
    assert assessment.selected is None
    assert assessment.method == "selector_live_abstain"


@pytest.mark.asyncio
async def test_ineligible_match_records_candidates_without_calling_selector(monkeypatch):
    monkeypatch.setattr(
        runner,
        "_get_llm_or_none",
        lambda: pytest.fail("selector must only run for low-margin ambiguous matches"),
    )
    result = KnowledgeRetrievalResult(
        hits=(_hit(1, "Approved answer.", 0.91),),
        vector_hits=(_hit(1, "Approved answer.", 0.91),),
    )

    assessment = await runner._assess_with_rag_selector(
        _snapshot(),
        result,
        mode="live",
        canary_bps=10000,
        eligible=False,
    )

    assert assessment.enabled is False
    assert len(assessment.candidates) == 1
    assert assessment.method == "legacy_top1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "canary_bps", "expected_method"),
    [("off", 10000, "legacy_top1"), ("live", 0, "selector_canary_off")],
)
async def test_selector_off_or_outside_canary_never_calls_model(
    monkeypatch,
    mode,
    canary_bps,
    expected_method,
):
    monkeypatch.setattr(
        runner,
        "_get_llm_or_none",
        lambda: pytest.fail("selector model must not be constructed"),
    )
    result = KnowledgeRetrievalResult(
        hits=(_hit(1, "Approved answer.", 0.91),),
        vector_hits=(),
    )

    assessment = await runner._assess_with_rag_selector(
        _snapshot(),
        result,
        mode=mode,
        canary_bps=canary_bps,
    )

    assert assessment.enabled is False
    assert assessment.selected is None
    assert assessment.method == expected_method


@pytest.mark.asyncio
async def test_exact_match_bypasses_selector_model(monkeypatch):
    monkeypatch.setattr(
        runner,
        "_get_llm_or_none",
        lambda: pytest.fail("exact matches must bypass the selector"),
    )
    hit = _hit(1, "Approved exact answer.", 1.0)

    assessment = await runner._assess_with_rag_selector(
        _snapshot(),
        KnowledgeRetrievalResult(
            hits=(hit,),
            vector_hits=(hit,),
            exact_match=True,
        ),
        mode="live",
        canary_bps=10000,
    )

    assert assessment.enabled is False
    assert assessment.selected is not None
    assert assessment.method == "exact"


@pytest.mark.asyncio
async def test_ambiguous_exact_match_fails_closed_without_selector(monkeypatch):
    monkeypatch.setattr(
        runner,
        "_get_llm_or_none",
        lambda: pytest.fail("conflicting exact answers must not be model-arbitrated"),
    )
    first = _hit(1, "First conflicting answer.", 1.0)
    second = _hit(2, "Second conflicting answer.", 1.0)

    assessment = await runner._assess_with_rag_selector(
        _snapshot(),
        KnowledgeRetrievalResult(
            hits=(first, second),
            vector_hits=(first, second),
            exact_ambiguous=True,
        ),
        mode="live",
        canary_bps=10000,
    )

    assert assessment.enabled is False
    assert assessment.selected is None
    assert assessment.method == "exact_ambiguous"


@pytest.mark.asyncio
async def test_non_exact_selector_never_receives_official_contact_candidate(monkeypatch):
    official = replace(
        _hit(1, "Official support: support@example.com", 0.99),
        is_official_contact=True,
    )
    ordinary = _hit(2, "Open a support ticket.", 0.88)

    class _Selector:
        async def select_rag_answer(self, **kwargs):
            assert [candidate.approved_answer for candidate in kwargs["candidates"]] == [
                ordinary.reply
            ]
            return RAGSelectionResult(
                selected_candidate_id=kwargs["candidates"][0].candidate_id,
                directly_answers=True,
            )

    monkeypatch.setattr(runner, "_get_llm_or_none", lambda: _Selector())
    assessment = await runner._assess_with_rag_selector(
        _snapshot(),
        KnowledgeRetrievalResult(
            hits=(official, ordinary),
            vector_hits=(official, ordinary),
        ),
        mode="live",
        canary_bps=10000,
    )

    assert assessment.enabled is True
    assert [candidate.hit for candidate in assessment.candidates] == [ordinary]
    assert assessment.selected is not None
    assert assessment.selected.hit == ordinary


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["shadow", "live"])
async def test_contact_only_non_exact_flow_never_invokes_selector(monkeypatch, mode):
    official = replace(
        _hit(1, "Official support: support@example.com", 0.99),
        is_official_contact=True,
    )
    monkeypatch.setattr(
        runner,
        "_get_llm_or_none",
        lambda: pytest.fail("approximate official contact must not reach the selector"),
    )

    assessment = await runner._assess_with_rag_selector(
        _snapshot(),
        KnowledgeRetrievalResult(hits=(official,), vector_hits=(official,)),
        mode=mode,
        canary_bps=10000,
    )

    assert assessment.enabled is False
    assert assessment.candidates == ()
    assert assessment.selected is None
    assert assessment.method == "official_contact_review"


@pytest.mark.asyncio
async def test_unambiguous_exact_official_contact_bypasses_selector(monkeypatch):
    official = replace(
        _hit(1, "Official support: support@example.com", 1.0),
        is_official_contact=True,
    )
    monkeypatch.setattr(
        runner,
        "_get_llm_or_none",
        lambda: pytest.fail("exact official contact must bypass the selector"),
    )

    assessment = await runner._assess_with_rag_selector(
        _snapshot(),
        KnowledgeRetrievalResult(
            hits=(official,),
            vector_hits=(official,),
            exact_match=True,
        ),
        mode="shadow",
        canary_bps=10000,
    )

    assert assessment.enabled is False
    assert assessment.selected is not None
    assert assessment.selected.hit == official
    assert assessment.method == "exact"


@pytest.mark.parametrize("initial_action", [ReplyAction.AUTO_REPLY, ReplyAction.DRAFT])
def test_unresolved_language_review_keeps_mirror_user_out_of_locale_fields(initial_action):
    decision = ReplyDecision(
        action=initial_action,
        reply_text="A reviewable reply",
        reply_visibility=Visibility.PUBLIC,
        reply_language="mirror-user",
        resolved_locale="mirror-user",
    )

    reviewed = runner._preserve_unresolved_language_review(decision)

    assert reviewed.action is ReplyAction.DRAFT
    assert reviewed.reply_visibility is Visibility.PRIVATE
    assert reviewed.reply_language == "und"
    assert reviewed.resolved_locale == "und"
    assert reviewed.reason_codes == ("UNKNOWN_LANGUAGE_REVIEW",)
