from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import distinct, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.application.account_management.human_workflow import (
    ensure_open_human_work_item,
)
from social_reply.application.handoff_notifications.service import (
    ensure_handoff_notification_intent,
)
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.advisory_locks import (
    acquire_conversation_delivery_xact_lock,
)
from social_reply.shared.config import get_settings

_RETIREABLE_OUTBOX_STATUSES = ("PENDING", "FAILED", "NEEDS_REVIEW")
_ROLLBACK_REASON = "REPLY_BUSINESS_PROMPT_ROLLBACK"
_ROLLBACK_ACTOR = "system:business-prompt-rollback"


@dataclass(frozen=True)
class BusinessPromptRetirementReport:
    conversations: int
    outboxes_cancelled: int
    drafts_rejected: int


def _decision_outbox_link():
    return or_(
        models.ReplyDecision.outbox_id == models.OutboxMessage.id,
        models.ReplyDecision.review_outbox_id == models.OutboxMessage.id,
    )


def _has_business_prompt_provenance():
    return or_(
        models.ReplyDecision.reply_business_prompt_version_id.is_not(None),
        models.ReplyDecision.reply_business_prompt_content_hash.is_not(None),
    )


def _is_pending_business_prompt_draft():
    return (
        (models.ReplyDecision.action == "draft")
        & _has_business_prompt_provenance()
        & (models.ReplyDecision.review_outbox_id.is_(None))
        & or_(
            models.ReplyDecision.review_action.is_(None),
            models.ReplyDecision.review_action == "PENDING",
        )
    )


async def retire_business_prompt_work_for_rollback(
    session: AsyncSession,
) -> BusinessPromptRetirementReport:
    """Make Prompt-derived unsent work unrepresentable to a legacy runtime.

    The shared runtime gate must already be disabled. Conversation delivery locks drain active
    provider calls and prevent a pending Outbox from being claimed while it is retired.
    """
    if get_settings().reply_business_prompt_enabled:
        raise RuntimeError("reply_business_prompt_must_be_disabled_before_retirement")

    outbox_conversation_ids = set(
        await session.scalars(
            select(distinct(models.ReplyDecision.conversation_id))
            .join(models.OutboxMessage, _decision_outbox_link())
            .where(
                _has_business_prompt_provenance(),
                models.OutboxMessage.status.in_(
                    (*_RETIREABLE_OUTBOX_STATUSES, "SENDING")
                ),
            )
        )
    )
    pending_draft_conversation_ids = set(
        await session.scalars(
            select(distinct(models.ReplyDecision.conversation_id)).where(
                _is_pending_business_prompt_draft()
            )
        )
    )
    conversation_ids = sorted(
        outbox_conversation_ids | pending_draft_conversation_ids,
        key=str,
    )

    for conversation_id in conversation_ids:
        await acquire_conversation_delivery_xact_lock(session, conversation_id)

    sending_outbox_ids = tuple(
        (
            await session.scalars(
                select(models.OutboxMessage.id)
                .join(models.ReplyDecision, _decision_outbox_link())
                .where(
                    _has_business_prompt_provenance(),
                    models.OutboxMessage.status == "SENDING",
                )
                .with_for_update()
            )
        ).unique()
    )
    if sending_outbox_ids:
        raise RuntimeError("business_prompt_delivery_still_sending")

    outboxes = tuple(
        (
            await session.scalars(
                select(models.OutboxMessage)
                .join(models.ReplyDecision, _decision_outbox_link())
                .where(
                    _has_business_prompt_provenance(),
                    models.OutboxMessage.status.in_(_RETIREABLE_OUTBOX_STATUSES),
                )
                .with_for_update()
            )
        ).unique()
    )
    for outbox in outboxes:
        outbox.status = "CANCELLED"
        outbox.last_error_code = _ROLLBACK_REASON
        outbox.last_error_message = None
        outbox.next_attempt_at = None
        outbox.locked_at = None
        outbox.locked_by = None

    drafts = tuple(
        await session.scalars(
            select(models.ReplyDecision)
            .where(_is_pending_business_prompt_draft())
            .with_for_update()
        )
    )
    reviewed_at = datetime.now(UTC)
    for decision in drafts:
        reason_codes = list(decision.reason_codes or [])
        if _ROLLBACK_REASON not in reason_codes:
            reason_codes.append(_ROLLBACK_REASON)
        decision.original_reply_text = decision.original_reply_text or decision.reply_text
        decision.final_reply_text = None
        decision.review_action = "REJECTED"
        decision.reviewed_by = _ROLLBACK_ACTOR
        decision.reviewed_at = reviewed_at
        decision.review_reason = _ROLLBACK_REASON
        decision.reason_codes = reason_codes

    for conversation_id in conversation_ids:
        conversation = await session.get(models.Conversation, conversation_id)
        if conversation is None:
            raise RuntimeError("business_prompt_retirement_conversation_missing")
        state = await session.get(
            models.AutomationState,
            conversation_id,
            with_for_update=True,
        )
        if state is not None and state.state not in {"HUMAN_ACTIVE", "CLOSED"}:
            if state.state != "HANDOFF_PENDING":
                state.state = "HANDOFF_PENDING"
                state.state_version += 1
                state.state_changed_reason = _ROLLBACK_REASON
            work = await ensure_open_human_work_item(
                session,
                tenant_id=conversation.tenant_id,
                conversation_id=conversation_id,
                reason_code=_ROLLBACK_REASON,
            )
            await ensure_handoff_notification_intent(session, work=work)
        session.add(
            models.AuditLog(
                tenant_id=conversation.tenant_id,
                category="release_safety",
                actor=_ROLLBACK_ACTOR,
                action="RETIRE_REPLY_BUSINESS_PROMPT_WORK",
                subject_type="conversation",
                subject_id=str(conversation_id),
                detail={"reason_code": _ROLLBACK_REASON},
            )
        )

    await session.flush()
    remaining_count = await session.scalar(
        select(models.OutboxMessage.id)
        .join(models.ReplyDecision, _decision_outbox_link())
        .where(
            _has_business_prompt_provenance(),
            models.OutboxMessage.status.in_(
                (*_RETIREABLE_OUTBOX_STATUSES, "SENDING")
            ),
        )
        .limit(1)
    )
    if remaining_count is not None:
        raise RuntimeError("business_prompt_retirement_incomplete")

    return BusinessPromptRetirementReport(
        conversations=len(conversation_ids),
        outboxes_cancelled=len(outboxes),
        drafts_rejected=len(drafts),
    )
