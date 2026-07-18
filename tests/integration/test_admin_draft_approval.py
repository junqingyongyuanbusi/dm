import uuid

import httpx
from sqlalchemy import insert, select

from apps.api.main import create_app
from social_reply.infrastructure.database import models
from social_reply.infrastructure.secret_crypto import encrypt_secret_bundle


async def _login(client: httpx.AsyncClient) -> str:
    await client.get("/admin/login")
    csrf = client.cookies["reply_admin_csrf"]
    await client.post(
        "/admin/login",
        data={"csrf_token": csrf, "username": "admin", "password": "test-admin-password"},
    )
    return csrf


async def test_admin_approved_draft_can_send_while_conversation_remains_draft_only(
    session, monkeypatch
):
    account_id, contact_id, conversation_id, message_id, decision_id = (
        uuid.uuid4() for _ in range(5)
    )
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            brand_id="b1",
            platform="telegram",
            name="bot",
            public_id="draft-bot",
            credential_bundle=encrypt_secret_bundle({"bot_token": "token"}),
            config={"delivery_mode": "direct"},
            capability={"dm": True, "max_text_length": 4096},
            status="active",
        )
    )
    await session.execute(
        insert(models.Contact).values(
            id=contact_id,
            platform="telegram",
            platform_account_id=account_id,
            external_user_id="user",
        )
    )
    await session.execute(
        insert(models.Conversation).values(
            id=conversation_id,
            brand_id="b1",
            platform="telegram",
            platform_account_id=account_id,
            contact_id=contact_id,
            conversation_key=f"telegram:{account_id}:user",
        )
    )
    await session.execute(
        insert(models.AutomationState).values(
            conversation_id=conversation_id, state="BOT_DRAFT_ONLY", state_version=1
        )
    )
    await session.execute(
        insert(models.Message).values(
            id=message_id,
            conversation_id=conversation_id,
            direction="inbound",
            sender_type="contact",
            text="hello",
            reply_target={"chat_id": 123},
        )
    )
    await session.execute(
        insert(models.ReplyDecision).values(
            id=decision_id,
            conversation_id=conversation_id,
            message_id=message_id,
            tenant_id="default",
            action="draft",
            reply_text="approved reply",
            reason_codes=[],
            source="rule",
            prompt_version="v1",
            state_version_at_decision=1,
        )
    )
    await session.commit()

    sent = []

    class Sender:
        async def send_text(self, *, target, text):
            sent.append((target, text))
            return "platform-1"

        async def aclose(self):
            return None

    async def get_sender(_account_id):
        return Sender()

    monkeypatch.setattr(
        "social_reply.application.message_delivery.outbox.get_platform_sender", get_sender
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        csrf = await _login(client)
        response = await client.post(
            f"/admin/decisions/{decision_id}/approve", data={"csrf_token": csrf}
        )
    assert response.status_code == 303
    assert sent == [({"chat_id": 123}, "approved reply")]
    outbox = (await session.execute(select(models.OutboxMessage))).scalar_one()
    assert outbox.status == "SENT"
    assert outbox.payload["approval"] == "admin"
