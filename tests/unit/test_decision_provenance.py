import math

import pytest

from social_reply.application.reply_decision.persist import _rag_evidence


def _valid_evidence() -> dict:
    return {
        "schema_version": "rag-evidence-v1",
        "retrieval_policy_version": "hybrid-union-selector-v2",
        "retrieval_mode": "vector_hybrid",
        "embedding_version": "embedding-v1",
        "selector_mode": "shadow",
        "canary_bucket": 42,
        "selection_method": "selector_shadow",
        "selector_version": "selector-v1",
        "selector_latency_ms": 12.5,
        "selected_answer_hash": "a" * 64,
        "selected_content_hash": "b" * 64,
        "selector_answer_hash": "c" * 64,
        "selector_content_hash": "d" * 64,
        "candidates": [
            {
                "answer_hash": "a" * 64,
                "content_hashes": ["b" * 64],
                "similarity": 0.91,
                "hybrid_rank": 1,
                "vector_rank": 2,
                "arms": {"translated_hybrid_rank": 1},
            }
        ],
        "guard": {"reason_codes": ["KNOWLEDGE_HIT"]},
        "verifier": {
            "relevant": True,
            "faithful": True,
            "version": "rag-verifier-v2:model",
            "latency_ms": 9.5,
        },
    }


def test_rag_evidence_accepts_only_the_versioned_metadata_shape():
    assert _rag_evidence(_valid_evidence()) == _valid_evidence()


def test_rag_evidence_accepts_official_contact_review_method():
    evidence = _valid_evidence()
    evidence.update(
        selection_method="official_contact_review",
        selector_version=None,
        selector_latency_ms=None,
        selected_answer_hash=None,
        selected_content_hash=None,
        selector_answer_hash=None,
        selector_content_hash=None,
        candidates=[],
        verifier=None,
    )

    assert _rag_evidence(evidence) == evidence


@pytest.mark.parametrize(
    ("container", "key"),
    [
        ("top", "details"),
        ("candidate", "approved_answer"),
        ("guard", "raw"),
        ("verifier", "candidate_reply"),
        ("arms", "query_rank"),
    ],
)
def test_rag_evidence_rejects_unversioned_or_body_capable_keys(container, key):
    evidence = _valid_evidence()
    target = {
        "top": evidence,
        "candidate": evidence["candidates"][0],
        "guard": evidence["guard"],
        "verifier": evidence["verifier"],
        "arms": evidence["candidates"][0]["arms"],
    }[container]
    target[key] = "customer body"

    with pytest.raises(ValueError, match="rag_evidence"):
        _rag_evidence(evidence)


def test_rag_evidence_rejects_non_finite_numbers():
    evidence = _valid_evidence()
    evidence["selector_latency_ms"] = math.nan

    with pytest.raises(ValueError, match="rag_evidence_number_invalid"):
        _rag_evidence(evidence)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("retrieval_mode",), "customer body"),
        (("selected_answer_hash",), "customer body"),
        (("candidates", 0, "answer_hash"), "customer body"),
        (("candidates", 0, "content_hashes"), ["customer body"]),
        (("candidates", 0, "arms", "translated_hybrid_rank"), "customer body"),
        (("guard", "reason_codes"), ["customer body"]),
        (("verifier", "version"), "customer body"),
    ],
)
def test_rag_evidence_rejects_bodies_hidden_in_allowed_values(path, value):
    evidence = _valid_evidence()
    target = evidence
    for segment in path[:-1]:
        target = target[segment]
    target[path[-1]] = value

    with pytest.raises(ValueError, match="rag_evidence"):
        _rag_evidence(evidence)
