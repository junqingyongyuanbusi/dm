import base64
import hashlib
import hmac
import json
import uuid

import httpx
import pytest
from sqlalchemy import func, insert, select

from apps.api.main import create_app
from social_reply.infrastructure.database import models
from social_reply.infrastructure.secret_crypto import encrypt_secret_bundle

pytestmark = pytest.mark.integration


async def test_dm_received_activity_webhook_persists_owner_and_ingests_message(
    session,
    migrated_db,
):
    app_id = uuid.uuid4()
    account_id = uuid.uuid4()
    await session.execute(
        insert(models.PlatformApp).values(
            id=app_id,
            tenant_id="default",
            platform_family="x",
            name="X App",
            public_id="x_activity_test",
            credential_bundle=encrypt_secret_bundle(
                {"consumer_key": "ck", "consumer_secret": "webhook-secret"}
            ),
            config={},
            status="active",
        )
    )
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            tenant_id="default",
            brand_id="default",
            platform="x",
            platform_app_id=app_id,
            name="bot",
            external_account_id="bot-1",
            public_id="x_bot_test",
            credential_bundle=encrypt_secret_bundle(
                {
                    "consumer_key": "ck",
                    "consumer_secret": "webhook-secret",
                    "access_token": "at",
                    "access_token_secret": "ats",
                }
            ),
            config={},
            capability={"dm": True, "x_chat": False, "mentions": True},
            automation_default="BOT_ACTIVE",
            status="active",
        )
    )
    await session.commit()

    payload = {
        "data": {
            "event_type": "dm.received",
            "event_uuid": "activity-1",
            "filter": {"user_id": "bot-1"},
            "payload": {
                "id": "dm-1",
                "event_type": "MessageCreate",
                "sender_id": "user-1",
                "dm_conversation_id": "conversation-1",
                "text": "hello",
                "created_at": "2026-07-20T01:51:31Z",
            },
        }
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    signature = (
        "sha256="
        + base64.b64encode(hmac.new(b"webhook-secret", body, hashlib.sha256).digest()).decode()
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/webhooks/x/x_activity_test",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Twitter-Webhooks-Signature": signature,
            },
        )

    assert response.status_code == 200
    session.expire_all()
    raw = (
        await session.execute(
            select(models.RawEvent).where(models.RawEvent.external_event_id == "activity-1")
        )
    ).scalar_one()
    assert raw.tenant_id == "default"
    assert raw.platform_account_id == account_id
    assert raw.event_namespace == "x.activity.dm_received"
    assert raw.external_conversation_id == "conversation-1"
    assert raw.context["raw_body_sha256"] == hashlib.sha256(body).hexdigest()
    assert raw.processing_status in {
        "DECISION_PENDING",
        "DECISION_NEEDS_REVIEW",
        "PROCESSED",
    }

    normalized = (
        await session.execute(
            select(models.NormalizedEvent).where(models.NormalizedEvent.external_event_id == "dm-1")
        )
    ).scalar_one()
    assert normalized.raw_event_id == raw.id
    assert normalized.event_metadata["event_namespace"] == "x.activity.dm_received"
    assert await session.scalar(select(func.count()).select_from(models.Message)) == 1
