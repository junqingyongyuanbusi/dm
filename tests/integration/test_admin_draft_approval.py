import uuid

import httpx
import pytest
from sqlalchemy import insert, select, update

from apps.api.main import create_app
from social_reply.application.message_delivery import outbox as outbox_module
from social_reply.infrastructure.database import models
from social_reply.infrastructure.secret_crypto import encrypt_secret_bundle


async def _login(client: httpx.AsyncClient) -> str:
    await client.get("/admin/login")
    csrf = client.cookies["reply_admin_csrf"]
    await client.post(
        "/admin/login",
        data={
            "csrf_token": csrf,
            "username": "admin",
            "password": "test-admin-password",
        },
    )
    return csrf


class _OpenKillSwitch:
    async def is_disabled(self, brand_id, account_id, tenant_id="default"):
        return False


@pytest.mark.parametrize("automation_state", ["BOT_ACTIVE", "BOT_DRAFT_ONLY"])
async def test_admin_approved_draft_can_send_in_bot_modes(
    session, monkeypatch, automation_state
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
            decision_generation=1,
        )
    )
    await session.execute(
        insert(models.AutomationState).values(
            conversation_id=conversation_id, state=automation_state, state_version=1
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
            decision_generation=1,
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
            decision_generation=1,
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
    monkeypatch.setattr(
        outbox_module,
        "make_killswitch_checker",
        lambda: _OpenKillSwitch(),
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
        replay = await client.post(
            f"/admin/decisions/{decision_id}/approve", data={"csrf_token": csrf}
        )
        conflict = await client.post(
            f"/admin/decisions/{decision_id}/approve",
            data={"csrf_token": csrf, "final_reply_text": "conflicting reply"},
        )
        await session.execute(
            update(models.Conversation)
            .where(models.Conversation.id == conversation_id)
            .values(decision_generation=2)
        )
        await session.commit()
        stale_replay = await client.post(
            f"/admin/decisions/{decision_id}/approve", data={"csrf_token": csrf}
        )
    assert response.status_code == 303
    assert replay.status_code == 303, replay.text
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "draft_approval_conflict"
    assert stale_replay.status_code == 409
    assert stale_replay.json()["detail"] == "draft_stale_conversation_input"
    assert sent == [({"chat_id": 123}, "approved reply")]
    outbox = (await session.execute(select(models.OutboxMessage))).scalar_one()
    assert outbox.status == "SENT"
    assert outbox.payload["approval"] == "admin"
    approved_outbox_id = outbox.id
    session.expire_all()
    decision = await session.get(models.ReplyDecision, decision_id)
    assert decision.outbox_id is None
    assert decision.review_outbox_id == approved_outbox_id
    audits = list(
        (
            await session.execute(
                select(models.AuditLog).where(
                    models.AuditLog.action == "APPROVE_DRAFT",
                    models.AuditLog.subject_id == str(decision_id),
                )
            )
        ).scalars()
    )
    assert len(audits) == 1
