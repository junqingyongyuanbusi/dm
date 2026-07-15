import uuid

import pytest
from sqlalchemy import insert, select

from social_reply.domain.automation.state_machine import (
    AutomationStateEnum,
    can_transition,
    ensure_state,
    flip_to_human_active,
)
from social_reply.infrastructure.database import models

pytestmark = pytest.mark.integration


async def _seed_conv_with_outbox(session, ob_status="PENDING"):
    account_id, contact_id, conv_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await session.execute(insert(models.PlatformAccount).values(
        id=account_id, brand_id="b1", platform="telegram", name="a", chatwoot_inbox_id=101))
    await session.execute(insert(models.Contact).values(
        id=contact_id, platform="telegram", platform_account_id=account_id, external_user_id="9"))
    await session.execute(insert(models.Conversation).values(
        id=conv_id, brand_id="b1", platform="telegram", platform_account_id=account_id,
        contact_id=contact_id, conversation_key="telegram:x:9"))
    await ensure_state(session, conv_id, "BOT_ACTIVE")
    ob_id = uuid.uuid4()
    await session.execute(insert(models.OutboxMessage).values(
        id=ob_id, conversation_id=conv_id, platform_account_id=account_id,
        destination_type="chatwoot_conversation", destination_id="k", message_type="text",
        payload={"text": "在途回复"}, idempotency_key=str(ob_id), status=ob_status))
    await session.commit()
    return conv_id, ob_id


async def test_flip_cancels_pending_outbox(session):
    conv_id, ob_id = await _seed_conv_with_outbox(session, "PENDING")
    await flip_to_human_active(session, conv_id, "3", "agent_public_reply")
    await session.commit()
    ob = (await session.execute(
        select(models.OutboxMessage).where(models.OutboxMessage.id == ob_id))).scalar_one()
    assert ob.status == "CANCELLED"  # defense 3：接管取消在途发送


async def test_flip_cancels_failed_outbox(session):
    conv_id, ob_id = await _seed_conv_with_outbox(session, "FAILED")
    await flip_to_human_active(session, conv_id, "3", "agent_public_reply")
    await session.commit()
    ob = (await session.execute(
        select(models.OutboxMessage).where(models.OutboxMessage.id == ob_id))).scalar_one()
    assert ob.status == "CANCELLED"


async def test_flip_does_not_cancel_already_sent(session):
    conv_id, ob_id = await _seed_conv_with_outbox(session, "SENT")
    await flip_to_human_active(session, conv_id, "3", "agent_public_reply")
    await session.commit()
    ob = (await session.execute(
        select(models.OutboxMessage).where(models.OutboxMessage.id == ob_id))).scalar_one()
    assert ob.status == "SENT"  # 已发送的不动


def test_closed_can_reopen_to_human_active():
    # Plan 1 backlog：CLOSED 收到坐席公开消息应可重开为 HUMAN_ACTIVE
    assert can_transition(AutomationStateEnum.CLOSED, AutomationStateEnum.HUMAN_ACTIVE)
