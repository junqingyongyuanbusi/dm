"""端到端冒烟：签名 webhook 入站 → 处理决策 → outbox 投递 → Fake Chatwoot 收到回复"""

import hashlib
import hmac
import json
import time
import uuid

import httpx
import pytest
from sqlalchemy import insert, select

import social_reply.infrastructure.queue.broker  # noqa: F401  确保测试用 StubBroker
from social_reply.application.event_ingestion.processor import process_raw_event
from social_reply.application.message_delivery.outbox import deliver_outbox
from social_reply.connectors.chatwoot.client import get_chatwoot_client
from social_reply.infrastructure.database import models

pytestmark = pytest.mark.integration

SECRET = "change-me"


def _signed_headers(body: bytes) -> dict[str, str]:
    ts = int(time.time())
    digest = hmac.new(SECRET.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return {
        "X-Chatwoot-Signature": f"sha256={digest}",
        "X-Chatwoot-Timestamp": str(ts),
        "Content-Type": "application/json",
    }


@pytest.fixture
async def client(migrated_db, monkeypatch):
    monkeypatch.setenv("TESTING", "true")
    from social_reply.shared.config import get_settings
    get_settings.cache_clear()

    from apps.api.main import create_app
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _flush_fake_sent():
    # Fake 为模块级单例，测试间累积 .sent；断言前清空以隔离其他套件的发送记录
    get_chatwoot_client().sent.clear()
    yield


PAYLOAD = {
    "event": "message_created", "id": 55, "content": "请问怎么改邮箱",
    "message_type": "incoming", "private": False,
    "created_at": "2026-07-15T10:00:00Z",
    "sender": {"id": 9, "type": "contact"},
    "conversation": {"id": 77, "inbox_id": 101, "status": "pending"},
    "account": {"id": 1},
}


async def test_full_loop_inbound_to_chatwoot_reply(client, session):
    # 1. seed 平台账号（BOT_ACTIVE：允许自动公开回复）
    await session.execute(insert(models.PlatformAccount).values(
        id=uuid.uuid4(), brand_id="b1", platform="telegram", name="a",
        chatwoot_inbox_id=101, automation_default="BOT_ACTIVE"))
    await session.commit()

    # 2. 签名 webhook 入站 → RawEvent 落库
    body = json.dumps(PAYLOAD).encode()
    resp = await client.post("/webhooks/chatwoot", content=body, headers=_signed_headers(body))
    assert resp.status_code == 200
    raw = (await session.execute(select(models.RawEvent))).scalar_one()

    # 3. 手动驱动处理（tx1 入站 + tx2 决策 → PENDING outbox）
    await process_raw_event(str(raw.id))
    session.expire_all()
    ob = (await session.execute(select(models.OutboxMessage))).scalar_one()
    assert ob.status == "PENDING"

    # 4. Plan 1 入站应已自动建 ConversationMapping（deliver 靠它解析目标）
    mapping = (await session.execute(select(models.ConversationMapping).where(
        models.ConversationMapping.chatwoot_account_id == 1,
        models.ConversationMapping.chatwoot_conversation_id == 77))).scalar_one()
    assert mapping.conversation_id == ob.conversation_id

    # 5. 手动驱动投递 → Fake Chatwoot 收到回复
    ob_id = ob.id
    result = await deliver_outbox(str(ob_id))
    assert result == "SENT"
    # deliver 在自己的 session 更新，这里按列重查（避免刷新已过期的 ORM 实例）
    status, chatwoot_message_id = (await session.execute(
        select(models.OutboxMessage.status, models.OutboxMessage.chatwoot_message_id)
        .where(models.OutboxMessage.id == ob_id))).one()
    assert status == "SENT" and chatwoot_message_id is not None

    fake = get_chatwoot_client()
    sent = [s for s in fake.sent if s["conversation_id"] == 77]
    assert len(sent) == 1
    assert sent[0]["content"]
    assert sent[0]["private"] is False
    assert sent[0]["id"] == chatwoot_message_id
