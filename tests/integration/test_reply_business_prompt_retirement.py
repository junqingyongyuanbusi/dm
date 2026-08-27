import uuid

import pytest
from sqlalchemy import insert, select

from social_reply.application.reply_decision import business_prompt_retirement
from social_reply.application.reply_decision.business_prompt_retirement import (
    retire_business_prompt_work_for_rollback,
)
from social_reply.domain.automation.state_machine import ensure_state
from social_reply.infrastructure.database import models
from social_reply.shared.config import get_settings

pytestmark = pytest.mark.integration


async def test_retirement_cancels_public_work_and_rejects_pending_drafts(
    session,
    monkeypatch,
) -> None:
    settings = get_settings().model_copy(update={"reply_business_prompt_enabled": False})
    monkeypatch.setattr(business_prompt_retirement, "get_settings", lambda: settings)
    account_id = uuid.uuid4()
    contact_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    public_message_id = uuid.uuid4()
    draft_message_id = uuid.uuid4()
    public_outbox_id = uuid.uuid4()
    draft_note_outbox_id = uuid.uuid4()
    prompt_version_id = uuid.uuid4()

    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            tenant_id="default",
            brand_id="b1",
            platform="telegram",
            name="retirement-test",
        )
    )
    await session.execute(
        insert(models.Contact).values(
            id=contact_id,
            tenant_id="default",
            platform="telegram",
            platform_account_id=account_id,
            external_user_id="retirement-user",
        )
    )
    await session.execute(
        insert(models.Conversation).values(
            id=conversation_id,
            tenant_id="default",
            brand_id="b1",
            platform="telegram",
            platform_account_id=account_id,
            contact_id=contact_id,
            conversation_key=f"telegram:retirement:{conversation_id}",
            decision_generation=2,
        )
    )
    await ensure_state(session, conversation_id, "BOT_ACTIVE")
    await session.execute(
        insert(models.Message),
        [
            {
                "id": public_message_id,
                "conversation_id": conversation_id,
                "direction": "inbound",
                "sender_type": "contact",
                "text": "public",
                "decision_generation": 1,
            },
            {
                "id": draft_message_id,
                "conversation_id": conversation_id,
                "direction": "inbound",
                "sender_type": "contact",
                "text": "draft",
                "decision_generation": 2,
            },
        ],
    )
    await session.execute(
        insert(models.ReplyBusinessPromptVersion).values(
            id=prompt_version_id,
            tenant_id="default",
            brand_id="b1",
            revision=1,
            content="Keep replies concise.",
            content_hash="a" * 64,
            created_by="user:admin",
        )
    )
    await session.execute(
        insert(models.OutboxMessage),
        [
            {
                "id": public_outbox_id,
                "tenant_id": "default",
                "conversation_id": conversation_id,
                "platform_account_id": account_id,
                "destination_type": "telegram_dm",
                "destination_id": "telegram:retirement-user",
                "message_type": "text",
                "payload": {"text": "Prompt-derived reply", "target": {"chat_id": "1"}},
                "reply_to_message_id": public_message_id,
                "idempotency_key": str(public_outbox_id),
                "status": "PENDING",
            },
            {
                "id": draft_note_outbox_id,
                "tenant_id": "default",
                "conversation_id": conversation_id,
                "platform_account_id": account_id,
                "destination_type": "chatwoot_conversation",
                "destination_id": "chatwoot:retirement",
                "message_type": "private_note",
                "payload": {"text": "Pending Prompt-derived draft"},
                "reply_to_message_id": draft_message_id,
                "idempotency_key": str(draft_note_outbox_id),
                "status": "PENDING",
            },
        ],
    )
    await session.execute(
        insert(models.ReplyDecision),
        [
            {
                "id": uuid.uuid4(),
                "tenant_id": "default",
                "conversation_id": conversation_id,
                "message_id": public_message_id,
                "action": "auto_reply",
                "reply_text": "Prompt-derived reply",
                "source": "llm",
                "decision_generation": 1,
                "outbox_id": public_outbox_id,
                "reply_business_prompt_version_id": prompt_version_id,
                "reply_business_prompt_content_hash": "a" * 64,
            },
            {
                "id": uuid.uuid4(),
                "tenant_id": "default",
                "conversation_id": conversation_id,
                "message_id": draft_message_id,
                "action": "draft",
                "reply_text": "Pending Prompt-derived draft",
                "source": "llm",
                "decision_generation": 2,
                "outbox_id": draft_note_outbox_id,
                "reply_business_prompt_version_id": prompt_version_id,
                "reply_business_prompt_content_hash": "a" * 64,
            },
        ],
    )
    await session.commit()

    report = await retire_business_prompt_work_for_rollback(session)
    await session.commit()

    assert report.conversations == 1
    assert report.outboxes_cancelled == 2
    assert report.drafts_rejected == 1
    session.expire_all()
    outbox = await session.get(models.OutboxMessage, public_outbox_id)
    assert outbox.status == "CANCELLED"
    assert outbox.last_error_code == "REPLY_BUSINESS_PROMPT_ROLLBACK"
    draft_note = await session.get(models.OutboxMessage, draft_note_outbox_id)
    assert draft_note.status == "CANCELLED"
    assert draft_note.last_error_code == "REPLY_BUSINESS_PROMPT_ROLLBACK"
    draft = await session.scalar(
        select(models.ReplyDecision).where(
            models.ReplyDecision.message_id == draft_message_id
        )
    )
    assert draft.review_action == "REJECTED"
    assert draft.reviewed_by == "system:business-prompt-rollback"
    state = await session.get(models.AutomationState, conversation_id)
    assert state.state == "HANDOFF_PENDING"
    assert state.state_changed_reason == "REPLY_BUSINESS_PROMPT_ROLLBACK"
    assert await session.scalar(
        select(models.HumanWorkItem.id).where(
            models.HumanWorkItem.conversation_id == conversation_id,
            models.HumanWorkItem.status == "WAITING",
        )
    )
    assert await session.scalar(
        select(models.AuditLog.id).where(
            models.AuditLog.subject_id == str(conversation_id),
            models.AuditLog.action == "RETIRE_REPLY_BUSINESS_PROMPT_WORK",
        )
    )
