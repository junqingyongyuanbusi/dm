"""Bounded RAG candidate selection and privacy-safe decision evidence."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from social_reply.application.knowledge.retrieval import (
    KnowledgeHit,
    KnowledgeRetrievalResult,
)
from social_reply.domain.knowledge.policy import (
    KnowledgeAnswerIdentity,
    canonical_answer_identity,
)
from social_reply.domain.reply.llm import RAGCandidate, RAGSelectionResult

logger = logging.getLogger(__name__)

RETRIEVAL_POLICY_VERSION = "hybrid-union-selector-v2"
RAG_EVIDENCE_VERSION = "rag-evidence-v1"
OFFICIAL_CONTACT_REVIEW_METHOD = "official_contact_review"
RAG_SELECTION_METHODS = frozenset(
    {
        "exact",
        "exact_ambiguous",
        "legacy_top1",
        "match_only_gate",
        OFFICIAL_CONTACT_REVIEW_METHOD,
        "selector_canary_off",
        "selector_live",
        "selector_live_abstain",
        "selector_shadow",
        "selector_shadow_abstain",
    }
)
MAX_SELECTOR_CANDIDATES = 3
MAX_EVIDENCE_CONTENT_HASHES = 8


@dataclass(frozen=True)
class RAGCandidateOption:
    candidate_id: str
    hit: KnowledgeHit
    answer_hash: str
    content_hashes: tuple[str, ...]
    hybrid_rank: int | None
    vector_rank: int | None
    arm_evidence: dict[str, Any]

    def to_llm_candidate(self) -> RAGCandidate:
        return RAGCandidate(
            candidate_id=self.candidate_id,
            question=self.hit.question,
            approved_answer=self.hit.reply,
            similarity=self.hit.similarity,
        )


@dataclass(frozen=True)
class RAGConsensusResult:
    selected_candidate_id: str | None
    forward_candidate_id: str | None
    reverse_candidate_id: str | None

    @property
    def disagreed(self) -> bool:
        return self.forward_candidate_id != self.reverse_candidate_id


def _safe_selected_candidate_id(result: RAGSelectionResult | None) -> str | None:
    if (
        result is None
        or result.selected_candidate_id is None
        or not result.directly_answers
        or result.requires_case_specific_data
        or result.has_conflict
    ):
        return None
    return result.selected_candidate_id


async def select_rag_answer_with_consensus(
    *,
    selector: Callable[..., Awaitable[RAGSelectionResult]],
    query: str,
    candidates: tuple[RAGCandidate, ...],
) -> RAGConsensusResult:
    """Run original and reversed candidate orders; accept only two matching safe choices."""
    selection_results: list[RAGSelectionResult | None] = []
    for ordered_candidates in (candidates, tuple(reversed(candidates))):
        try:
            selection_results.append(
                await selector(query=query, candidates=ordered_candidates)
            )
        except Exception:
            logger.exception("RAG selector pass failed; treating that pass as abstain")
            selection_results.append(None)
    forward_candidate_id, reverse_candidate_id = (
        _safe_selected_candidate_id(result) for result in selection_results
    )
    selected_candidate_id = (
        forward_candidate_id
        if forward_candidate_id is not None
        and forward_candidate_id == reverse_candidate_id
        else None
    )
    return RAGConsensusResult(
        selected_candidate_id=selected_candidate_id,
        forward_candidate_id=forward_candidate_id,
        reverse_candidate_id=reverse_candidate_id,
    )


def stable_canary_bucket(key: str) -> int:
    """Map a durable decision key into one of 10,000 stable rollout buckets."""
    digest = hashlib.sha256(key.encode()).digest()
    return int.from_bytes(digest[:8], "big") % 10000


def selector_is_enabled(*, mode: str, canary_bps: int, key: str) -> tuple[bool, int]:
    bucket = stable_canary_bucket(key)
    return mode in {"shadow", "live"} and bucket < canary_bps, bucket


def _answer_hash(identity: KnowledgeAnswerIdentity) -> str:
    normalized, is_official_contact, protected_values = identity
    value = repr((normalized, is_official_contact, protected_values))
    return hashlib.sha256(value.encode()).hexdigest()


def build_rag_candidates(
    result: KnowledgeRetrievalResult,
    *,
    limit: int = MAX_SELECTOR_CANDIDATES,
) -> tuple[RAGCandidateOption, ...]:
    """Union both retrieval arms, retain each arm's top answer, and dedupe approved answers."""
    hybrid_rank_by_chunk = {hit.chunk_id: rank for rank, hit in enumerate(result.hits, start=1)}
    vector_rank_by_chunk = {
        hit.chunk_id: rank for rank, hit in enumerate(result.vector_hits, start=1)
    }
    grouped: dict[KnowledgeAnswerIdentity, list[KnowledgeHit]] = {}
    order: list[KnowledgeAnswerIdentity] = []
    for hit in (*result.hits, *result.vector_hits):
        identity = canonical_answer_identity(
            hit.reply,
            hit.is_official_contact,
            hit.protected_values,
        )
        if identity not in grouped:
            grouped[identity] = []
            order.append(identity)
        if all(existing.chunk_id != hit.chunk_id for existing in grouped[identity]):
            grouped[identity].append(hit)

    def rank_key(identity: KnowledgeAnswerIdentity) -> tuple[int, int]:
        hits = grouped[identity]
        hybrid = min(
            (
                hybrid_rank_by_chunk[hit.chunk_id]
                for hit in hits
                if hit.chunk_id in hybrid_rank_by_chunk
            ),
            default=10**9,
        )
        vector = min(
            (
                vector_rank_by_chunk[hit.chunk_id]
                for hit in hits
                if hit.chunk_id in vector_rank_by_chunk
            ),
            default=10**9,
        )
        return hybrid, vector

    ranked_identities = sorted(order, key=rank_key)
    selected_identities: list[KnowledgeAnswerIdentity] = []

    def include(identity: KnowledgeAnswerIdentity | None) -> None:
        if identity is None or identity in selected_identities or len(selected_identities) >= limit:
            return
        selected_identities.append(identity)

    hybrid_top = min(
        (identity for identity in order if rank_key(identity)[0] < 10**9),
        key=lambda identity: rank_key(identity)[0],
        default=None,
    )
    vector_top = min(
        (identity for identity in order if rank_key(identity)[1] < 10**9),
        key=lambda identity: rank_key(identity)[1],
        default=None,
    )
    include(hybrid_top)
    include(vector_top)
    for identity in ranked_identities:
        include(identity)

    options: list[RAGCandidateOption] = []
    arm_evidence = result.arm_evidence or {}
    for index, identity in enumerate(selected_identities, start=1):
        hits = grouped[identity]
        representative = max(hits, key=lambda hit: hit.similarity)
        hybrid_rank = min(
            (hybrid_rank_by_chunk[h.chunk_id] for h in hits if h.chunk_id in hybrid_rank_by_chunk),
            default=None,
        )
        vector_rank = min(
            (vector_rank_by_chunk[h.chunk_id] for h in hits if h.chunk_id in vector_rank_by_chunk),
            default=None,
        )
        merged_arm_evidence: dict[str, Any] = {}
        for hit in hits:
            evidence = arm_evidence.get(hit.content_hash)
            if isinstance(evidence, dict):
                for key, value in evidence.items():
                    current = merged_arm_evidence.get(key)
                    if current is None or (isinstance(value, int) and value < current):
                        merged_arm_evidence[key] = value
        options.append(
            RAGCandidateOption(
                candidate_id=f"candidate-{index}",
                hit=representative,
                answer_hash=_answer_hash(identity),
                content_hashes=tuple(
                    sorted({hit.content_hash for hit in hits})[:MAX_EVIDENCE_CONTENT_HASHES]
                ),
                hybrid_rank=hybrid_rank,
                vector_rank=vector_rank,
                arm_evidence=merged_arm_evidence,
            )
        )
    return tuple(options)


def rag_evidence(
    *,
    candidates: tuple[RAGCandidateOption, ...],
    mode: str,
    canary_bucket: int,
    selection_method: str,
    selected_candidate_id: str | None,
    selector_candidate_id: str | None = None,
    selector_version: str | None,
    selector_latency_ms: float | None,
    retrieval_mode: str | None,
    embedding_version: str | None,
    verifier: dict[str, Any] | None = None,
    guard_reason_codes: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Build bounded evidence without query, message, answer, reply, or contact text."""
    selected = next(
        (candidate for candidate in candidates if candidate.candidate_id == selected_candidate_id),
        None,
    )
    selector_selected = next(
        (candidate for candidate in candidates if candidate.candidate_id == selector_candidate_id),
        None,
    )
    evidence: dict[str, Any] = {
        "schema_version": RAG_EVIDENCE_VERSION,
        "retrieval_policy_version": RETRIEVAL_POLICY_VERSION,
        "retrieval_mode": retrieval_mode,
        "embedding_version": embedding_version,
        "selector_mode": mode,
        "canary_bucket": canary_bucket,
        "selection_method": selection_method,
        "selector_version": selector_version,
        "selector_latency_ms": (
            round(selector_latency_ms, 3) if selector_latency_ms is not None else None
        ),
        "selected_answer_hash": selected.answer_hash if selected else None,
        "selected_content_hash": selected.hit.content_hash if selected else None,
        "selector_answer_hash": selector_selected.answer_hash if selector_selected else None,
        "selector_content_hash": (
            selector_selected.hit.content_hash if selector_selected else None
        ),
        "candidates": [
            {
                "answer_hash": candidate.answer_hash,
                "content_hashes": list(candidate.content_hashes),
                "similarity": round(candidate.hit.similarity, 6),
                "hybrid_rank": candidate.hybrid_rank,
                "vector_rank": candidate.vector_rank,
                "arms": candidate.arm_evidence,
            }
            for candidate in candidates
        ],
        "guard": {"reason_codes": list(guard_reason_codes[:16])},
        "verifier": verifier,
    }
    return evidence
