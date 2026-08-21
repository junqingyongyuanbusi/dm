import uuid

import pytest

from social_reply.application.knowledge.retrieval import KnowledgeHit, KnowledgeRetrievalResult
from social_reply.application.reply_decision.runner import _assess_answer_match


def _hit(*, reply: str, similarity: float, document_id: uuid.UUID | None = None) -> KnowledgeHit:
    return KnowledgeHit(
        content=reply,
        reply=reply,
        question="How long does it take?",
        similarity=similarity,
        document_id=document_id or uuid.uuid4(),
        chunk_id=uuid.uuid4(),
        content_hash=uuid.uuid4().hex,
        source_language="en",
        language_verified=True,
    )


def test_same_approved_answer_from_duplicate_documents_does_not_reduce_margin() -> None:
    result = KnowledgeRetrievalResult(
        vector_hits=(
            _hit(reply="Refunds take 3 to 5 business days.", similarity=0.91),
            _hit(reply="  refunds take 3 to 5 business days. ", similarity=0.84),
            _hit(reply="Verification takes 7 business days.", similarity=0.83),
        )
    )

    assessment = _assess_answer_match(result, min_similarity=0.8, min_margin=0.05)

    assert assessment.strong is True
    assert assessment.selected is not None
    assert assessment.selected.reply.startswith("Refunds")
    assert assessment.second is not None
    assert assessment.second.reply == "Verification takes 7 business days."
    assert assessment.margin == pytest.approx(0.08)


def test_different_approved_answers_remain_ambiguous() -> None:
    result = KnowledgeRetrievalResult(
        vector_hits=(
            _hit(reply="Refunds take 3 to 5 business days.", similarity=0.91),
            _hit(reply="Verification takes 7 business days.", similarity=0.88),
        )
    )

    assessment = _assess_answer_match(result, min_similarity=0.8, min_margin=0.05)

    assert assessment.strong is False
    assert assessment.status == "ambiguous"
