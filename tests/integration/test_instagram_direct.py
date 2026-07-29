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


async def _seed_instagram_account(
    session,
    *,
    login_mode: str,
) -> tuple[uuid.UUID, str]:
    app_id, account_id = uuid.uuid4(), uuid.uuid4()
    app_family = "instagram" if login_mode == "instagram_login" else "meta"
    app_public_id = f"{app_family}_{uuid.uuid4().hex}"
    config = {
        "delivery_mode": "direct",
        "graph_base_url": (
            "https://graph.instagram.com"
            if login_mode == "instagram_login"
            else "https://graph.facebook.com"
        ),
        "api_version": "v23.0",
        "instagram_login_mode": login_mode,
        "meta_desired_subscribed_fields": ["messages"],
        "meta_subscribed_fields": ["messages"],
        "meta_health_status": "READY",
    }
    if login_mode == "facebook_login":
        config["page_id"] = "page-1"
    await session.execute(
        insert(models.PlatformApp).values(
            id=app_id,
            tenant_id="tenant-a",
            platform_family=app_family,
            name="Instagram App",
            external_app_id=f"app-{uuid.uuid4().hex}",
            public_id=app_public_id,
            credential_bundle=encrypt_secret_bundle(
                {"app_secret": "app-secret", "verify_token": "verify-token"}
            ),
            config={"api_version": "v23.0"},
            status="active",
        )
    )
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            tenant_id="tenant-a",
            brand_id="brand-a",
            platform="instagram",
            platform_app_id=app_id,
            name="@shop",
            external_account_id="ig-1",
            public_id=f"ig_{uuid.uuid4().hex}",
            credential_bundle=encrypt_secret_bundle({"access_token": "account-token"}),
            config=config,
            capability={"dm": True, "comments": False, "max_text_length": 1000},
            automation_default="BOT_DRAFT_ONLY",
            status="active",
        )
    )
    await session.commit()
    return account_id, app_public_id


@pytest.mark.parametrize("login_mode", ["facebook_login", "instagram_login"])
async def test_signed_instagram_dm_reaches_draft_for_each_login_mode(session, login_mode):
    account_id, app_public_id = await _seed_instagram_account(
        session,
        login_mode=login_mode,
    )
    payload = {
        "object": "instagram",
        "entry": [
            {
                "id": "ig-1",
                "messaging": [
                    {
                        "sender": {"id": "igsid-valid"},
                        "recipient": {"id": "ig-1"},
                        "timestamp": 1_750_000_000_000,
                        "message": {"mid": f"mid-{login_mode}", "text": "hello"},
                    },
                    {
                        "sender": {"id": "ig-1"},
                        "recipient": {"id": "igsid-valid"},
                        "message": {"mid": "mid-echo", "text": "echo", "is_echo": True},
                    },
                    {
                        "sender": {"id": "igsid-wrong"},
                        "recipient": {"id": "other-account"},
                        "message": {"mid": "mid-wrong", "text": "wrong recipient"},
                    },
                ],
                "changes": [
                    {
                        "field": "comments",
                        "value": {
                            "id": "comment-1",
                            "from": {"id": "comment-user"},
                            "text": "comments are disabled",
                        },
                    }
                ],
            }
        ],
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    signature = "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://test",
    ) as client:
        response = await client.post(
            f"/webhooks/meta/{app_public_id}",
            content=body,
            headers={"X-Hub-Signature-256": signature},
        )
    assert response.status_code == 200

    occurrence = (
        await session.execute(
            select(models.RawEvent).where(models.RawEvent.platform_account_id == account_id)
        )
    ).scalar_one()
    assert occurrence.tenant_id == "tenant-a"
    assert occurrence.event_namespace == "meta.instagram.entry"
    assert "changes" not in occurrence.payload["entry"][0]
    assert "comments are disabled" not in json.dumps(occurrence.payload)
    event = occurrence.context["initial_dispatch"]["events"][0]
    assert event["platform"] == "instagram"
    assert event["platform_account_key"] == str(account_id)
    assert event["external_user_id"] == "igsid-valid"
    assert event["reply_target"] == {"kind": "dm", "recipient_id": "igsid-valid"}
    assert len(occurrence.context["initial_dispatch"]["events"]) == 1
    assert await session.scalar(select(func.count()).select_from(models.NormalizedEvent)) == 1
    contact = (await session.execute(select(models.Contact))).scalar_one()
    conversation = (await session.execute(select(models.Conversation))).scalar_one()
    decision = (await session.execute(select(models.ReplyDecision))).scalar_one()
    assert contact.external_user_id == "igsid-valid"
    assert contact.platform_account_id == account_id
    assert conversation.conversation_key == f"instagram_dm:{account_id}:igsid-valid"
    assert decision.action == "draft"
    assert await session.scalar(select(func.count()).select_from(models.OutboxMessage)) == 0
