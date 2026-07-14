import uuid

import pytest
from sqlalchemy import insert, select

from social_reply.domain.automation.state_machine import (
    ensure_state,
    flip_to_human_active,
)
from social_reply.infrastructure.database import models

pytestmark = pytest.mark.integration


async def _make_conversation(session) -> uuid.UUID:
    account_id, contact_id, conv_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await session.execute(insert(models.PlatformAccount).values(
        id=account_id, brand_id="b1", platform="telegram", name="acc", chatwoot_inbox_id=101,
    ))
    await session.execute(insert(models.Contact).values(
        id=contact_id, platform="telegram", platform_account_id=account_id,
        external_user_id="u1",
    ))
    await session.execute(insert(models.Conversation).values(
        id=conv_id, brand_id="b1", platform="telegram", platform_account_id=account_id,
        contact_id=contact_id, conversation_key=f"telegram:{account_id}:u1",
    ))
    return conv_id


async def test_ensure_state_is_idempotent(session):
    conv_id = await _make_conversation(session)
    await ensure_state(session, conv_id, "BOT_DRAFT_ONLY")
    await ensure_state(session, conv_id, "BOT_ACTIVE")  # 第二次不覆盖
    row = (await session.execute(
        select(models.AutomationState).where(
            models.AutomationState.conversation_id == conv_id)
    )).scalar_one()
    assert row.state == "BOT_DRAFT_ONLY"
    assert row.state_version == 1


async def test_flip_to_human_active_increments_version_and_audits(session):
    conv_id = await _make_conversation(session)
    await ensure_state(session, conv_id, "BOT_ACTIVE")
    assert await flip_to_human_active(session, conv_id, "3", "agent_public_reply") is True
    assert await flip_to_human_active(session, conv_id, "3", "agent_public_reply") is False
    row = (await session.execute(
        select(models.AutomationState).where(
            models.AutomationState.conversation_id == conv_id)
    )).scalar_one()
    assert row.state == "HUMAN_ACTIVE"
    assert row.state_version == 2
    audit_count = len((await session.execute(
        select(models.AuditLog).where(models.AuditLog.subject_id == str(conv_id))
    )).all())
    assert audit_count == 1
