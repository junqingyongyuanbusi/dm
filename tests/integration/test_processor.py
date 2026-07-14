import uuid

import pytest
from sqlalchemy import func, insert, select

from social_reply.application.event_ingestion.processor import process_raw_event
from social_reply.infrastructure.database import models

pytestmark = pytest.mark.integration


def _payload(**overrides) -> dict:
    p = {
        "event": "message_created", "id": 55, "content": "你好",
        "message_type": "incoming", "private": False,
        "created_at": "2026-07-14T10:00:00Z",
        "sender": {"id": 9, "type": "contact", "name": "张三"},
        "conversation": {"id": 77, "inbox_id": 101, "status": "pending"},
        "account": {"id": 1},
    }
    p.update(overrides)
    return p


async def _seed_account(session) -> uuid.UUID:
    account_id = uuid.uuid4()
    await session.execute(insert(models.PlatformAccount).values(
        id=account_id, brand_id="b1", platform="telegram", name="tg-main",
        chatwoot_inbox_id=101, automation_default="BOT_DRAFT_ONLY",
    ))
    await session.commit()
    return account_id


async def _seed_raw(session, payload: dict) -> str:
    result = await session.execute(
        insert(models.RawEvent).values(source="chatwoot", payload=payload)
        .returning(models.RawEvent.id)
    )
    raw_id = result.scalar_one()
    await session.commit()
    return str(raw_id)


async def _count(session, model) -> int:
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


async def test_inbound_user_message_full_chain(session):
    account_id = await _seed_account(session)
    raw_id = await _seed_raw(session, _payload())

    await process_raw_event(raw_id)

    conv = (await session.execute(select(models.Conversation))).scalar_one()
    assert conv.platform_account_id == account_id
    assert conv.conversation_key == f"telegram:{account_id}:9"
    mapping = (await session.execute(select(models.ConversationMapping))).scalar_one()
    assert mapping.chatwoot_conversation_id == 77
    msg = (await session.execute(select(models.Message))).scalar_one()
    assert msg.direction == "inbound" and msg.chatwoot_message_id == 55
    state = (await session.execute(select(models.AutomationState))).scalar_one()
    assert state.state == "BOT_DRAFT_ONLY"  # 账号默认草稿先行
    norm = (await session.execute(select(models.NormalizedEvent))).scalar_one()
    assert norm.external_event_id == "55"
    assert norm.event_type == "dm.message.created"
    raw = (await session.execute(select(models.RawEvent))).scalar_one()
    assert raw.processing_status == "PROCESSED"


async def test_duplicate_delivery_is_idempotent(session):
    await _seed_account(session)
    raw1 = await _seed_raw(session, _payload())
    raw2 = await _seed_raw(session, _payload())  # Chatwoot 重投同一条消息

    await process_raw_event(raw1)
    await process_raw_event(raw2)

    assert await _count(session, models.NormalizedEvent) == 1
    assert await _count(session, models.Message) == 1


async def test_agent_public_reply_flips_human_active(session):
    await _seed_account(session)
    await process_raw_event(await _seed_raw(session, _payload()))  # 先建会话
    agent_payload = _payload(
        id=56, message_type="outgoing", sender={"id": 3, "type": "user", "name": "客服A"},
    )
    await process_raw_event(await _seed_raw(session, agent_payload))

    state = (await session.execute(select(models.AutomationState))).scalar_one()
    assert state.state == "HUMAN_ACTIVE"
    assert state.state_version == 2
    msg_count = await _count(session, models.Message)
    assert msg_count == 2  # 坐席消息也落库（direction=outbound, sender_type=agent）


async def test_self_echo_via_outbox_is_skipped(session):
    await _seed_account(session)
    await process_raw_event(await _seed_raw(session, _payload()))
    conv = (await session.execute(select(models.Conversation))).scalar_one()
    account = (await session.execute(select(models.PlatformAccount))).scalar_one()
    # 模拟 Plan 2 的 Outbox 已发送记录：chatwoot_message_id=99
    await session.execute(insert(models.OutboxMessage).values(
        conversation_id=conv.id, platform_account_id=account.id,
        destination_type="chatwoot_conversation", destination_id="77",
        message_type="text", payload={}, idempotency_key="k1",
        status="SENT", chatwoot_message_id=99,
    ))
    await session.commit()

    echo_payload = _payload(
        id=99, message_type="outgoing", sender={"id": 2, "type": "agent_bot"},
    )
    await process_raw_event(await _seed_raw(session, echo_payload))

    state = (await session.execute(select(models.AutomationState))).scalar_one()
    assert state.state == "BOT_DRAFT_ONLY"  # 未被误翻转
    assert await _count(session, models.Message) == 1  # 回声不入消息表
    raw_rows = (await session.execute(select(models.RawEvent))).scalars().all()
    assert raw_rows[-1].processing_status == "SKIPPED_ECHO"


async def test_unknown_inbox_is_skipped(session):
    await _seed_account(session)
    raw_id = await _seed_raw(session, _payload(conversation={
        "id": 88, "inbox_id": 999, "status": "pending",
    }))
    await process_raw_event(raw_id)
    assert await _count(session, models.Conversation) == 0
    raw = (await session.execute(
        select(models.RawEvent).order_by(models.RawEvent.received_at.desc())
    )).scalars().first()
    assert raw.processing_status == "SKIPPED_UNKNOWN_INBOX"


async def test_malformed_payload_marked_parse_failed(session):
    await _seed_account(session)
    # 缺 conversation 键 → parse 抛 KeyError → PARSE_FAILED，不得留下任何业务行
    bad = {"event": "message_created", "id": 60, "message_type": "incoming",
           "private": False, "sender": {"id": 9, "type": "contact"}, "account": {"id": 1}}
    raw_id = await _seed_raw(session, bad)
    await process_raw_event(raw_id)
    raw = (await session.execute(
        select(models.RawEvent).where(models.RawEvent.id == uuid.UUID(raw_id))
    )).scalar_one()
    assert raw.processing_status == "PARSE_FAILED"
    assert await _count(session, models.Conversation) == 0
    assert await _count(session, models.NormalizedEvent) == 0


async def test_missing_account_id_marked_parse_failed(session):
    await _seed_account(session)
    raw_id = await _seed_raw(session, _payload(account={}))  # account.id 缺失 → 哨兵 0
    await process_raw_event(raw_id)
    raw = (await session.execute(
        select(models.RawEvent).where(models.RawEvent.id == uuid.UUID(raw_id))
    )).scalar_one()
    assert raw.processing_status == "PARSE_FAILED"
    assert await _count(session, models.ConversationMapping) == 0
