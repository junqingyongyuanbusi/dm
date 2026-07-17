import hashlib
import hmac
import json
import time

import httpx
import pytest
from sqlalchemy import select

from social_reply.infrastructure.database import models

pytestmark = pytest.mark.integration

SECRET = "change-me"


def _signed_headers(body: bytes, ts: int | None = None) -> dict[str, str]:
    ts = ts or int(time.time())
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


PAYLOAD = {
    "event": "message_created",
    "id": 55,
    "content": "你好",
    "message_type": "incoming",
    "private": False,
    "created_at": "2026-07-14T10:00:00Z",
    "sender": {"id": 9, "type": "contact"},
    "conversation": {"id": 77, "inbox_id": 101, "status": "pending"},
    "account": {"id": 1},
}


async def test_bad_signature_rejected_and_nothing_stored(client, session):
    body = json.dumps(PAYLOAD).encode()
    resp = await client.post(
        "/webhooks/chatwoot",
        content=body,
        headers={**_signed_headers(body), "X-Chatwoot-Signature": "sha256=bad"},
    )
    assert resp.status_code == 401
    assert (await session.execute(select(models.RawEvent))).first() is None


async def test_valid_webhook_stores_raw_and_enqueues(client, session):
    import dramatiq

    broker = dramatiq.get_broker()
    body = json.dumps(PAYLOAD).encode()
    resp = await client.post("/webhooks/chatwoot", content=body, headers=_signed_headers(body))
    assert resp.status_code == 200
    raw = (await session.execute(select(models.RawEvent))).scalar_one()
    assert raw.source == "chatwoot"
    assert raw.payload["id"] == 55
    queue = broker.queues["default"]
    assert queue.qsize() == 1


async def test_conversation_event_recovers_embedded_latest_message(client, session):
    import dramatiq

    broker = dramatiq.get_broker()
    payload = {
        "event": "conversation_typing_off",
        "conversation": {
            "id": 77,
            "inbox_id": 101,
            "status": "open",
            "account": {"id": 1},
            "messages": [
                {
                    "id": 56,
                    "content": "True",
                    "message_type": 0,
                    "private": False,
                    "created_at": 1784184869,
                    "sender": {"id": 9, "type": "contact"},
                    "conversation_id": 77,
                    "inbox_id": 101,
                    "account_id": 1,
                }
            ],
        },
    }
    body = json.dumps(payload).encode()
    resp = await client.post("/webhooks/chatwoot", content=body, headers=_signed_headers(body))
    assert resp.status_code == 200
    raw = (await session.execute(select(models.RawEvent))).scalar_one()
    assert raw.processing_status == "PENDING"
    assert raw.payload["event"] == "message_created"
    assert raw.payload["id"] == 56
    assert raw.payload["content"] == "True"
    assert raw.payload["message_type"] == 0
    assert broker.queues["default"].qsize() == 1


async def test_top_level_conversation_event_recovers_embedded_message(client, session):
    import dramatiq

    broker = dramatiq.get_broker()
    payload = {
        "event": "conversation_updated",
        "id": 77,
        "inbox_id": 101,
        "status": "open",
        "account": {"id": 1},
        "messages": [
            {
                "id": 57,
                "content": "Help",
                "message_type": 0,
                "private": False,
                "created_at": 1784186112,
                "sender": {"id": 9, "type": "contact"},
                "conversation_id": 77,
                "inbox_id": 101,
                "account_id": 1,
            }
        ],
    }
    body = json.dumps(payload).encode()
    resp = await client.post("/webhooks/chatwoot", content=body, headers=_signed_headers(body))
    assert resp.status_code == 200
    raw = (await session.execute(select(models.RawEvent))).scalar_one()
    assert raw.processing_status == "PENDING"
    assert raw.payload["event"] == "message_created"
    assert raw.payload["id"] == 57
    assert raw.payload["content"] == "Help"
    assert broker.queues["default"].qsize() == 1


async def test_non_message_event_acknowledged_without_enqueue(client, session):
    import dramatiq

    broker = dramatiq.get_broker()
    payload = {**PAYLOAD, "event": "conversation_updated"}
    body = json.dumps(payload).encode()
    resp = await client.post("/webhooks/chatwoot", content=body, headers=_signed_headers(body))
    assert resp.status_code == 200
    # conversation_* 事件 Plan 2 处理，这里只存 raw 不入队
    raw = (await session.execute(select(models.RawEvent))).scalar_one()
    assert raw.processing_status == "IGNORED_AT_INGRESS"
    assert broker.queues["default"].qsize() == 0
