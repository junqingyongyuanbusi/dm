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


async def _seed_messenger_account(
    session,
    *,
    dm: bool = True,
    comments: bool = False,
    automation_default: str = "BOT_DRAFT_ONLY",
) -> tuple[uuid.UUID, uuid.UUID, str]:
    app_id, account_id = uuid.uuid4(), uuid.uuid4()
    app_public_id = f"meta_messenger_{uuid.uuid4().hex}"
    await session.execute(
        insert(models.PlatformApp).values(
            id=app_id,
            tenant_id="tenant-a",
            platform_family="meta",
            name="Messenger App",
            external_app_id="app-1",
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
            platform="facebook",
            platform_app_id=app_id,
            name="Support Page",
            external_account_id="page-1",
            public_id=f"fb_{uuid.uuid4().hex}",
            credential_bundle=encrypt_secret_bundle({"access_token": "page-token"}),
            config={
                "delivery_mode": "direct",
                "graph_base_url": "https://graph.facebook.com",
                "api_version": "v23.0",
                "instagram_login_mode": "facebook_login",
                "meta_desired_subscribed_fields": ["messages"],
                "meta_subscribed_fields": ["messages"],
                "meta_health_status": "READY",
            },
            capability={"dm": dm, "comments": comments, "max_text_length": 2000},
            automation_default=automation_default,
            status="active",
        )
    )
    await session.commit()
    return app_id, account_id, app_public_id


def _signed_body(payload: dict) -> tuple[bytes, str]:
    body = json.dumps(payload, separators=(",", ":")).encode()
    signature = "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
    return body, signature


async def test_signed_messenger_dm_is_account_scoped_and_becomes_draft(session):
    _app_id, account_id, app_public_id = await _seed_messenger_account(session)
    payload = {
        "object": "page",
        "entry": [
            {
                "id": "page-1",
                "messaging": [
                    {
                        "sender": {"id": "psid-1"},
                        "recipient": {"id": "page-1"},
                        "timestamp": 1_750_000_000_000,
                        "message": {"mid": "mid-1", "text": "hello"},
                    }
                ],
                "changes": [
                    {
                        "field": "feed",
                        "value": {
                            "id": "comment-1",
                            "from": {"id": "user-comment"},
                            "message": "comment must stay out of decisions",
                        },
                    }
                ],
            }
        ],
    }
    body, signature = _signed_body(payload)
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

    raw_events = list(
        (
            await session.execute(select(models.RawEvent).order_by(models.RawEvent.received_at))
        ).scalars()
    )
    assert len(raw_events) == 2
    request_evidence = next(row for row in raw_events if row.ingress_kind == "webhook_request")
    occurrence = next(row for row in raw_events if row.ingress_kind == "webhook")
    assert request_evidence.tenant_id == "tenant-a"
    assert request_evidence.platform_account_id is None
    assert request_evidence.payload == {"object": "page", "entry_count": 1}
    assert "hello" not in json.dumps(request_evidence.payload)
    assert occurrence.tenant_id == "tenant-a"
    assert occurrence.platform_account_id == account_id
    assert occurrence.event_namespace == "meta.facebook.entry"
    assert "changes" not in occurrence.payload["entry"][0]
    assert "comment must stay out of decisions" not in json.dumps(occurrence.payload)
    dispatch = occurrence.context["initial_dispatch"]
    assert len(dispatch["events"]) == 1
    event = dispatch["events"][0]
    assert event["platform"] == "facebook"
    assert event["platform_account_key"] == str(account_id)
    assert event["external_event_id"] == "mid-1"
    assert event["external_user_id"] == "psid-1"
    assert event["conversation_key"] == f"facebook_dm:{account_id}:psid-1"
    assert event["text"] == "hello"
    assert event["reply_target"] == {"kind": "dm", "recipient_id": "psid-1"}
    assert event["raw_payload"] == {}
    assert await session.scalar(select(func.count()).select_from(models.NormalizedEvent)) == 1
    contact = (await session.execute(select(models.Contact))).scalar_one()
    conversation = (await session.execute(select(models.Conversation))).scalar_one()
    message = (await session.execute(select(models.Message))).scalar_one()
    decision = (await session.execute(select(models.ReplyDecision))).scalar_one()
    assert contact.platform_account_id == account_id
    assert contact.external_user_id == "psid-1"
    assert conversation.tenant_id == "tenant-a"
    assert conversation.conversation_key == f"facebook_dm:{account_id}:psid-1"
    assert message.reply_target == {"kind": "dm", "recipient_id": "psid-1"}
    assert decision.action == "draft"
    assert await session.scalar(select(func.count()).select_from(models.OutboxMessage)) == 0


async def test_messenger_dm_capability_blocks_decision_but_keeps_occurrence(session):
    _app_id, account_id, app_public_id = await _seed_messenger_account(session, dm=False)
    payload = {
        "object": "page",
        "entry": [
            {
                "id": "page-1",
                "messaging": [
                    {
                        "sender": {"id": "psid-1"},
                        "recipient": {"id": "page-1"},
                        "message": {"mid": "mid-disabled", "text": "hello"},
                    }
                ],
            }
        ],
    }
    body, signature = _signed_body(payload)
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
    assert occurrence.processing_status == "IGNORED_AT_INGRESS"
    assert occurrence.context.get("initial_dispatch") is None
    assert await session.scalar(select(func.count()).select_from(models.NormalizedEvent)) == 0
    assert await session.scalar(select(func.count()).select_from(models.DecisionJob)) == 0


async def test_facebook_comment_webhook_sends_only_public_child_reply(session, monkeypatch):
    from social_reply.application.message_delivery import outbox as outbox_module

    _app_id, account_id, app_public_id = await _seed_messenger_account(
        session,
        comments=True,
        automation_default="BOT_ACTIVE",
    )
    sent: list[dict] = []

    class FakeSender:
        async def send_text(self, *, target: dict, text: str) -> str:
            sent.append({"target": target, "text": text})
            return "facebook-reply-1"

        async def aclose(self) -> None:
            return None

    async def fake_sender(_account_id):
        assert _account_id == account_id
        return FakeSender()

    monkeypatch.setattr(outbox_module, "get_platform_sender", fake_sender)
    payload = {
        "object": "page",
        "entry": [
            {
                "id": "page-1",
                "changes": [
                    {
                        "field": "feed",
                        "value": {
                            "item": "comment",
                            "verb": "add",
                            "comment_id": "comment-1",
                            "post_id": "post-1",
                            "from": {"id": "user-1"},
                            "message": "How can I order?",
                        },
                    }
                ],
            }
        ],
    }
    body, signature = _signed_body(payload)
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

    session.expire_all()
    conversation = (await session.execute(select(models.Conversation))).scalar_one()
    decision = (await session.execute(select(models.ReplyDecision))).scalar_one()
    outbox = (await session.execute(select(models.OutboxMessage))).scalar_one()
    assert conversation.channel_type == "comment"
    assert decision.action == "auto_reply"
    assert decision.reply_visibility == "public"
    assert "FACEBOOK_COMMENT_PUBLIC" in decision.reason_codes
    assert outbox.destination_type == "meta_public_comment"
    assert outbox.status == "SENT"
    assert outbox.platform_message_id == "facebook-reply-1"
    assert outbox.payload["target"] == {"kind": "comment", "comment_id": "comment-1"}
    assert sent == [{"target": outbox.payload["target"], "text": outbox.payload["text"]}]
    assert await session.scalar(
        select(func.count())
        .select_from(models.OutboxMessage)
        .where(models.OutboxMessage.destination_type == "meta_private_reply")
    ) == 0
