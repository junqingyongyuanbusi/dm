import uuid

import pytest
from sqlalchemy import func, insert, select

from social_reply.application.event_ingestion.processor import process_raw_event
from social_reply.infrastructure.database import models

pytestmark = pytest.mark.integration


def _payload(**o):
    p = {"event": "message_created", "id": 55, "content": "请问怎么改邮箱",
         "message_type": "incoming", "private": False,
         "created_at": "2026-07-14T10:00:00Z",
         "sender": {"id": 9, "type": "contact"},
         "conversation": {"id": 77, "inbox_id": 101, "status": "pending"},
         "account": {"id": 1}}
    p.update(o)
    return p


async def _seed_account(session, automation_default="BOT_ACTIVE"):
    aid = uuid.uuid4()
    await session.execute(insert(models.PlatformAccount).values(
        id=aid, brand_id="b1", platform="telegram", name="a", chatwoot_inbox_id=101,
        automation_default=automation_default))
    await session.commit()
    return aid


async def _seed_raw(session, payload):
    r = (await session.execute(insert(models.RawEvent).values(
        source="chatwoot", payload=payload).returning(models.RawEvent.id))).scalar_one()
    await session.commit()
    return str(r)


async def _count(session, model):
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


async def test_bot_active_inbound_produces_pending_outbox(session):
    await _seed_account(session, "BOT_ACTIVE")
    await process_raw_event(await _seed_raw(session, _payload()))
    assert await _count(session, models.ReplyDecision) == 1
    ob = (await session.execute(select(models.OutboxMessage))).scalar_one()
    assert ob.status == "PENDING" and ob.message_type == "text"


async def test_draft_only_inbound_produces_private_note_outbox(session):
    await _seed_account(session, "BOT_DRAFT_ONLY")
    await process_raw_event(await _seed_raw(session, _payload()))
    ob = (await session.execute(select(models.OutboxMessage))).scalar_one()
    assert ob.message_type == "private_note"


async def test_risk_word_inbound_handoff_no_outbox(session):
    await _seed_account(session, "BOT_ACTIVE")
    await process_raw_event(await _seed_raw(session, _payload(content="我要起诉，无法出金")))
    assert await _count(session, models.OutboxMessage) == 0
    dec = (await session.execute(select(models.ReplyDecision))).scalar_one()
    assert dec.action == "handoff"
    st = (await session.execute(select(models.AutomationState))).scalar_one()
    assert st.state == "HANDOFF_PENDING"


async def test_agent_reply_does_not_trigger_decision(session):
    # 坐席公开回复只触发 HUMAN_ACTIVE，不产生决策/outbox
    await _seed_account(session, "BOT_ACTIVE")
    await process_raw_event(await _seed_raw(session, _payload()))  # 先建会话 + 1 decision
    await process_raw_event(await _seed_raw(session, _payload(
        id=56, message_type="outgoing", sender={"id": 3, "type": "user"})))
    assert await _count(session, models.ReplyDecision) == 1  # 仍是 1，坐席回复不决策
