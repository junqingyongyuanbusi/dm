import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.application.handoff_notifications.cards import (
    HandoffCardSnapshot,
    render_handoff_card,
)
from social_reply.infrastructure.database import models
from social_reply.shared.config import get_settings


async def render_current_handoff_card(
    session: AsyncSession,
    *,
    intent: models.HandoffNotificationIntent,
    conversation: models.Conversation,
    work: models.HumanWorkItem,
    state: models.AutomationState,
) -> dict[str, object]:
    customer_account = await session.get(models.PlatformAccount, conversation.platform_account_id)
    contact = await session.get(models.Contact, conversation.contact_id)
    latest_message = await session.scalar(
        select(models.Message.text)
        .where(
            models.Message.conversation_id == conversation.id,
            models.Message.direction == "inbound",
        )
        .order_by(models.Message.history_seq.desc())
        .limit(1)
    )
    if (
        customer_account is None
        or customer_account.tenant_id != intent.tenant_id
        or contact is None
        or contact.tenant_id != intent.tenant_id
    ):
        raise ValueError("handoff_card_scope_mismatch")
    assigned_actor = work.assigned_actor
    if assigned_actor and assigned_actor.startswith("feishu_operator:"):
        try:
            operator_id = uuid.UUID(assigned_actor.removeprefix("feishu_operator:"))
            operator = await session.get(models.FeishuHandoffOperator, operator_id)
        except (TypeError, ValueError):
            operator = None
        if operator is not None and operator.tenant_id == intent.tenant_id:
            assigned_actor = operator.display_name or assigned_actor
    settings = get_settings()
    snapshot = HandoffCardSnapshot(
        notification_public_id=str(intent.public_id),
        action_nonce=str(intent.action_nonce),
        work_version=work.version,
        card_revision=intent.desired_revision,
        card_state=intent.desired_card_state,
        platform=conversation.platform,
        account_name=customer_account.name,
        channel_type=conversation.channel_type,
        contact_label=contact.display_name or contact.external_user_id,
        reason_code=work.reason_code,
        latest_message=latest_message or "",
        work_created_at=work.created_at,
        due_at=work.due_at,
        rendered_at=datetime.now(UTC),
        assigned_actor=assigned_actor,
        claimed_at=work.claimed_at,
        resolved_at=work.resolved_at,
        restored_automation_state=state.state if work.status == "RESOLVED" else None,
        conversation_url=(
            f"{settings.public_base_url.rstrip('/')}/admin/conversations/{conversation.id}"
        ),
    )
    return render_handoff_card(snapshot)
