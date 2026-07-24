import uuid

import httpx
import pytest
from sqlalchemy import insert, select

from social_reply.connectors import registry
from social_reply.connectors.telegram.client import TelegramClient
from social_reply.infrastructure.database import models
from social_reply.infrastructure.secret_crypto import encrypt_secret_bundle
from social_reply.shared.config import get_settings

pytestmark = pytest.mark.integration


async def test_telegram_webhook_direct_to_sent_outbox(session, monkeypatch):
    account_id = uuid.uuid4()
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            brand_id="b1",
            platform="telegram",
            name="direct-bot",
            external_account_id="bot-1",
            public_id="primary",
            credential_bundle=encrypt_secret_bundle({"bot_token": "token"}),
            webhook_secret_bundle=encrypt_secret_bundle({"secret": "secret-1"}),
            config={"delivery_mode": "direct", "api_base_url": "https://api.telegram.test"},
            capability={"dm": True, "max_text_length": 4096},
            chatwoot_inbox_id=None,
            automation_default="BOT_ACTIVE",
            status="active",
        )
    )
    await session.commit()

    monkeypatch.setenv("KNOWLEDGE_RETRIEVAL_ENABLED", "false")
    get_settings.cache_clear()
    registry._senders.clear()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 9001}})

    registry._senders[("telegram", account_id, 1)] = TelegramClient(
        token="token",
        api_base_url="https://api.telegram.test",
        transport=httpx.MockTransport(handler),
    )

    from apps.api.main import create_app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/webhooks/telegram/primary",
            headers={"X-Telegram-Bot-Api-Secret-Token": "secret-1"},
            json={
                "update_id": 100,
                "message": {
                    "message_id": 55,
                    "date": 1784180000,
                    "from": {"id": 9},
                    "chat": {"id": 77, "type": "private"},
                    "text": "hello",
                },
            },
        )
    assert response.status_code == 200
    raw_event = (await session.execute(select(models.RawEvent))).scalar_one()
    assert raw_event.source == "telegram"
    assert raw_event.tenant_id == "default"
    assert raw_event.platform_account_id == account_id
    assert raw_event.processing_status == "PROCESSED"
    dispatch = raw_event.context["initial_dispatch"]
    assert dispatch["version"] == 1
    assert dispatch["kind"] == "direct"
    assert dispatch["events"][0]["platform_account_key"] == str(account_id)
    assert dispatch["events"][0]["raw_payload"] == {}
    decision = (await session.execute(select(models.ReplyDecision))).scalar_one()
    outbox = (await session.execute(select(models.OutboxMessage))).scalar_one()
    assert decision.action == "auto_reply"
    assert outbox.destination_type == "telegram_dm"
    assert outbox.status == "SENT"
    assert outbox.platform_message_id == "9001"

    sender = registry._senders.pop(("telegram", account_id, 1))
    await sender.aclose()
    get_settings.cache_clear()
