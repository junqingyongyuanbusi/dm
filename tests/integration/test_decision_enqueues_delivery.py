import uuid

import dramatiq
import pytest
from sqlalchemy import insert, select

import social_reply.infrastructure.queue.broker  # noqa: F401  确保测试用 StubBroker
from social_reply.application.event_ingestion.processor import process_raw_event
from social_reply.infrastructure.database import models

pytestmark = pytest.mark.integration


def _payload(**o):
    p = {"event": "message_created", "id": 55, "content": "请问怎么改邮箱",
         "message_type": "incoming", "private": False,
         "created_at": "2026-07-15T10:00:00Z",
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


def _queued_actor_names(broker):
    # StubBroker 在 actor 声明前不会创建队列，缺队列视为空
    q = broker.queues.get("default")
    if q is None:
        return []
    return [dramatiq.Message.decode(m).actor_name for m in list(q.queue)]


async def test_bot_active_inbound_enqueues_delivery(session):
    await _seed_account(session, "BOT_ACTIVE")
    await process_raw_event(await _seed_raw(session, _payload()))
    ob = (await session.execute(select(models.OutboxMessage))).scalar_one()
    assert ob.status == "PENDING"
    broker = dramatiq.get_broker()
    names = _queued_actor_names(broker)
    assert names == ["deliver_outbox_message"]
    # 入队参数应为 outbox_id 字符串
    msg = dramatiq.Message.decode(list(broker.queues["default"].queue)[0])
    assert msg.args == (str(ob.id),)


async def test_handoff_no_outbox_no_enqueue(session):
    await _seed_account(session, "BOT_ACTIVE")
    await process_raw_event(await _seed_raw(session, _payload(content="我要起诉，无法出金")))
    ob = (await session.execute(select(models.OutboxMessage))).first()
    assert ob is None
    assert _queued_actor_names(dramatiq.get_broker()) == []
