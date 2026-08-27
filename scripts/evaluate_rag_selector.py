"""Replay privacy-safe historical RAG evidence against the consensus selector.

The script reads customer and knowledge text only in memory. It emits aggregate metrics and
never prints or writes message bodies, approved answers, candidate hashes, or database IDs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from social_reply.application.reply_decision.rag_selection import (
    RAGConsensusResult,
    select_rag_answer_with_consensus,
)
from social_reply.application.reply_decision.runner import _get_llm
from social_reply.domain.reply.llm import RAGCandidate
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory


@dataclass(frozen=True)
class ReplaySample:
    query: str
    candidates: tuple[RAGCandidate, ...]
    answer_hash_by_candidate_id: dict[str, str]
    cohort: str
    expected_answer_hash: str | None


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate the dual-order RAG selector without persisting replay content."
    )
    parser.add_argument(
        "--max-samples",
        type=int,
        default=100,
        help="Maximum historical decisions to inspect (default: 100).",
    )
    return parser.parse_args()


def _candidate_content_hashes(evidence: dict[str, Any]) -> set[str]:
    content_hashes: set[str] = set()
    for candidate in evidence.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        for content_hash in candidate.get("content_hashes") or []:
            if isinstance(content_hash, str) and content_hash:
                content_hashes.add(content_hash)
    return content_hashes


async def _load_replay_samples(max_samples: int) -> tuple[list[ReplaySample], int]:
    async with get_session_factory()() as session:
        decision_rows = (
            await session.execute(
                select(
                    models.ReplyDecision.tenant_id,
                    models.ReplyDecision.action,
                    models.ReplyDecision.reason_codes,
                    models.ReplyDecision.rag_evidence,
                    models.Message.text,
                )
                .join(models.Message, models.Message.id == models.ReplyDecision.message_id)
                .where(models.ReplyDecision.rag_evidence.is_not(None))
                .order_by(models.ReplyDecision.created_at.desc())
                .limit(max_samples)
            )
        ).all()
        requested_hashes_by_tenant: dict[str, set[str]] = {}
        for row in decision_rows:
            evidence = row.rag_evidence if isinstance(row.rag_evidence, dict) else {}
            requested_hashes_by_tenant.setdefault(row.tenant_id, set()).update(
                _candidate_content_hashes(evidence)
            )

        knowledge_by_tenant_and_hash: dict[
            tuple[str, str], tuple[str, str, bool]
        ] = {}
        for tenant_id, content_hashes in requested_hashes_by_tenant.items():
            if not content_hashes:
                continue
            knowledge_rows = (
                await session.execute(
                    select(
                        models.KnowledgeChunk.content_hash,
                        models.KnowledgeDocument.question,
                        models.KnowledgeDocument.reply,
                        models.KnowledgeDocument.is_official_contact,
                    )
                    .join(
                        models.KnowledgeDocument,
                        (models.KnowledgeDocument.tenant_id == models.KnowledgeChunk.tenant_id)
                        & (models.KnowledgeDocument.id == models.KnowledgeChunk.document_id),
                    )
                    .where(
                        models.KnowledgeChunk.tenant_id == tenant_id,
                        models.KnowledgeChunk.content_hash.in_(content_hashes),
                    )
                )
            ).all()
            for knowledge_row in knowledge_rows:
                knowledge_by_tenant_and_hash[(tenant_id, knowledge_row.content_hash)] = (
                    knowledge_row.question,
                    knowledge_row.reply,
                    knowledge_row.is_official_contact,
                )

    samples: list[ReplaySample] = []
    for row in decision_rows:
        if not isinstance(row.text, str) or not row.text.strip():
            continue
        evidence = row.rag_evidence if isinstance(row.rag_evidence, dict) else {}
        reason_codes = set(row.reason_codes or [])
        expected_answer_hash = evidence.get("selected_answer_hash")
        if row.action in {"auto_reply", "draft"} and isinstance(expected_answer_hash, str):
            cohort = "prior_safe"
        elif "NO_STRONG_KNOWLEDGE_MATCH" in reason_codes:
            cohort = "ambiguous_handoff"
            expected_answer_hash = None
        else:
            continue

        candidates: list[RAGCandidate] = []
        answer_hash_by_candidate_id: dict[str, str] = {}
        for index, candidate_evidence in enumerate(evidence.get("candidates") or [], start=1):
            if not isinstance(candidate_evidence, dict):
                continue
            answer_hash = candidate_evidence.get("answer_hash")
            similarity = candidate_evidence.get("similarity")
            content_hashes = candidate_evidence.get("content_hashes") or []
            if not isinstance(answer_hash, str) or not isinstance(similarity, (int, float)):
                continue
            knowledge = next(
                (
                    knowledge_by_tenant_and_hash.get((row.tenant_id, content_hash))
                    for content_hash in content_hashes
                    if isinstance(content_hash, str)
                    and (row.tenant_id, content_hash) in knowledge_by_tenant_and_hash
                ),
                None,
            )
            if knowledge is None:
                continue
            question, approved_answer, is_official_contact = knowledge
            if is_official_contact:
                continue
            candidate_id = f"candidate-{index}"
            candidates.append(
                RAGCandidate(
                    candidate_id=candidate_id,
                    question=question,
                    approved_answer=approved_answer,
                    similarity=float(similarity),
                )
            )
            answer_hash_by_candidate_id[candidate_id] = answer_hash
        if not candidates:
            continue
        samples.append(
            ReplaySample(
                query=row.text,
                candidates=tuple(candidates),
                answer_hash_by_candidate_id=answer_hash_by_candidate_id,
                cohort=cohort,
                expected_answer_hash=(
                    expected_answer_hash if isinstance(expected_answer_hash, str) else None
                ),
            )
        )
    return samples, len(decision_rows)


async def _evaluate_sample(selector: Any, sample: ReplaySample) -> tuple[RAGConsensusResult, float]:
    started = time.perf_counter()
    result = await select_rag_answer_with_consensus(
        selector=selector,
        query=sample.query,
        candidates=sample.candidates,
    )
    return result, (time.perf_counter() - started) * 1000


async def main() -> None:
    arguments = _parse_arguments()
    if arguments.max_samples < 1 or arguments.max_samples > 100:
        raise SystemExit("--max-samples must be between 1 and 100")
    samples, inspected_decisions = await _load_replay_samples(arguments.max_samples)
    llm = _get_llm()
    selector = getattr(llm, "select_rag_answer", None)
    if selector is None:
        raise SystemExit("configured LLM does not provide select_rag_answer")

    metrics: dict[str, int | float | bool | str | None] = {
        "inspected_decisions": inspected_decisions,
        "evaluable_samples": len(samples),
        "prior_safe_samples": 0,
        "ambiguous_handoff_samples": 0,
        "consensus_selections": 0,
        "consensus_abstains": 0,
        "order_disagreements": 0,
        "prior_safe_matching_selections": 0,
        "prior_safe_wrong_selections": 0,
        "ambiguous_consensus_selections": 0,
        "average_selector_latency_ms": None,
        "zero_wrong_selection_target_met": False,
        "selector_version": getattr(llm, "rag_selector_id", None),
    }
    latencies: list[float] = []
    try:
        for sample in samples:
            metrics[f"{sample.cohort}_samples"] += 1
            consensus, latency_ms = await _evaluate_sample(selector, sample)
            latencies.append(latency_ms)
            if consensus.disagreed:
                metrics["order_disagreements"] += 1
            if consensus.selected_candidate_id is None:
                metrics["consensus_abstains"] += 1
                continue
            metrics["consensus_selections"] += 1
            selected_answer_hash = sample.answer_hash_by_candidate_id.get(
                consensus.selected_candidate_id
            )
            if sample.cohort == "ambiguous_handoff":
                metrics["ambiguous_consensus_selections"] += 1
            elif selected_answer_hash == sample.expected_answer_hash:
                metrics["prior_safe_matching_selections"] += 1
            else:
                metrics["prior_safe_wrong_selections"] += 1
    finally:
        close = getattr(llm, "aclose", None)
        if close is not None:
            await close()

    if latencies:
        metrics["average_selector_latency_ms"] = round(
            sum(latencies) / len(latencies),
            3,
        )
    metrics["zero_wrong_selection_target_met"] = (
        metrics["prior_safe_wrong_selections"] == 0
    )
    print(json.dumps(metrics, ensure_ascii=True, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
