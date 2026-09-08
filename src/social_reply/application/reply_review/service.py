from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.application.account_management.access import lock_user_authority
from social_reply.application.account_management.auth import (
    Principal,
    principal_from_session_row,
)
from social_reply.application.knowledge.drafts import knowledge_document_safety_lock_key
from social_reply.application.message_delivery.intents import (
    OutboxActor,
    OutboxIdempotencyConflict,
    OutboxIntentError,
    OutboxOrigin,
    create_or_get_outbox_intent,
)
from social_reply.application.reply_decision.business_prompt import (
    business_prompt_provenance_is_current,
)
from social_reply.domain.knowledge.policy import knowledge_revision_hash
from social_reply.domain.platform_accounts import LEGACY_ACTIVE_ACCOUNT_STATUSES
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.advisory_locks import (
    acquire_conversation_delivery_xact_lock,
    acquire_shared_xact_lock,
)
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.queue.dispatch import dispatch_actor
from social_reply.shared.config import get_settings

_MAX_REPLY_TEXT_LENGTH = 10000
_MAX_REVIEW_REASON_LENGTH = 500
_KNOWN_REVIEW_ACTIONS = frozenset({"PENDING", "ACCEPTED", "EDITED", "REJECTED"})


class DraftReviewError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class DraftReviewNotFound(DraftReviewError):
    pass


class DraftReviewConflict(DraftReviewError):
    pass


class DraftReviewValidationError(DraftReviewError):
    pass


@dataclass(frozen=True)
class DraftReviewResult:
    decision_id: uuid.UUID
    review_action: str
    outbox_id: uuid.UUID | None
    created: bool


@dataclass(frozen=True)
class _LockedDraftContext:
    decision: models.ReplyDecision
    conversation: models.Conversation
    account: models.PlatformAccount
    message: models.Message
    original_text: str
    current_review_action: str
    current_principal: Principal | None
    staff_user: models.AdminUser | None


def _normalize_actor(actor: str) -> str:
    normalized_actor = actor.strip()
    if not normalized_actor or len(normalized_actor) > 255:
        raise DraftReviewValidationError("draft_review_actor_invalid")
    return normalized_actor


async def _authorize_reviewer(
    *,
    principal: Principal | None,
    current_principal: Principal | None,
    staff_user: models.AdminUser | None,
    account: models.PlatformAccount,
    tenant_id: str,
    actor: str,
) -> Principal:
    if principal is None or current_principal is None:
        raise DraftReviewConflict("draft_approver_principal_required")
    current = current_principal
    if (
        not current.is_workspace_admin
        or tenant_id not in current.allowed_tenants
        or current.tenant_id not in {None, tenant_id}
        or current.must_change_password
        or not current.can_access_account(account)
    ):
        raise DraftReviewConflict("draft_approver_not_authorized")
    if current.user_id is not None:
        if (
            staff_user is None
            or staff_user.id != current.user_id
            or staff_user.tenant_id != tenant_id
            or staff_user.username != current.username
            or staff_user.status != "active"
            or staff_user.role != "WORKSPACE_ADMIN"
            or staff_user.must_change_password
        ):
            raise DraftReviewConflict("draft_approver_not_authorized")
    elif not current.is_superadmin:
        raise DraftReviewConflict("draft_approver_not_authorized")
    if actor != current.actor:
        raise DraftReviewConflict("draft_approver_identity_mismatch")
    return current


def _normalize_tenant_id(required_tenant_id: str) -> str:
    normalized_tenant_id = required_tenant_id.strip()
    if not normalized_tenant_id or len(normalized_tenant_id) > 255:
        raise DraftReviewValidationError("draft_review_tenant_invalid")
    return normalized_tenant_id


def _validate_expected_review_action(expected_review_action: str | None) -> str | None:
    if expected_review_action is None:
        return None
    normalized_action = expected_review_action.strip().upper()
    if normalized_action not in _KNOWN_REVIEW_ACTIONS:
        raise DraftReviewValidationError("draft_expected_review_action_invalid")
    return normalized_action


def _normalize_final_reply_text(value: str | None, *, original_text: str) -> str:
    normalized_text = original_text if value is None else value.strip()
    if not normalized_text:
        raise DraftReviewValidationError("draft_reply_text_required")
    if len(normalized_text) > _MAX_REPLY_TEXT_LENGTH:
        raise DraftReviewValidationError("draft_reply_text_too_long")
    return normalized_text


def _normalize_review_reason(value: str) -> str:
    normalized_reason = value.strip()
    if not normalized_reason:
        raise DraftReviewValidationError("draft_review_reason_required")
    if len(normalized_reason) > _MAX_REVIEW_REASON_LENGTH:
        raise DraftReviewValidationError("draft_review_reason_too_long")
    return normalized_reason


async def _load_locked_draft_context(
    session: AsyncSession,
    *,
    decision_id: uuid.UUID,
    required_tenant_id: str,
    expected_generation: int | None,
    principal: Principal | None,
) -> _LockedDraftContext:
    identity = (
        await session.execute(
            select(
                models.ReplyDecision.conversation_id,
                models.ReplyDecision.tenant_id,
            ).where(
                models.ReplyDecision.id == decision_id,
                models.ReplyDecision.tenant_id == required_tenant_id,
            )
        )
    ).one_or_none()
    if identity is None:
        raise DraftReviewNotFound("decision_not_found")
    if principal is None or principal.session_id is None:
        raise DraftReviewConflict("draft_approver_principal_required")
    if principal.user_id is not None:
        await lock_user_authority(session, principal.user_id)
    current_principal = await principal_from_session_row(
        session,
        principal.session_id,
        for_update=True,
    )
    if current_principal is None:
        raise DraftReviewConflict("draft_approver_session_invalid")

    await acquire_conversation_delivery_xact_lock(session, identity.conversation_id)
    conversation = await session.scalar(
        select(models.Conversation)
        .where(
            models.Conversation.id == identity.conversation_id,
            models.Conversation.tenant_id == required_tenant_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if conversation is None:
        raise DraftReviewConflict("decision_tenant_scope_mismatch")
    account = await session.scalar(
        select(models.PlatformAccount)
        .where(
            models.PlatformAccount.id == conversation.platform_account_id,
            models.PlatformAccount.tenant_id == required_tenant_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    staff_user = None
    if current_principal.user_id is not None:
        staff_user = await session.scalar(
            select(models.AdminUser)
            .where(
                models.AdminUser.id == current_principal.user_id,
                models.AdminUser.tenant_id == required_tenant_id,
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
    decision = await session.scalar(
        select(models.ReplyDecision)
        .where(
            models.ReplyDecision.id == decision_id,
            models.ReplyDecision.tenant_id == required_tenant_id,
            models.ReplyDecision.conversation_id == identity.conversation_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if account is None or decision is None:
        raise DraftReviewConflict("decision_tenant_scope_mismatch")
    if decision.action != "draft":
        raise DraftReviewConflict("decision_not_pending_draft")
    if account.brand_id != conversation.brand_id or account.platform != conversation.platform:
        raise DraftReviewConflict("decision_tenant_scope_mismatch")
    if account.status not in LEGACY_ACTIVE_ACCOUNT_STATUSES:
        raise DraftReviewConflict("draft_account_not_active")

    if decision.message_id is None:
        raise DraftReviewConflict("draft_message_provenance_invalid")
    message = await session.scalar(
        select(models.Message)
        .where(
            models.Message.id == decision.message_id,
            models.Message.conversation_id == conversation.id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if message is None or message.direction != "inbound":
        raise DraftReviewConflict("draft_message_provenance_invalid")
    if (
        decision.decision_generation is None
        or decision.decision_generation != conversation.decision_generation
        or message.decision_generation != decision.decision_generation
    ):
        raise DraftReviewConflict("draft_stale_conversation_input")
    if expected_generation is not None and expected_generation != decision.decision_generation:
        raise DraftReviewConflict("draft_stale_conversation_input")

    await _validate_prompt_provenance(
        session,
        decision=decision,
        account=account,
    )
    await _validate_knowledge_provenance(
        session,
        decision=decision,
    )
    original_text = (decision.original_reply_text or "").strip() or (
        decision.reply_text or ""
    ).strip()
    if not original_text:
        raise DraftReviewConflict("draft_reply_text_missing")
    return _LockedDraftContext(
        decision=decision,
        conversation=conversation,
        account=account,
        message=message,
        original_text=original_text,
        current_review_action=decision.review_action or "PENDING",
        current_principal=current_principal,
        staff_user=staff_user,
    )


async def _validate_prompt_provenance(
    session: AsyncSession,
    *,
    decision: models.ReplyDecision,
    account: models.PlatformAccount,
) -> None:
    prompt_version_id = decision.reply_business_prompt_version_id
    prompt_content_hash = decision.reply_business_prompt_content_hash
    if prompt_version_id is not None and prompt_content_hash is None:
        raise DraftReviewConflict("draft_business_prompt_provenance_invalid")

    prompt_feature_enabled = get_settings().reply_business_prompt_enabled
    if prompt_content_hash is not None and not prompt_feature_enabled:
        raise DraftReviewConflict("draft_business_prompt_disabled")
    if prompt_feature_enabled and prompt_content_hash is None:
        raise DraftReviewConflict("draft_business_prompt_provenance_required")
    if prompt_content_hash is None:
        return

    prompt_is_current = await business_prompt_provenance_is_current(
        session,
        tenant_id=account.tenant_id,
        brand_id=account.brand_id,
        version_id=prompt_version_id,
        content_hash=prompt_content_hash,
    )
    if not prompt_is_current:
        raise DraftReviewConflict("draft_business_prompt_stale")


async def _validate_knowledge_provenance(
    session: AsyncSession,
    *,
    decision: models.ReplyDecision,
) -> None:
    provenance_values = (
        decision.knowledge_document_id,
        decision.knowledge_chunk_id,
        decision.knowledge_content_hash,
    )
    if all(value is None for value in provenance_values):
        return
    if any(value is None for value in provenance_values):
        raise DraftReviewConflict("draft_knowledge_provenance_invalid")

    document_id = decision.knowledge_document_id
    chunk_id = decision.knowledge_chunk_id
    content_hash = decision.knowledge_content_hash
    if document_id is None or chunk_id is None or not content_hash:
        raise DraftReviewConflict("draft_knowledge_provenance_invalid")

    await acquire_shared_xact_lock(
        session,
        knowledge_document_safety_lock_key(decision.tenant_id, document_id),
    )
    row = (
        await session.execute(
            select(models.KnowledgeDocument, models.KnowledgeChunk)
            .join(
                models.KnowledgeChunk,
                and_(
                    models.KnowledgeChunk.tenant_id == models.KnowledgeDocument.tenant_id,
                    models.KnowledgeChunk.document_id == models.KnowledgeDocument.id,
                ),
            )
            .where(
                models.KnowledgeDocument.tenant_id == decision.tenant_id,
                models.KnowledgeDocument.id == document_id,
                models.KnowledgeChunk.id == chunk_id,
            )
            .with_for_update(read=True)
        )
    ).one_or_none()
    if row is None:
        raise DraftReviewConflict("draft_knowledge_provenance_stale")

    document, chunk = row
    current_content = f"问：{document.question}\n答：{document.reply}"
    current_hash = knowledge_revision_hash(
        current_content,
        document.protected_values or (),
    )
    if (
        document.status != "published"
        or chunk.content != current_content
        or chunk.content_hash != content_hash
        or current_hash != content_hash
    ):
        raise DraftReviewConflict("draft_knowledge_provenance_stale")


def _require_pending_fence(
    *,
    current_review_action: str,
    expected_review_action: str | None,
) -> None:
    if current_review_action != "PENDING":
        raise DraftReviewConflict("decision_not_pending_draft")
    if expected_review_action is not None and expected_review_action != current_review_action:
        raise DraftReviewConflict("draft_review_action_conflict")


async def approve_draft(
    *,
    decision_id: uuid.UUID,
    required_tenant_id: str,
    actor: str,
    final_reply_text: str | None,
    expected_generation: int | None,
    expected_review_action: str | None,
    principal: Principal | None = None,
) -> DraftReviewResult:
    normalized_tenant_id = _normalize_tenant_id(required_tenant_id)
    normalized_actor = _normalize_actor(actor)
    normalized_expected_action = _validate_expected_review_action(expected_review_action)
    created = False
    outbox_id: uuid.UUID | None = None

    async with get_session_factory()() as session:
        context = await _load_locked_draft_context(
            session,
            decision_id=decision_id,
            required_tenant_id=normalized_tenant_id,
            expected_generation=expected_generation,
            principal=principal,
        )
        reviewer = await _authorize_reviewer(
            principal=principal,
            current_principal=context.current_principal,
            staff_user=context.staff_user,
            account=context.account,
            tenant_id=normalized_tenant_id,
            actor=normalized_actor,
        )
        effective_actor = reviewer.actor
        if (
            context.current_review_action in {"ACCEPTED", "EDITED"}
            and context.decision.reviewed_by != effective_actor
        ):
            raise DraftReviewConflict("draft_approval_identity_conflict")
        final_text = _normalize_final_reply_text(
            final_reply_text,
            original_text=context.original_text,
        )
        review_action = "ACCEPTED" if final_text == context.original_text else "EDITED"

        if context.current_review_action in {"ACCEPTED", "EDITED"}:
            if (
                context.decision.review_outbox_id is None
                or context.decision.final_reply_text != final_text
                or context.current_review_action != review_action
            ):
                raise DraftReviewConflict("draft_approval_conflict")
            outbox_id = context.decision.review_outbox_id
        elif context.current_review_action == "REJECTED":
            raise DraftReviewConflict("decision_not_pending_draft")
        else:
            _require_pending_fence(
                current_review_action=context.current_review_action,
                expected_review_action=normalized_expected_action,
            )
            if context.decision.review_outbox_id is not None:
                raise DraftReviewConflict("decision_not_pending_draft")
            try:
                outbox_id = await create_or_get_outbox_intent(
                    session,
                    conversation_id=context.conversation.id,
                    platform_account_id=context.account.id,
                    reply_to_message_id=context.message.id,
                    text=final_text,
                    origin_kind=OutboxOrigin.DRAFT_APPROVAL,
                    actor_kind=OutboxActor.ADMIN_HUMAN,
                    actor_id=effective_actor,
                    idempotency_key=f"draft-approval:{decision_id}",
                    visibility=context.decision.reply_visibility,
                    payload_metadata={
                        "approval": "admin",
                        "approved_by": effective_actor,
                    },
                    initiator_user_id=reviewer.user_id,
                    initiator_session_id=reviewer.session_id,
                    human_work_item_version=None,
                )
            except OutboxIdempotencyConflict as exc:
                raise DraftReviewConflict("draft_approval_conflict") from exc
            except OutboxIntentError as exc:
                raise DraftReviewValidationError(str(exc)) from exc

            context.decision.original_reply_text = context.original_text
            context.decision.final_reply_text = final_text
            context.decision.review_action = review_action
            context.decision.reviewed_by = effective_actor
            context.decision.reviewed_at = datetime.now(UTC)
            context.decision.review_reason = None
            context.decision.review_outbox_id = outbox_id
            session.add(
                models.AuditLog(
                    tenant_id=normalized_tenant_id,
                    category="admin_action",
                    actor=effective_actor,
                    action="APPROVE_DRAFT",
                    subject_type="reply_decision",
                    subject_id=str(decision_id),
                    detail={
                        "outbox_id": str(outbox_id),
                        "review_action": review_action,
                    },
                )
            )
            created = True
        await session.commit()

    if created and outbox_id is not None:
        from social_reply.application.message_delivery.actors import deliver_outbox_message
        from social_reply.application.message_delivery.outbox import deliver_outbox

        await dispatch_actor(
            deliver_outbox_message,
            str(outbox_id),
            inline=lambda: deliver_outbox(str(outbox_id)),
        )
    return DraftReviewResult(
        decision_id=decision_id,
        review_action=review_action,
        outbox_id=outbox_id,
        created=created,
    )


async def reject_draft(
    *,
    decision_id: uuid.UUID,
    required_tenant_id: str,
    actor: str,
    review_reason: str,
    expected_generation: int | None,
    expected_review_action: str | None,
    principal: Principal | None = None,
) -> DraftReviewResult:
    normalized_tenant_id = _normalize_tenant_id(required_tenant_id)
    normalized_actor = _normalize_actor(actor)
    normalized_reason = _normalize_review_reason(review_reason)
    normalized_expected_action = _validate_expected_review_action(expected_review_action)
    created = False

    async with get_session_factory()() as session:
        context = await _load_locked_draft_context(
            session,
            decision_id=decision_id,
            required_tenant_id=normalized_tenant_id,
            expected_generation=expected_generation,
            principal=principal,
        )
        reviewer = await _authorize_reviewer(
            principal=principal,
            current_principal=context.current_principal,
            staff_user=context.staff_user,
            account=context.account,
            tenant_id=normalized_tenant_id,
            actor=normalized_actor,
        )
        effective_actor = reviewer.actor
        if (
            context.current_review_action == "REJECTED"
            and context.decision.reviewed_by != effective_actor
        ):
            raise DraftReviewConflict("draft_rejection_identity_conflict")
        if context.current_review_action == "REJECTED":
            if (
                context.decision.review_outbox_id is not None
                or context.decision.review_reason != normalized_reason
            ):
                raise DraftReviewConflict("draft_rejection_conflict")
        elif context.current_review_action in {"ACCEPTED", "EDITED"}:
            raise DraftReviewConflict("decision_not_pending_draft")
        else:
            _require_pending_fence(
                current_review_action=context.current_review_action,
                expected_review_action=normalized_expected_action,
            )
            if context.decision.review_outbox_id is not None:
                raise DraftReviewConflict("decision_not_pending_draft")
            reason_codes = list(context.decision.reason_codes or [])
            if "ADMIN_DISCARDED" not in reason_codes:
                reason_codes.append("ADMIN_DISCARDED")
            context.decision.original_reply_text = context.original_text
            context.decision.final_reply_text = None
            context.decision.review_action = "REJECTED"
            context.decision.reviewed_by = effective_actor
            context.decision.reviewed_at = datetime.now(UTC)
            context.decision.review_reason = normalized_reason
            context.decision.reason_codes = reason_codes
            session.add(
                models.AuditLog(
                    tenant_id=normalized_tenant_id,
                    category="admin_action",
                    actor=effective_actor,
                    action="REJECT_DRAFT",
                    subject_type="reply_decision",
                    subject_id=str(decision_id),
                    detail={"reason": normalized_reason},
                )
            )
            created = True
        await session.commit()

    return DraftReviewResult(
        decision_id=decision_id,
        review_action="REJECTED",
        outbox_id=None,
        created=created,
    )
