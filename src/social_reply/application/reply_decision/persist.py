import math
import re
import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.application.account_management.human_workflow import (
    ensure_open_human_work_item,
)
from social_reply.application.handoff_notifications.service import (
    ensure_handoff_notification_intent,
)
from social_reply.application.message_delivery.intents import (
    OutboxActor,
    OutboxOrigin,
    create_or_get_outbox_intent,
    decision_idempotency_key,
)
from social_reply.application.reply_decision.business_prompt import (
    ResolvedBusinessPrompt,
    require_current_business_prompt,
)
from social_reply.application.reply_decision.pipeline import DecisionSnapshot
from social_reply.application.reply_decision.rag_selection import RAG_SELECTION_METHODS
from social_reply.domain.automation.state_machine import AutomationStateEnum
from social_reply.domain.reply.decision import ReplyAction, ReplyDecision
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.advisory_locks import (
    acquire_conversation_delivery_xact_lock,
)
from social_reply.shared.config import get_settings
from social_reply.shared.release import current_release_sha


class ChatwootDecisionDeferred(RuntimeError):
    pass


class DecisionDeliveryConfigurationError(RuntimeError):
    pass


_RAG_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "retrieval_policy_version",
        "retrieval_mode",
        "embedding_version",
        "selector_mode",
        "canary_bucket",
        "selection_method",
        "selector_version",
        "selector_latency_ms",
        "selected_answer_hash",
        "selected_content_hash",
        "selector_answer_hash",
        "selector_content_hash",
        "candidates",
        "guard",
        "verifier",
    }
)
_RAG_AMBIGUITY_TOP_LEVEL_KEYS = _RAG_TOP_LEVEL_KEYS | {"resolution"}
_RAG_CANDIDATE_KEYS = frozenset(
    {
        "answer_hash",
        "content_hashes",
        "similarity",
        "hybrid_rank",
        "vector_rank",
        "arms",
    }
)
_RAG_AMBIGUITY_CANDIDATE_KEYS = _RAG_CANDIDATE_KEYS | {"candidate_id"}
_RAG_GUARD_KEYS = frozenset({"reason_codes"})
_RAG_VERIFIER_KEYS = frozenset({"relevant", "faithful", "version", "latency_ms"})
_RAG_RESOLUTION_KEYS = frozenset(
    {"outcome", "used_candidate_ids", "version", "latency_ms"}
)
_RAG_ARM_KEY = re.compile(r"^(?:native|translated)_(?:hybrid|vector)_rank$")
_RAG_HASH = re.compile(r"^[0-9a-f]{64}$")
_RAG_METADATA_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,63}$")
_RAG_REASON_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_RAG_RETRIEVAL_MODES = frozenset(
    {"exact", "vector_hybrid", "vector_hybrid+query_translation"}
)
def _metadata_version(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > 64:
        raise ValueError(f"{field}_invalid")
    return normalized


def _rag_evidence(value: dict | None) -> dict | None:
    """Validate compact retrieval metadata and reject any evidence body."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("rag_evidence_invalid")

    def normalize(node, *, depth: int):
        if depth > 4:
            raise ValueError("rag_evidence_too_deep")
        if isinstance(node, dict):
            if len(node) > 64:
                raise ValueError("rag_evidence_too_large")
            result = {}
            for key, item in node.items():
                if not isinstance(key, str) or not key or len(key) > 64:
                    raise ValueError("rag_evidence_key_invalid")
                result[key] = normalize(item, depth=depth + 1)
            return result
        if isinstance(node, (list, tuple)):
            if len(node) > 100:
                raise ValueError("rag_evidence_too_large")
            return [normalize(item, depth=depth + 1) for item in node]
        if isinstance(node, uuid.UUID):
            return str(node)
        if isinstance(node, str):
            if len(node) > 256:
                raise ValueError("rag_evidence_string_too_long")
            return node
        if isinstance(node, bool) or node is None or isinstance(node, int):
            return node
        if isinstance(node, float):
            if not math.isfinite(node):
                raise ValueError("rag_evidence_number_invalid")
            return node
        raise ValueError("rag_evidence_value_invalid")

    normalized = normalize(value, depth=0)
    schema_version = normalized.get("schema_version")
    if schema_version == "rag-evidence-v1":
        expected_top_level_keys = _RAG_TOP_LEVEL_KEYS
        expected_candidate_keys = _RAG_CANDIDATE_KEYS
    elif schema_version == "rag-evidence-v2":
        expected_top_level_keys = _RAG_AMBIGUITY_TOP_LEVEL_KEYS
        expected_candidate_keys = _RAG_AMBIGUITY_CANDIDATE_KEYS
    else:
        raise ValueError("rag_evidence_schema_invalid")
    if set(normalized) != expected_top_level_keys:
        raise ValueError("rag_evidence_key_forbidden")
    if normalized.get("retrieval_policy_version") != "hybrid-union-selector-v2":
        raise ValueError("rag_evidence_retrieval_policy_invalid")
    if normalized.get("retrieval_mode") not in {*_RAG_RETRIEVAL_MODES, None}:
        raise ValueError("rag_evidence_retrieval_mode_invalid")

    def require_metadata_token(value, field: str, *, optional: bool = False) -> None:
        if value is None and optional:
            return
        if not isinstance(value, str) or _RAG_METADATA_TOKEN.fullmatch(value) is None:
            raise ValueError(f"rag_evidence_{field}_invalid")

    def require_hash(value, field: str, *, optional: bool = False) -> None:
        if value is None and optional:
            return
        if not isinstance(value, str) or _RAG_HASH.fullmatch(value) is None:
            raise ValueError(f"rag_evidence_{field}_invalid")

    def require_nonnegative_number(value, field: str, *, optional: bool = False) -> None:
        if value is None and optional:
            return
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ValueError(f"rag_evidence_{field}_invalid")

    require_metadata_token(
        normalized.get("embedding_version"),
        "embedding_version",
        optional=True,
    )
    if normalized.get("selector_mode") not in {"off", "shadow", "live"}:
        raise ValueError("rag_evidence_selector_mode_invalid")
    canary_bucket = normalized.get("canary_bucket")
    if isinstance(canary_bucket, bool) or not isinstance(canary_bucket, int) or not (
        0 <= canary_bucket < 10000
    ):
        raise ValueError("rag_evidence_canary_bucket_invalid")
    if normalized.get("selection_method") not in RAG_SELECTION_METHODS:
        raise ValueError("rag_evidence_selection_method_invalid")
    require_metadata_token(
        normalized.get("selector_version"),
        "selector_version",
        optional=True,
    )
    require_nonnegative_number(
        normalized.get("selector_latency_ms"),
        "selector_latency",
        optional=True,
    )
    for field in (
        "selected_answer_hash",
        "selected_content_hash",
        "selector_answer_hash",
        "selector_content_hash",
    ):
        require_hash(normalized.get(field), field, optional=True)

    candidates = normalized.get("candidates")
    if not isinstance(candidates, list) or len(candidates) > 3:
        raise ValueError("rag_evidence_candidates_invalid")
    candidate_ids: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict) or set(candidate) != expected_candidate_keys:
            raise ValueError("rag_evidence_candidate_invalid")
        if schema_version == "rag-evidence-v2":
            candidate_id = candidate.get("candidate_id")
            if (
                not isinstance(candidate_id, str)
                or re.fullmatch(r"candidate-[1-9][0-9]*", candidate_id) is None
                or candidate_id in candidate_ids
            ):
                raise ValueError("rag_evidence_candidate_id_invalid")
            candidate_ids.add(candidate_id)
        require_hash(candidate.get("answer_hash"), "candidate_answer_hash")
        content_hashes = candidate.get("content_hashes")
        if (
            not isinstance(content_hashes, list)
            or not 1 <= len(content_hashes) <= 8
            or any(
                not isinstance(value, str) or _RAG_HASH.fullmatch(value) is None
                for value in content_hashes
            )
        ):
            raise ValueError("rag_evidence_candidate_content_hashes_invalid")
        similarity = candidate.get("similarity")
        if (
            isinstance(similarity, bool)
            or not isinstance(similarity, (int, float))
            or not 0 <= similarity <= 1
        ):
            raise ValueError("rag_evidence_candidate_similarity_invalid")
        ranks = (candidate.get("hybrid_rank"), candidate.get("vector_rank"))
        if all(rank is None for rank in ranks) or any(
            rank is not None
            and (isinstance(rank, bool) or not isinstance(rank, int) or rank < 1)
            for rank in ranks
        ):
            raise ValueError("rag_evidence_candidate_rank_invalid")
        arms = candidate.get("arms")
        if not isinstance(arms, dict) or any(
            _RAG_ARM_KEY.fullmatch(key) is None for key in arms
        ):
            raise ValueError("rag_evidence_arms_invalid")
        if any(
            isinstance(rank, bool) or not isinstance(rank, int) or rank < 1
            for rank in arms.values()
        ):
            raise ValueError("rag_evidence_arm_rank_invalid")

    guard = normalized.get("guard")
    if not isinstance(guard, dict) or set(guard) != _RAG_GUARD_KEYS:
        raise ValueError("rag_evidence_guard_invalid")
    reason_codes = guard.get("reason_codes")
    if (
        not isinstance(reason_codes, list)
        or len(reason_codes) > 16
        or any(
            not isinstance(reason, str) or _RAG_REASON_CODE.fullmatch(reason) is None
            for reason in reason_codes
        )
    ):
        raise ValueError("rag_evidence_reason_codes_invalid")
    verifier = normalized.get("verifier")
    if verifier is not None and (
        not isinstance(verifier, dict) or set(verifier) != _RAG_VERIFIER_KEYS
    ):
        raise ValueError("rag_evidence_verifier_invalid")
    if verifier is not None:
        if any(
            value is not None and not isinstance(value, bool)
            for value in (verifier.get("relevant"), verifier.get("faithful"))
        ):
            raise ValueError("rag_evidence_verifier_result_invalid")
        require_metadata_token(verifier.get("version"), "verifier_version", optional=True)
        require_nonnegative_number(
            verifier.get("latency_ms"),
            "verifier_latency",
            optional=True,
        )
    if schema_version == "rag-evidence-v2":
        resolution = normalized.get("resolution")
        if (
            not isinstance(resolution, dict)
            or set(resolution) != _RAG_RESOLUTION_KEYS
        ):
            raise ValueError("rag_evidence_resolution_invalid")
        outcome = resolution.get("outcome")
        if outcome not in {"answer", "clarify", "abstain"}:
            raise ValueError("rag_evidence_resolution_outcome_invalid")
        used_candidate_ids = resolution.get("used_candidate_ids")
        if (
            not isinstance(used_candidate_ids, list)
            or len(used_candidate_ids) > 2
            or len(set(used_candidate_ids)) != len(used_candidate_ids)
            or any(candidate_id not in candidate_ids for candidate_id in used_candidate_ids)
        ):
            raise ValueError("rag_evidence_resolution_candidates_invalid")
        if outcome == "abstain" and used_candidate_ids:
            raise ValueError("rag_evidence_resolution_abstain_invalid")
        if outcome in {"answer", "clarify"} and not used_candidate_ids:
            raise ValueError("rag_evidence_resolution_evidence_missing")
        if outcome == "clarify" and set(used_candidate_ids) != candidate_ids:
            raise ValueError("rag_evidence_resolution_clarify_invalid")
        require_metadata_token(
            resolution.get("version"),
            "resolution_version",
            optional=True,
        )
        require_nonnegative_number(
            resolution.get("latency_ms"),
            "resolution_latency",
        )
    return normalized


def ensure_decision_delivery_available(
    *,
    account_config: dict,
    chatwoot_inbox_id: int | None,
    chatwoot_enabled: bool,
) -> bool:
    direct_delivery = account_config.get("delivery_mode") == "direct"
    if direct_delivery:
        return True
    if chatwoot_inbox_id is None:
        raise DecisionDeliveryConfigurationError("chatwoot_inbox_id_missing")
    if not chatwoot_enabled:
        raise ChatwootDecisionDeferred("chatwoot_disabled")
    return False


def _idempotency_key(
    account_id: uuid.UUID, conversation_id: uuid.UUID, message_id: uuid.UUID, action: str
) -> str:
    return decision_idempotency_key(account_id, conversation_id, message_id, action)


async def persist_decision(
    session: AsyncSession,
    snapshot: DecisionSnapshot,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID | None,
    account_id: uuid.UUID,
    decision: ReplyDecision,
    prompt_version: str,
    *,
    business_prompt: ResolvedBusinessPrompt | None = None,
    decision_job_id: uuid.UUID | None = None,
    decision_generation: int | None = None,
    decision_claim_token: uuid.UUID | None = None,
    handoff_notification_ids: list[uuid.UUID] | None = None,
) -> uuid.UUID | None:
    """在调用方事务内写 reply_decisions（永远写）+ 按 action 落地副作用。
    auto_reply/draft → 写 outbox（auto_reply 受 state_version CAS 守护，defense 1）。
    返回 outbox_id 或 None。调用方负责 commit。"""
    outbox_id: uuid.UUID | None = None
    message_type: str | None = None
    release_sha = current_release_sha()
    if decision.decision_release_sha not in {None, release_sha}:
        raise ValueError("decision_release_sha_mismatch")
    retrieval_policy_version = _metadata_version(
        decision.retrieval_policy_version,
        "retrieval_policy_version",
    )
    selector_version = _metadata_version(decision.selector_version, "selector_version")
    rag_evidence = _rag_evidence(decision.rag_evidence)
    account = (
        await session.execute(
            select(
                models.PlatformAccount.tenant_id,
                models.PlatformAccount.brand_id,
                models.PlatformAccount.platform,
                models.PlatformAccount.config,
                models.PlatformAccount.chatwoot_inbox_id,
            ).where(models.PlatformAccount.id == account_id)
        )
    ).one()
    direct_delivery = ensure_decision_delivery_available(
        account_config=dict(account.config or {}),
        chatwoot_inbox_id=account.chatwoot_inbox_id,
        chatwoot_enabled=get_settings().chatwoot_enabled,
    )

    if message_id is not None:
        existing = (
            await session.execute(
                select(models.ReplyDecision.outbox_id).where(
                    models.ReplyDecision.message_id == message_id
                )
            )
        ).first()
        if existing is not None:
            return existing.outbox_id

    if business_prompt is not None:
        # Delivery holds these locks in the same order through provider I/O. Keeping one
        # global order avoids a Prompt-save/HANDOFF/delivery deadlock cycle.
        await acquire_conversation_delivery_xact_lock(session, conversation_id)
        await require_current_business_prompt(
            session,
            tenant_id=account.tenant_id,
            brand_id=account.brand_id,
            prompt=business_prompt,
        )

    if decision.action is ReplyAction.AUTO_REPLY:
        # CAS defense 1：仅当会话仍是 BOT_ACTIVE 且 version 未变时才写 outbox
        current = (
            await session.execute(
                select(models.AutomationState.state, models.AutomationState.state_version).where(
                    models.AutomationState.conversation_id == conversation_id
                )
            )
        ).first()
        if (
            current is not None
            and current.state == AutomationStateEnum.BOT_ACTIVE
            and current.state_version == snapshot.state_version
        ):
            message_type = "text"
    elif decision.action is ReplyAction.DRAFT:
        # Direct platforms have no private-note channel. Retain the ReplyDecision as a
        # draft for the admin inbox; never send it to the customer before approval.
        if not direct_delivery and decision.reply_text:
            message_type = "private_note"
    elif decision.action is ReplyAction.HANDOFF:
        await acquire_conversation_delivery_xact_lock(session, conversation_id)
        current = (
            await session.execute(
                select(models.AutomationState)
                .where(models.AutomationState.conversation_id == conversation_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        may_create_work = current is not None and (
            current.state == AutomationStateEnum.HANDOFF_PENDING
            or (
                current.state_version == snapshot.state_version
                and current.state
                not in [AutomationStateEnum.HUMAN_ACTIVE, AutomationStateEnum.CLOSED]
            )
        )
        if may_create_work:
            reason_code = decision.reason_codes[-1] if decision.reason_codes else "HANDOFF"
            if current.state != AutomationStateEnum.HANDOFF_PENDING:
                current.state = AutomationStateEnum.HANDOFF_PENDING
                current.state_version += 1
                current.state_changed_reason = reason_code
            work = await ensure_open_human_work_item(
                session,
                tenant_id=account.tenant_id,
                conversation_id=conversation_id,
                reason_code=reason_code,
            )
            notification = await ensure_handoff_notification_intent(session, work=work)
            if handoff_notification_ids is not None and notification.status == "PENDING":
                handoff_notification_ids.append(notification.id)

    if message_type is not None:
        if direct_delivery and message_id is None:
            raise DecisionDeliveryConfigurationError("direct_delivery_message_id_missing")
        try:
            outbox_id = await create_or_get_outbox_intent(
                session,
                conversation_id=conversation_id,
                platform_account_id=account_id,
                reply_to_message_id=message_id,
                text=decision.reply_text or "",
                origin_kind=OutboxOrigin.DECISION,
                actor_kind=OutboxActor.BOT,
                actor_id=None,
                idempotency_key=_idempotency_key(
                    account_id,
                    conversation_id,
                    message_id or conversation_id,
                    decision.action,
                ),
                visibility=decision.reply_visibility,
                message_type=message_type,
            )
        except ValueError as exc:
            raise DecisionDeliveryConfigurationError(str(exc)) from exc

    inserted_decision = (
        await session.execute(
            pg_insert(models.ReplyDecision)
            .values(
                id=uuid.uuid4(),
                tenant_id=account.tenant_id,
                conversation_id=conversation_id,
                message_id=message_id,
                action=decision.action,
                intent=decision.intent,
                risk_level=decision.risk_level,
                confidence=decision.confidence,
                reply_text=decision.reply_text,
                original_reply_text=decision.reply_text,
                review_action="PENDING" if decision.action is ReplyAction.DRAFT else None,
                reply_visibility=decision.reply_visibility,
                reason_codes=list(decision.reason_codes),
                source=decision.source,
                prompt_version=prompt_version,
                reply_business_prompt_version_id=(
                    business_prompt.version_id if business_prompt is not None else None
                ),
                reply_business_prompt_content_hash=(
                    business_prompt.content_hash if business_prompt is not None else None
                ),
                decision_release_sha=release_sha,
                retrieval_policy_version=retrieval_policy_version,
                selector_version=selector_version,
                rag_evidence=rag_evidence,
                request_language=decision.request_language,
                reply_language=decision.reply_language,
                resolved_locale=decision.resolved_locale,
                knowledge_localization_id=decision.knowledge_localization_id,
                knowledge_localization_release_id=decision.knowledge_localization_release_id,
                knowledge_localization_text_hash=decision.knowledge_localization_text_hash,
                knowledge_localization_source_hash=decision.knowledge_localization_source_hash,
                knowledge_content_hash=decision.knowledge_content_hash,
                knowledge_document_id=decision.knowledge_document_id,
                knowledge_chunk_id=decision.knowledge_chunk_id,
                knowledge_similarity=decision.knowledge_similarity,
                knowledge_similarity_margin=decision.knowledge_similarity_margin,
                multilingual_shadow=decision.multilingual_shadow,
                multilingual_contract_version=decision.multilingual_contract_version,
                multilingual_shadow_evidence=decision.multilingual_shadow_evidence,
                request_language_confidence=decision.request_language_confidence,
                request_language_source=decision.request_language_source,
                knowledge_top2_content_hash=decision.knowledge_top2_content_hash,
                knowledge_top2_similarity=decision.knowledge_top2_similarity,
                knowledge_match_status=decision.knowledge_match_status,
                knowledge_gate_version=decision.knowledge_gate_version,
                knowledge_min_similarity_threshold=decision.knowledge_min_similarity_threshold,
                knowledge_min_margin_threshold=decision.knowledge_min_margin_threshold,
                grounding_verified=decision.grounding_verified,
                grounding_verifier_version=decision.grounding_verifier_version,
                grounding_latency_ms=decision.grounding_latency_ms,
                state_version_at_decision=snapshot.state_version,
                decision_job_id=decision_job_id,
                decision_generation=decision_generation,
                decision_claim_token=decision_claim_token,
                outbox_id=outbox_id,
            )
            .on_conflict_do_nothing(index_elements=["message_id"])
            .returning(models.ReplyDecision.outbox_id)
        )
    ).scalar_one_or_none()
    if inserted_decision is None and message_id is not None:
        return (
            await session.execute(
                select(models.ReplyDecision.outbox_id).where(
                    models.ReplyDecision.message_id == message_id
                )
            )
        ).scalar_one()
    return outbox_id
