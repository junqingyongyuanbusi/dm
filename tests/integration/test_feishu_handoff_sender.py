import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import insert, update

from social_reply.application.handoff_notifications import sender as sender_module
from social_reply.application.handoff_notifications import sweep as sweep_module
from social_reply.application.handoff_notifications.sender import deliver_handoff_notification
from social_reply.application.handoff_notifications.service import (
    ensure_handoff_notification_intent,
)
from social_reply.connectors.feishu.client import FeishuClient
from social_reply.infrastructure.database import models

pytestmark = pytest.mark.integration


def _settings(**updates):
    return SimpleNamespace(
        feishu_enabled=True,
        feishu_handoff_notifications_enabled=True,
        feishu_handoff_sender_lease_seconds=30,
        feishu_handoff_max_attempts=8,
        public_base_url="https://reply.example.com",
        **updates,
    )


async def _seed(session):
    customer_account_id = uuid.uuid4()
    feishu_account_id = uuid.uuid4()
    contact_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    message_id = uuid.uuid4()
    work_id = uuid.uuid4()
    config_id = uuid.uuid4()
    await session.execute(
        insert(models.PlatformAccount).values(
            id=customer_account_id,
            tenant_id="default",
            brand_id="b1",
            platform="telegram",
            name="Customer channel",
            config={"delivery_mode": "direct"},
            capability={"dm": True, "max_text_length": 4096},
            status="active",
        )
    )
    await session.execute(
        insert(models.PlatformAccount).values(
            id=feishu_account_id,
            tenant_id="default",
            brand_id="b1",
            platform="feishu",
            name="Support Bot",
            external_account_id="cli_12345678",
            config={"feishu_health_status": "READY"},
            capability={"dm": True, "mentions": True, "max_text_length": 4000},
            status="active",
        )
    )
    await session.execute(
        insert(models.Contact).values(
            id=contact_id,
            tenant_id="default",
            platform="telegram",
            platform_account_id=customer_account_id,
            external_user_id="customer-1",
            display_name="Customer",
        )
    )
    await session.execute(
        insert(models.Conversation).values(
            id=conversation_id,
            tenant_id="default",
            brand_id="b1",
            platform="telegram",
            platform_account_id=customer_account_id,
            contact_id=contact_id,
            conversation_key=f"telegram:{conversation_id}",
            channel_type="dm",
        )
    )
    await session.execute(
        insert(models.AutomationState).values(
            conversation_id=conversation_id,
            state="HANDOFF_PENDING",
            state_version=2,
            state_changed_reason="RISK_WORD",
        )
    )
    await session.execute(
        insert(models.Message).values(
            id=message_id,
            conversation_id=conversation_id,
            direction="inbound",
            sender_type="contact",
            text="I need a human",
            reply_target={"chat_id": "customer-1"},
            occurred_at=datetime.now(UTC),
        )
    )
    await session.execute(
        insert(models.HumanWorkItem).values(
            id=work_id,
            tenant_id="default",
            conversation_id=conversation_id,
            status="WAITING",
            reason_code="RISK_WORD",
            due_at=datetime.now(UTC) + timedelta(minutes=30),
            version=1,
        )
    )
    await session.execute(
        insert(models.TenantFeishuHandoffConfig).values(
            id=config_id,
            tenant_id="default",
            feishu_platform_account_id=feishu_account_id,
            destination_chat_id="oc_support",
            enabled=True,
            config_version=1,
        )
    )
    await session.flush()
    work = await session.get(models.HumanWorkItem, work_id)
    intent = await ensure_handoff_notification_intent(session, work=work)
    await session.commit()
    return intent.id, work_id


async def test_sender_creates_card_and_fences_success(session, monkeypatch):
    intent_id, _work_id = await _seed(session)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "tenant", "expire": 7200},
            )
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_card"}})

    client = FeishuClient(
        app_id="cli_12345678",
        app_secret="app-secret",
        transport=httpx.MockTransport(handler),
    )

    async def sender(_account_id):
        return client

    monkeypatch.setattr(sender_module, "get_settings", lambda: _settings())
    monkeypatch.setattr(sender_module, "get_platform_sender", sender)

    assert await deliver_handoff_notification(str(intent_id)) == "SYNCED"
    await client.aclose()

    session.expire_all()
    intent = await session.get(models.HandoffNotificationIntent, intent_id)
    assert intent.status == "SYNCED"
    assert intent.provider_message_id == "om_card"
    assert intent.delivered_revision == intent.desired_revision == 1
    assert intent.claim_token is None
    assert any(request.url.params.get("receive_id_type") == "chat_id" for request in requests)


async def test_sender_marks_ambiguous_card_create_for_review(session, monkeypatch):
    intent_id, _work_id = await _seed(session)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "tenant", "expire": 7200},
            )
        return httpx.Response(503, json={"code": 230999})

    client = FeishuClient(
        app_id="cli_12345678",
        app_secret="app-secret",
        transport=httpx.MockTransport(handler),
    )

    async def sender(_account_id):
        return client

    monkeypatch.setattr(sender_module, "get_settings", lambda: _settings())
    monkeypatch.setattr(sender_module, "get_platform_sender", sender)

    assert await deliver_handoff_notification(str(intent_id)) == "NEEDS_REVIEW"
    await client.aclose()

    session.expire_all()
    intent = await session.get(models.HandoffNotificationIntent, intent_id)
    assert intent.status == "NEEDS_REVIEW"
    assert intent.last_error_code == "AMBIGUOUS_CARD_CREATE"
    assert intent.provider_message_id is None


async def test_sender_cancels_resolved_work_before_first_card_create(session, monkeypatch):
    intent_id, work_id = await _seed(session)
    await session.execute(
        update(models.HumanWorkItem)
        .where(models.HumanWorkItem.id == work_id)
        .values(status="RESOLVED", resolved_at=datetime.now(UTC), version=2)
    )
    await session.commit()
    monkeypatch.setattr(sender_module, "get_settings", lambda: _settings())

    assert await deliver_handoff_notification(str(intent_id)) == "CANCELLED_BEFORE_CREATE"
    session.expire_all()
    intent = await session.get(models.HandoffNotificationIntent, intent_id)
    assert intent.status == "CANCELLED"
    assert intent.desired_card_state == "RESOLVED"


async def test_sweep_recovers_stale_card_update_without_creating_second_card(session, monkeypatch):
    intent_id, _work_id = await _seed(session)
    await session.execute(
        update(models.HandoffNotificationIntent)
        .where(models.HandoffNotificationIntent.id == intent_id)
        .values(
            status="SENDING",
            provider_message_id="om_existing",
            claim_token=uuid.uuid4(),
            claim_expires_at=datetime.now(UTC) - timedelta(seconds=1),
            sending_revision=1,
            attempt_count=1,
        )
    )
    await session.commit()
    dispatched: list[uuid.UUID] = []

    async def dispatch(target_id):
        dispatched.append(target_id)

    monkeypatch.setattr(sweep_module, "get_settings", lambda: _settings())
    monkeypatch.setattr(sweep_module, "dispatch_handoff_notification", dispatch)

    assert await sweep_module.sweep_handoff_notifications() == [intent_id]
    session.expire_all()
    intent = await session.get(models.HandoffNotificationIntent, intent_id)
    assert intent.status == "FAILED"
    assert intent.last_error_code == "STALE_CARD_UPDATE"
    assert intent.claim_token is None
    assert dispatched == [intent_id]
