from sqlalchemy import and_, exists, func, or_, select

from social_reply.infrastructure.database import models


def reviewable_draft_condition():
    """Return the canonical SQL predicate for an actionable draft review."""
    original_text = func.nullif(
        func.btrim(models.ReplyDecision.original_reply_text, " \t\n\r\f\v"),
        "",
    )
    generated_text = func.nullif(
        func.btrim(models.ReplyDecision.reply_text, " \t\n\r\f\v"),
        "",
    )
    effective_draft_text = func.coalesce(original_text, generated_text, "")
    current_inbound_message_exists = exists(
        select(models.Message.id).where(
            models.Message.id == models.ReplyDecision.message_id,
            models.Message.conversation_id == models.ReplyDecision.conversation_id,
            models.Message.direction == "inbound",
            models.Message.decision_generation == models.ReplyDecision.decision_generation,
        )
    )
    return and_(
        models.ReplyDecision.action == "draft",
        or_(
            models.ReplyDecision.review_action.is_(None),
            models.ReplyDecision.review_action == "PENDING",
        ),
        models.ReplyDecision.review_outbox_id.is_(None),
        models.ReplyDecision.message_id.is_not(None),
        func.length(effective_draft_text) > 0,
        models.ReplyDecision.decision_generation.is_not(None),
        models.ReplyDecision.decision_generation
        == models.Conversation.decision_generation,
        current_inbound_message_exists,
    )
