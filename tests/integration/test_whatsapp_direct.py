import hashlib
import hmac
import json
import uuid

import httpx
import pytest
from sqlalchemy import insert, select

from apps.api.main import create_app
from social_reply.connectors import registry
from social_reply.connectors.meta import router as meta_router
from social_reply.infrastructure.database import models
from social_reply.infrastructure.secret_crypto import encrypt_secret_bundle
from social_reply.shared.config import get_settings


@pytest.mark.parametrize(
    ("object_type", "settings_update", "error_code"),
    [
        (
            "page",
            {"facebook_messenger_enabled": False},
            "FACEBOOK_MESSENGER_DISABLED",
        ),
        (
            "instagram",
            {"instagram_messaging_enabled": False},
            "INSTAGRAM_MESSAGING_DISABLED",
        ),
        (
            "whatsapp_business_account",
            {"whatsapp_enabled": False},
            "WHATSAPP_DISABLED",
        ),
    ],
)
async def test_disabled_meta_family_is_audited_without_dispatch(
    session, monkeypatch, object_type, settings_update, error_code
):
    app_id = uuid.uuid4()
    await session.execute(
        insert(models.PlatformApp).values(
            id=app_id,
            tenant_id="tenant-a",
            platform_family="meta",
            name="Meta",
            external_app_id="app-disabled",
            public_id="meta_disabled_test",
            credential_bundle=encrypt_secret_bundle(
                {"app_secret": "secret", "verify_token": "verify"}
            ),
            config={},
            status="active",
        )
    )
    await session.commit()
    settings = get_settings().model_copy(update=settings_update)
    monkeypatch.setattr(meta_router, "get_settings", lambda: settings)

    async def unexpected_dispatch(*_args, **_kwargs):
        raise AssertionError("disabled family must not dispatch")

    monkeypatch.setattr(
        meta_router,
        "dispatch_initial_raw_event",
        unexpected_dispatch,
    )
    payload = {
        "object": object_type,
        "entry": [
            {
                "id": "account-sensitive",
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": "15551234567",
                                    "text": {"body": "private message body"},
                                }
                            ]
                        }
                    }
                ],
            }
        ],
    }
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/webhooks/meta/meta_disabled_test",
            content=body,
            headers={"X-Hub-Signature-256": signature, "Content-Type": "application/json"},
        )
    assert response.status_code == 200
    raw_event = (await session.execute(select(models.RawEvent))).scalar_one()
    assert raw_event.processing_status == "IGNORED_AT_INGRESS"
    assert raw_event.tenant_id == "tenant-a"
    assert raw_event.payload == {"object": object_type}
    assert raw_event.context == {
        "app_id": str(app_id),
        "app_public_id": "meta_disabled_test",
    }
    assert raw_event.headers["ingress_gate"] == error_code
    assert raw_event.headers["payload_sha256"] == hashlib.sha256(body).hexdigest()
    assert "private message body" not in str(raw_event.payload)
    assert "15551234567" not in str(raw_event.payload)


async def test_whatsapp_webhook_uses_shared_reply_core_and_sender(
    migrated_db, session, tmp_path, monkeypatch
):
    account_id = uuid.uuid4()
    app_id = uuid.uuid4()
    await session.execute(
        insert(models.PlatformApp).values(
            id=app_id,
            tenant_id="tenant-a",
            platform_family="meta",
            name="Meta",
            external_app_id="app-1",
            public_id="meta_whatsapp_test",
            credential_bundle=encrypt_secret_bundle(
                {"app_secret": "secret", "verify_token": "verify"}
            ),
            config={},
            status="active",
        )
    )
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            tenant_id="tenant-a",
            brand_id="brand-a",
            platform="whatsapp",
            platform_app_id=app_id,
            name="Support",
            external_account_id="phone-1",
            public_id="wa_test",
            credential_bundle=encrypt_secret_bundle({"access_token": "access"}),
            config={"delivery_mode": "direct"},
            capability={"dm": True, "session_messages": True, "max_text_length": 4096},
            automation_default="BOT_ACTIVE",
            status="active",
        )
    )
    await session.commit()

    sent = []

    class FakeSender:
        platform = "whatsapp"

        async def send_text(self, *, target, text):
            sent.append({"target": target, "text": text})
            return "wamid.outbound"

        async def aclose(self):
            return None

    registry._senders[("whatsapp", account_id, 1, 0)] = FakeSender()
    monkeypatch.setenv("LLM_PROVIDER", "stub")
    get_settings.cache_clear()
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba-1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": "phone-1"},
                            "messages": [
                                {
                                    "id": "wamid.inbound",
                                    "from": "15551234567",
                                    "text": {"body": "hello"},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/webhooks/meta/meta_whatsapp_test",
            content=body,
            headers={"X-Hub-Signature-256": signature, "Content-Type": "application/json"},
        )
    assert response.status_code == 200
    raw_event = (await session.execute(select(models.RawEvent))).scalar_one()
    assert raw_event.tenant_id == "tenant-a"
    dispatch = raw_event.context["initial_dispatch"]
    assert dispatch["version"] == 1
    assert dispatch["kind"] == "direct"
    assert dispatch["events"][0]["platform_account_key"] == str(account_id)
    assert dispatch["events"][0]["raw_payload"] == {}
    outbox = (await session.execute(select(models.OutboxMessage))).scalar_one()
    assert outbox.destination_type == "whatsapp_session_message"
    assert outbox.status == "SENT"
    assert outbox.platform_message_id == "wamid.outbound"
    assert sent[0]["target"]["to"] == "15551234567"
    registry._senders.pop(("whatsapp", account_id, 1), None)
    get_settings.cache_clear()
