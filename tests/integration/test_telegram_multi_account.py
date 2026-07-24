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


async def test_two_telegram_accounts_route_and_send_with_isolated_credentials(session, monkeypatch):
    sent: dict[str, list[dict]] = {"bot-a": [], "bot-b": []}
    accounts = []
    for name in ("bot-a", "bot-b"):
        account_id = uuid.uuid4()
        await session.execute(
            insert(models.PlatformAccount).values(
                id=account_id,
                tenant_id=name,
                brand_id="brand",
                platform="telegram",
                name=name,
                external_account_id=f"external-{name}",
                public_id=name,
                credential_bundle=encrypt_secret_bundle({"bot_token": f"token-{name}"}),
                webhook_secret_bundle=encrypt_secret_bundle({"secret": f"secret-{name}"}),
                config={"delivery_mode": "direct"},
                capability={"dm": True, "max_text_length": 4096},
                automation_default="BOT_ACTIVE",
                status="active",
            )
        )
        accounts.append((name, account_id))
    await session.commit()

    monkeypatch.setenv("KNOWLEDGE_RETRIEVAL_ENABLED", "false")
    get_settings.cache_clear()
    registry._senders.clear()

    for name, account_id in accounts:

        def make_handler(account_name):
            def handler(request: httpx.Request) -> httpx.Response:
                sent[account_name].append(__import__("json").loads(request.content))
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "result": {"message_id": 100 if account_name == "bot-a" else 200},
                    },
                )

            return handler

        registry._senders[("telegram", account_id, 1, 0)] = TelegramClient(
            token=f"token-{name}",
            transport=httpx.MockTransport(make_handler(name)),
        )

    from apps.api.main import create_app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        for index, (name, _) in enumerate(accounts, start=1):
            response = await client.post(
                f"/webhooks/telegram/{name}",
                headers={"X-Telegram-Bot-Api-Secret-Token": f"secret-{name}"},
                json={
                    "update_id": index,
                    "message": {
                        "message_id": index,
                        "date": 1784180000,
                        "from": {"id": 9},
                        "chat": {"id": 77, "type": "private"},
                        "text": "hello",
                    },
                },
            )
            assert response.status_code == 200

    outboxes = (
        (
            await session.execute(
                select(models.OutboxMessage).order_by(models.OutboxMessage.tenant_id)
            )
        )
        .scalars()
        .all()
    )
    assert [(row.tenant_id, row.platform_message_id) for row in outboxes] == [
        ("bot-a", "100"),
        ("bot-b", "200"),
    ]
    assert sent["bot-a"][0]["chat_id"] == 77
    assert sent["bot-b"][0]["chat_id"] == 77

    for _name, account_id in accounts:
        sender = registry._senders.pop(("telegram", account_id, 1, 0))
        await sender.aclose()
    get_settings.cache_clear()
