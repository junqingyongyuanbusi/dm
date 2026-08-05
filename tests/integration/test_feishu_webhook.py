import asyncio
import base64
import hashlib
import json
import time
import uuid
from collections.abc import Callable

import httpx
import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from sqlalchemy import func, insert, select

from social_reply.application.event_ingestion import raw_recovery
from social_reply.infrastructure.database import models
from social_reply.infrastructure.secret_crypto import encrypt_secret_bundle
from social_reply.shared.config import get_settings

pytestmark = pytest.mark.integration

_ENCRYPT_KEY = "feishu-encrypt-key"
_VERIFY_TOKEN = "feishu-verify-token"
_APP_ID = "cli_fixture123"
_BOT_OPEN_ID = "ou_bot"


async def _seed_account(
    session,
    *,
    tenant_id="tenant-feishu",
    public_id="fs_primary",
    automation_default="BOT_DRAFT_ONLY",
):
    account_id = uuid.uuid4()
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            tenant_id=tenant_id,
            brand_id="brand-feishu",
            platform="feishu",
            name="Feishu bot",
            external_account_id=_APP_ID,
            public_id=public_id,
            credential_bundle=encrypt_secret_bundle(
                {"app_id": _APP_ID, "app_secret": "app-secret"}
            ),
            webhook_secret_bundle=encrypt_secret_bundle(
                {
                    "verification_token": _VERIFY_TOKEN,
                    "encrypt_key": _ENCRYPT_KEY,
                }
            ),
            config={
                "delivery_mode": "direct",
                "feishu_group_mode": "mentions_only",
                "feishu_bot_open_id": _BOT_OPEN_ID,
                "feishu_health_status": "READY",
            },
            capability={"dm": True, "mentions": True, "max_text_length": 4000},
            automation_default=automation_default,
            status="active",
        )
    )
    await session.commit()
    return account_id


def _event_payload(
    *,
    event_id="evt_1",
    message_id="om_1",
    app_id=_APP_ID,
    token=_VERIFY_TOKEN,
    chat_type="p2p",
    sender_open_id="ou_user",
    event_type="im.message.receive_v1",
    message_create_time=None,
    header_create_time=None,
):
    provider_now = str(int(time.time() * 1000))
    message = {
        "message_id": message_id,
        "chat_id": "oc_group" if chat_type == "group" else "oc_dm",
        "chat_type": chat_type,
        "message_type": "text",
        "create_time": provider_now if message_create_time is None else message_create_time,
        "content": json.dumps(
            {"text": "@_user_1 hello from Feishu" if chat_type == "group" else "hello"}
        ),
    }
    if chat_type == "group":
        message["mentions"] = [{"key": "@_user_1", "id": {"open_id": _BOT_OPEN_ID}}]
    return {
        "schema": "2.0",
        "token": "top-level-token-must-be-redacted",
        "header": {
            "event_id": event_id,
            "event_type": event_type,
            "create_time": provider_now if header_create_time is None else header_create_time,
            "token": token,
            "app_id": app_id,
            "tenant_key": "provider-tenant-key",
        },
        "event": {
            "sender": {
                "sender_id": {"open_id": sender_open_id},
                "sender_type": "user",
            },
            "message": message,
        },
    }


def _encrypted_request(payload: dict, *, signature_transform: Callable[[str], str] = lambda x: x):
    plaintext = json.dumps(payload, separators=(",", ":")).encode()
    padding_length = 16 - len(plaintext) % 16
    padded = plaintext + bytes([padding_length]) * padding_length
    iv = bytes(range(16))
    key = hashlib.sha256(_ENCRYPT_KEY.encode()).digest()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    encrypted = base64.b64encode(iv + encryptor.update(padded) + encryptor.finalize()).decode()
    body = json.dumps({"encrypt": encrypted}, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    nonce = "fixture-nonce"
    signature = hashlib.sha256(
        timestamp.encode() + nonce.encode() + _ENCRYPT_KEY.encode() + body
    ).hexdigest()
    return body, {
        "Content-Type": "application/json",
        "X-Lark-Request-Timestamp": timestamp,
        "X-Lark-Request-Nonce": nonce,
        "X-Lark-Signature": signature_transform(signature),
    }


def _app(*, feishu_enabled=True, handoff_notifications_enabled=False):
    from apps.api.main import create_app

    settings = get_settings().model_copy(
        update={
            "feishu_enabled": feishu_enabled,
            "feishu_handoff_notifications_enabled": handoff_notifications_enabled,
        }
    )
    return create_app(settings)


async def _post(app, path, *, content=None, headers=None, json_body=None):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post(path, content=content, headers=headers, json=json_body)


async def test_plaintext_and_encrypted_url_verification(session):
    await _seed_account(session)
    plaintext = await _post(
        _app(feishu_enabled=False),
        "/webhooks/feishu/fs_primary",
        json_body={
            "type": "url_verification",
            "challenge": "plain-challenge",
            "token": _VERIFY_TOKEN,
        },
    )
    encrypted_body, _ = _encrypted_request(
        {
            "type": "url_verification",
            "challenge": "encrypted-challenge",
            "token": _VERIFY_TOKEN,
        }
    )
    encrypted = await _post(
        _app(feishu_enabled=False),
        "/webhooks/feishu/fs_primary",
        content=encrypted_body,
        headers={"Content-Type": "application/json"},
    )
    assert plaintext.status_code == 200
    assert plaintext.json() == {"challenge": "plain-challenge"}
    assert encrypted.status_code == 200
    assert encrypted.json() == {"challenge": "encrypted-challenge"}
    assert (
        await session.execute(select(func.count()).select_from(models.RawEvent))
    ).scalar_one() == 0


async def test_plaintext_normal_event_and_bad_challenge_token_are_rejected(session):
    await _seed_account(session)
    plaintext_event = await _post(
        _app(),
        "/webhooks/feishu/fs_primary",
        json_body=_event_payload(),
    )
    bad_challenge = await _post(
        _app(),
        "/webhooks/feishu/fs_primary",
        json_body={
            "type": "url_verification",
            "challenge": "challenge",
            "token": "wrong",
        },
    )
    assert plaintext_event.status_code == bad_challenge.status_code == 401
    assert (
        await session.execute(select(func.count()).select_from(models.RawEvent))
    ).scalar_one() == 0


@pytest.mark.parametrize(
    "payload_change,header_change",
    [
        ({"token": "wrong"}, None),
        ({"app_id": "cli_other"}, None),
        (None, "signature"),
        (None, "partial_headers"),
        (None, "replay"),
    ],
)
async def test_bad_auth_stores_nothing(session, payload_change, header_change):
    await _seed_account(session)
    payload = _event_payload(
        token=(payload_change or {}).get("token", _VERIFY_TOKEN),
        app_id=(payload_change or {}).get("app_id", _APP_ID),
    )
    transform = (lambda _: "0" * 64) if header_change == "signature" else (lambda value: value)
    body, headers = _encrypted_request(payload, signature_transform=transform)
    if header_change == "partial_headers":
        headers.pop("X-Lark-Request-Nonce")
    if header_change == "replay":
        headers["X-Lark-Request-Timestamp"] = str(int(time.time()) - 301)
        headers["X-Lark-Signature"] = hashlib.sha256(
            headers["X-Lark-Request-Timestamp"].encode()
            + headers["X-Lark-Request-Nonce"].encode()
            + _ENCRYPT_KEY.encode()
            + body
        ).hexdigest()
    response = await _post(_app(), "/webhooks/feishu/fs_primary", content=body, headers=headers)
    assert response.status_code == 401
    assert (
        await session.execute(select(func.count()).select_from(models.RawEvent))
    ).scalar_one() == 0


async def test_body_size_unknown_and_disabled_account_fail_without_evidence(session):
    await _seed_account(session)
    oversized = await _post(
        _app(),
        "/webhooks/feishu/fs_primary",
        content=b"{" + b"x" * (1024 * 1024),
        headers={"Content-Type": "application/json"},
    )
    unknown = await _post(
        _app(),
        "/webhooks/feishu/fs_unknown",
        json_body={"type": "url_verification", "challenge": "x", "token": _VERIFY_TOKEN},
    )
    await session.execute(models.PlatformAccount.__table__.update().values(status="DISABLED"))
    await session.commit()
    disabled = await _post(
        _app(),
        "/webhooks/feishu/fs_primary",
        json_body={"type": "url_verification", "challenge": "x", "token": _VERIFY_TOKEN},
    )
    assert oversized.status_code == 413
    assert unknown.status_code == disabled.status_code == 404
    assert (
        await session.execute(select(func.count()).select_from(models.RawEvent))
    ).scalar_one() == 0


@pytest.mark.parametrize("event_id", [None, "", "   "])
async def test_normal_callback_requires_nonblank_header_event_id(session, event_id):
    await _seed_account(session)
    payload = _event_payload()
    payload["header"]["event_id"] = event_id
    body, headers = _encrypted_request(payload)

    response = await _post(_app(), "/webhooks/feishu/fs_primary", content=body, headers=headers)

    assert response.status_code == 401
    assert (
        await session.execute(select(func.count()).select_from(models.RawEvent))
    ).scalar_one() == 0


async def test_valid_callback_persists_redacted_scoped_evidence_and_dispatches(session):
    account_id = await _seed_account(session)
    body, headers = _encrypted_request(_event_payload())
    response = await _post(_app(), "/webhooks/feishu/fs_primary", content=body, headers=headers)
    assert response.status_code == 200
    raw = (await session.execute(select(models.RawEvent))).scalar_one()
    assert raw.tenant_id == "tenant-feishu"
    assert raw.platform_account_id == account_id
    assert raw.source == "feishu"
    assert raw.ingress_kind == "webhook"
    assert raw.event_namespace == "im.message.receive_v1"
    assert raw.external_event_id == "evt_1"
    assert raw.external_conversation_id == "oc_dm"
    assert raw.processing_status == "PROCESSED"
    assert "token" not in raw.payload
    assert "token" not in raw.payload["header"]
    assert raw.headers == {
        "signature_verified": True,
        "token_verified": True,
        "encrypted": True,
    }
    persisted_evidence = json.dumps(
        {"payload": raw.payload, "headers": raw.headers, "context": raw.context},
        sort_keys=True,
    )
    ciphertext = json.loads(body)["encrypt"]
    for forbidden in (
        _VERIFY_TOKEN,
        _ENCRYPT_KEY,
        "app-secret",
        ciphertext,
        headers["X-Lark-Signature"],
        headers["X-Lark-Request-Nonce"],
    ):
        assert forbidden not in persisted_evidence
    dispatch = raw.context["initial_dispatch"]
    assert dispatch["version"] == 1
    assert dispatch["kind"] == "direct"
    assert dispatch["events"][0]["raw_payload"] == {}
    message = (await session.execute(select(models.Message))).scalar_one()
    job = (await session.execute(select(models.DecisionJob))).scalar_one()
    assert message.platform_message_id == "om_1"
    assert job.message_id == message.id


async def test_feature_disabled_acks_and_persists_ignored_without_dispatch(session, monkeypatch):
    await _seed_account(session)
    dispatched = []

    async def capture(raw_event_id):
        dispatched.append(raw_event_id)

    monkeypatch.setattr("social_reply.connectors.feishu.router.dispatch_initial_raw_event", capture)
    body, headers = _encrypted_request(_event_payload())
    app = _app(feishu_enabled=False)
    first, duplicate = await asyncio.gather(
        _post(app, "/webhooks/feishu/fs_primary", content=body, headers=headers),
        _post(app, "/webhooks/feishu/fs_primary", content=body, headers=headers),
    )
    assert first.status_code == duplicate.status_code == 200
    raw = (await session.execute(select(models.RawEvent))).scalar_one()
    assert raw.processing_status == "IGNORED_AT_INGRESS"
    assert raw.headers["ingress_gate"] == "FEISHU_DISABLED"
    assert raw.context == {}
    assert dispatched == []
    assert (await session.execute(select(models.Message))).first() is None


async def test_unknown_event_is_acknowledged_and_stored_ignored(session):
    await _seed_account(session)
    body, headers = _encrypted_request(_event_payload(event_type="contact.user.created_v3"))
    response = await _post(_app(), "/webhooks/feishu/fs_primary", content=body, headers=headers)
    assert response.status_code == 200
    raw = (await session.execute(select(models.RawEvent))).scalar_one()
    assert raw.processing_status == "IGNORED_AT_INGRESS"
    assert raw.context == {}


async def test_raw_event_dedup_uses_header_event_id_not_message_id(session):
    await _seed_account(session)
    same_event_first = _event_payload(event_id="evt_same", message_id="om_first")
    same_event_second = _event_payload(event_id="evt_same", message_id="om_second")
    app = _app()
    for payload in (same_event_first, same_event_second):
        body, headers = _encrypted_request(payload)
        response = await _post(
            app,
            "/webhooks/feishu/fs_primary",
            content=body,
            headers=headers,
        )
        assert response.status_code == 200

    same_message_new_event = _event_payload(event_id="evt_new", message_id="om_first")
    body, headers = _encrypted_request(same_message_new_event)
    response = await _post(
        app,
        "/webhooks/feishu/fs_primary",
        content=body,
        headers=headers,
    )
    assert response.status_code == 200

    raw_events = list(
        (
            await session.execute(select(models.RawEvent).order_by(models.RawEvent.received_at))
        ).scalars()
    )
    assert [
        (raw.external_event_id, raw.payload["event"]["message"]["message_id"]) for raw in raw_events
    ] == [
        ("evt_same", "om_first"),
        ("evt_new", "om_first"),
    ]
    assert (
        await session.execute(select(func.count()).select_from(models.NormalizedEvent))
    ).scalar_one() == 1


async def test_duplicate_delivery_is_idempotent_downstream(session):
    await _seed_account(session)
    body, headers = _encrypted_request(_event_payload())
    app = _app()
    responses = await asyncio.gather(
        _post(app, "/webhooks/feishu/fs_primary", content=body, headers=headers),
        _post(app, "/webhooks/feishu/fs_primary", content=body, headers=headers),
    )
    assert [response.status_code for response in responses] == [200, 200]
    for model, expected in (
        (models.RawEvent, 1),
        (models.NormalizedEvent, 1),
        (models.Message, 1),
        (models.DecisionJob, 1),
    ):
        count = (await session.execute(select(func.count()).select_from(model))).scalar_one()
        assert count == expected


async def test_delayed_older_dispatch_is_recorded_without_superseding_newer_work(
    session, monkeypatch
):
    await _seed_account(session)
    app = _app()
    older_dispatch_started = asyncio.Event()
    release_older_dispatch = asyncio.Event()
    dispatch_calls = 0

    async def delay_first_dispatch(raw_event_id):
        nonlocal dispatch_calls
        dispatch_calls += 1
        if dispatch_calls == 1:
            older_dispatch_started.set()
            await release_older_dispatch.wait()
        return await raw_recovery.dispatch_initial_raw_event(raw_event_id)

    monkeypatch.setattr(
        "social_reply.connectors.feishu.router.dispatch_initial_raw_event",
        delay_first_dispatch,
    )
    older_body, older_headers = _encrypted_request(
        _event_payload(
            event_id="evt_older",
            message_id="om_older",
            message_create_time="1785729500000",
            header_create_time="1785730000000",
        )
    )
    newer_body, newer_headers = _encrypted_request(
        _event_payload(
            event_id="evt_newer",
            message_id="om_newer",
            message_create_time="1785729600000",
            header_create_time="1785720000000",
        )
    )

    older_request = asyncio.create_task(
        _post(
            app,
            "/webhooks/feishu/fs_primary",
            content=older_body,
            headers=older_headers,
        )
    )
    await older_dispatch_started.wait()
    newer_response = await _post(
        app,
        "/webhooks/feishu/fs_primary",
        content=newer_body,
        headers=newer_headers,
    )
    release_older_dispatch.set()
    older_response = await older_request

    assert newer_response.status_code == older_response.status_code == 200
    stale = (
        await session.execute(
            select(models.NormalizedEvent).where(
                models.NormalizedEvent.external_event_id == "om_older"
            )
        )
    ).scalar_one()
    conversation = (await session.execute(select(models.Conversation))).scalar_one()
    assert stale.conversation_id == conversation.id
    assert stale.message_id is None
    assert stale.event_metadata["disposition"] == "stale_provider_order"
    assert "token" not in json.dumps(stale.event_metadata)
    assert conversation.decision_generation == 1
    for model, expected in (
        (models.RawEvent, 2),
        (models.NormalizedEvent, 2),
        (models.Message, 1),
        (models.DecisionJob, 1),
        (models.OutboxMessage, 0),
    ):
        count = (await session.execute(select(func.count()).select_from(model))).scalar_one()
        assert count == expected


async def test_messages_in_provider_order_create_normal_work(session):
    await _seed_account(session)
    app = _app()
    for event_id, message_id, create_time in (
        ("evt_ordered_1", "om_ordered_1", "1785729500000"),
        ("evt_ordered_2", "om_ordered_2", "1785729600000"),
    ):
        body, headers = _encrypted_request(
            _event_payload(
                event_id=event_id,
                message_id=message_id,
                message_create_time=create_time,
            )
        )
        response = await _post(
            app,
            "/webhooks/feishu/fs_primary",
            content=body,
            headers=headers,
        )
        assert response.status_code == 200

    messages = list(
        (
            await session.execute(
                select(models.Message).order_by(models.Message.decision_generation)
            )
        ).scalars()
    )
    conversation = (await session.execute(select(models.Conversation))).scalar_one()
    assert [message.platform_message_id for message in messages] == [
        "om_ordered_1",
        "om_ordered_2",
    ]
    assert [message.decision_generation for message in messages] == [1, 2]
    assert conversation.decision_generation == 2
    assert (
        await session.execute(select(func.count()).select_from(models.DecisionJob))
    ).scalar_one() == 2


async def test_equal_message_create_time_is_not_stale(session):
    await _seed_account(session)
    app = _app()
    for event_id, message_id in (("evt_equal_1", "om_equal_1"), ("evt_equal_2", "om_equal_2")):
        body, headers = _encrypted_request(
            _event_payload(
                event_id=event_id,
                message_id=message_id,
                message_create_time="1785729600000",
            )
        )
        response = await _post(
            app,
            "/webhooks/feishu/fs_primary",
            content=body,
            headers=headers,
        )
        assert response.status_code == 200

    normalized = list((await session.execute(select(models.NormalizedEvent))).scalars())
    conversation = (await session.execute(select(models.Conversation))).scalar_one()
    assert len(normalized) == 2
    assert all("disposition" not in event.event_metadata for event in normalized)
    assert (
        await session.execute(select(func.count()).select_from(models.Message))
    ).scalar_one() == 2
    assert (
        await session.execute(select(func.count()).select_from(models.DecisionJob))
    ).scalar_one() == 2
    assert conversation.decision_generation == 2


async def test_raw_event_recovery_dispatches_committed_context(session, monkeypatch):
    await _seed_account(session)

    async def defer(_raw_event_id):
        return None

    monkeypatch.setattr("social_reply.connectors.feishu.router.dispatch_initial_raw_event", defer)
    body, headers = _encrypted_request(_event_payload())
    response = await _post(_app(), "/webhooks/feishu/fs_primary", content=body, headers=headers)
    raw = (await session.execute(select(models.RawEvent))).scalar_one()
    assert response.status_code == 200
    assert raw.processing_status == "PENDING"
    await raw_recovery.dispatch_initial_raw_event(raw.id)
    await session.refresh(raw)
    assert raw.processing_status == "PROCESSED"
    assert (
        await session.execute(select(models.Message))
    ).scalar_one().platform_message_id == "om_1"


@pytest.mark.parametrize("chat_type", ["p2p", "group"])
async def test_bot_active_feishu_ingress_reaches_sent_once(session, monkeypatch, chat_type):
    await _seed_account(session, automation_default="BOT_ACTIVE")
    calls = []

    class Sender:
        async def send_text(self, *, target, text):
            calls.append((target, text))
            return "om_provider_reply"

        async def aclose(self):
            return None

    async def get_sender(_account_id):
        return Sender()

    monkeypatch.setattr(
        "social_reply.application.message_delivery.outbox.get_platform_sender",
        get_sender,
    )
    settings = get_settings().model_copy(update={"feishu_enabled": True})
    monkeypatch.setattr(
        "social_reply.application.message_delivery.outbox.get_settings",
        lambda: settings,
    )
    body, headers = _encrypted_request(_event_payload(chat_type=chat_type))
    response = await _post(_app(), "/webhooks/feishu/fs_primary", content=body, headers=headers)
    assert response.status_code == 200
    assert len(calls) == 1
    outbox = (await session.execute(select(models.OutboxMessage))).scalar_one()
    assert outbox.status == "SENT"
    assert outbox.platform_message_id == "om_provider_reply"
    assert calls[0][0]["uuid"] == str(outbox.id)
    assert calls[0][0]["message_id"] == "om_1"


async def test_group_mention_reaches_decision_job_and_draft_without_outbound(session):
    await _seed_account(session)
    body, headers = _encrypted_request(_event_payload(chat_type="group"))
    response = await _post(_app(), "/webhooks/feishu/fs_primary", content=body, headers=headers)
    assert response.status_code == 200
    message = (await session.execute(select(models.Message))).scalar_one()
    job = (await session.execute(select(models.DecisionJob))).scalar_one()
    decision = (await session.execute(select(models.ReplyDecision))).scalar_one()
    assert message.text == "hello from Feishu"
    assert message.reply_target == {
        "kind": "mention",
        "message_id": "om_1",
        "chat_id": "oc_group",
        "chat_type": "group",
        "sender_open_id": "ou_user",
    }
    assert job.message_id == message.id
    assert decision.message_id == message.id
    assert decision.action == "draft"
    assert (await session.execute(select(models.OutboxMessage))).first() is None


async def _seed_card_work(session, feishu_account_id, *operator_open_ids):
    customer_account_id = uuid.uuid4()
    contact_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    message_id = uuid.uuid4()
    work_id = uuid.uuid4()
    config_id = uuid.uuid4()
    intent_id = uuid.uuid4()
    public_id = uuid.uuid4()
    action_nonce = uuid.uuid4()
    await session.execute(
        insert(models.PlatformAccount).values(
            id=customer_account_id,
            tenant_id="tenant-feishu",
            brand_id="brand-feishu",
            platform="telegram",
            name="Customer channel",
            config={"delivery_mode": "direct"},
            capability={"dm": True, "max_text_length": 4096},
            automation_default="BOT_ACTIVE",
            status="active",
        )
    )
    await session.execute(
        insert(models.Contact).values(
            id=contact_id,
            tenant_id="tenant-feishu",
            platform="telegram",
            platform_account_id=customer_account_id,
            external_user_id="customer-1",
            display_name="Customer",
        )
    )
    await session.execute(
        insert(models.Conversation).values(
            id=conversation_id,
            tenant_id="tenant-feishu",
            brand_id="brand-feishu",
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
        )
    )
    await session.execute(
        insert(models.HumanWorkItem).values(
            id=work_id,
            tenant_id="tenant-feishu",
            conversation_id=conversation_id,
            status="WAITING",
            reason_code="RISK_WORD",
            version=1,
        )
    )
    await session.execute(
        insert(models.TenantFeishuHandoffConfig).values(
            id=config_id,
            tenant_id="tenant-feishu",
            feishu_platform_account_id=feishu_account_id,
            destination_chat_id="oc_support",
            enabled=True,
            config_version=1,
        )
    )
    await session.execute(
        insert(models.HandoffNotificationIntent).values(
            id=intent_id,
            public_id=public_id,
            tenant_id="tenant-feishu",
            human_work_item_id=work_id,
            conversation_id=conversation_id,
            notification_config_id=config_id,
            config_version=1,
            feishu_platform_account_id=feishu_account_id,
            destination_chat_id="oc_support",
            provider_uuid=uuid.uuid4(),
            provider_message_id="om_card",
            status="SYNCED",
            desired_card_state="WAITING",
            desired_revision=1,
            delivered_revision=1,
            action_nonce=action_nonce,
            attempt_count=1,
        )
    )
    operator_ids = []
    for open_id in operator_open_ids:
        operator_id = uuid.uuid4()
        operator_ids.append(operator_id)
        await session.execute(
            insert(models.FeishuHandoffOperator).values(
                id=operator_id,
                tenant_id="tenant-feishu",
                feishu_platform_account_id=feishu_account_id,
                operator_open_id=open_id,
                display_name=f"Agent {open_id}",
                can_claim=True,
                can_resolve=True,
                status="ACTIVE",
            )
        )
    await session.commit()
    return intent_id, work_id, public_id, action_nonce, operator_ids


def _card_action_payload(
    *,
    event_id,
    operator_open_id,
    public_id,
    action_nonce,
    action,
    work_version,
    card_revision,
):
    return {
        "schema": "2.0",
        "header": {
            "event_id": event_id,
            "event_type": "card.action.trigger",
            "create_time": str(int(time.time() * 1000)),
            "token": _VERIFY_TOKEN,
            "app_id": _APP_ID,
            "tenant_key": "provider-tenant-key",
        },
        "event": {
            "operator": {"open_id": operator_open_id},
            "token": "card-update-token-must-not-be-stored",
            "open_message_id": "om_card",
            "action": {
                "tag": "button",
                "value": {
                    "contract_version": 1,
                    "notification_id": str(public_id),
                    "action": action,
                    "expected_work_version": work_version,
                    "expected_card_revision": card_revision,
                    "action_nonce": str(action_nonce),
                },
            },
        },
    }


async def test_card_action_claim_is_atomic_and_duplicate_event_is_idempotent(session):
    feishu_account_id = await _seed_account(session)
    intent_id, work_id, public_id, nonce, operator_ids = await _seed_card_work(
        session,
        feishu_account_id,
        "ou_agent",
    )
    payload = _card_action_payload(
        event_id="evt_card_claim",
        operator_open_id="ou_agent",
        public_id=public_id,
        action_nonce=nonce,
        action="claim",
        work_version=1,
        card_revision=1,
    )
    body, headers = _encrypted_request(payload)
    app = _app(handoff_notifications_enabled=True)

    first = await _post(app, "/webhooks/feishu/fs_primary", content=body, headers=headers)
    duplicate = await _post(app, "/webhooks/feishu/fs_primary", content=body, headers=headers)

    assert first.status_code == duplicate.status_code == 200
    assert first.json() == duplicate.json()
    assert first.json()["toast"]["type"] == "success"
    session.expire_all()
    work = await session.get(models.HumanWorkItem, work_id)
    intent = await session.get(models.HandoffNotificationIntent, intent_id)
    state = await session.get(models.AutomationState, work.conversation_id)
    assert work.status == "CLAIMED"
    assert work.version == 2
    assert work.assigned_actor == f"feishu_operator:{operator_ids[0]}"
    assert state.state == "HUMAN_ACTIVE"
    assert intent.status == "PENDING"
    assert intent.desired_card_state == "CLAIMED"
    assert intent.desired_revision == 2
    assert (
        await session.execute(select(func.count()).select_from(models.FeishuCardActionReceipt))
    ).scalar_one() == 1
    assert (
        await session.execute(select(func.count()).select_from(models.RawEvent))
    ).scalar_one() == 0


async def test_card_action_resolve_restores_account_policy_and_records_attestation(session):
    feishu_account_id = await _seed_account(session)
    intent_id, work_id, public_id, nonce, _operator_ids = await _seed_card_work(
        session,
        feishu_account_id,
        "ou_agent",
    )
    app = _app(handoff_notifications_enabled=True)
    claim_payload = _card_action_payload(
        event_id="evt_card_claim_2",
        operator_open_id="ou_agent",
        public_id=public_id,
        action_nonce=nonce,
        action="claim",
        work_version=1,
        card_revision=1,
    )
    claim_body, claim_headers = _encrypted_request(claim_payload)
    claim = await _post(
        app,
        "/webhooks/feishu/fs_primary/card-actions",
        content=claim_body,
        headers=claim_headers,
    )
    assert claim.json()["toast"]["type"] == "success"

    session.expire_all()
    claimed_work = await session.get(models.HumanWorkItem, work_id)
    claimed_intent = await session.get(models.HandoffNotificationIntent, intent_id)
    resolve_payload = _card_action_payload(
        event_id="evt_card_resolve",
        operator_open_id="ou_agent",
        public_id=public_id,
        action_nonce=claimed_intent.action_nonce,
        action="resolve",
        work_version=claimed_work.version,
        card_revision=claimed_intent.desired_revision,
    )
    resolve_body, resolve_headers = _encrypted_request(resolve_payload)
    resolved = await _post(
        app,
        "/webhooks/feishu/fs_primary/card-actions",
        content=resolve_body,
        headers=resolve_headers,
    )

    assert resolved.status_code == 200
    assert resolved.json()["toast"]["type"] == "success"
    session.expire_all()
    work = await session.get(models.HumanWorkItem, work_id)
    intent = await session.get(models.HandoffNotificationIntent, intent_id)
    state = await session.get(models.AutomationState, work.conversation_id)
    assert work.status == "RESOLVED"
    assert work.resolution_evidence == "FEISHU_OPERATOR_ATTESTED"
    assert work.resolved_actor == work.assigned_actor
    assert state.state == "BOT_ACTIVE"
    assert intent.status == "PENDING"
    assert intent.desired_card_state == "RESOLVED"
    assert intent.desired_revision == 3


async def test_card_action_rejects_unlisted_operator_without_mutating_work(session):
    feishu_account_id = await _seed_account(session)
    _intent_id, work_id, public_id, nonce, _operator_ids = await _seed_card_work(
        session,
        feishu_account_id,
    )
    payload = _card_action_payload(
        event_id="evt_card_unauthorized",
        operator_open_id="ou_unlisted",
        public_id=public_id,
        action_nonce=nonce,
        action="claim",
        work_version=1,
        card_revision=1,
    )
    body, headers = _encrypted_request(payload)
    response = await _post(
        _app(handoff_notifications_enabled=True),
        "/webhooks/feishu/fs_primary/card-actions",
        content=body,
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["toast"]["type"] == "error"
    session.expire_all()
    work = await session.get(models.HumanWorkItem, work_id)
    receipt = (await session.execute(select(models.FeishuCardActionReceipt))).scalar_one()
    assert work.status == "WAITING"
    assert receipt.outcome == "UNAUTHORIZED"


async def test_card_action_non_assignee_cannot_resolve_claimed_work(session):
    feishu_account_id = await _seed_account(session)
    intent_id, work_id, public_id, nonce, _operator_ids = await _seed_card_work(
        session,
        feishu_account_id,
        "ou_agent_1",
        "ou_agent_2",
    )
    app = _app(handoff_notifications_enabled=True)
    claim_payload = _card_action_payload(
        event_id="evt_card_claim_owner",
        operator_open_id="ou_agent_1",
        public_id=public_id,
        action_nonce=nonce,
        action="claim",
        work_version=1,
        card_revision=1,
    )
    claim_body, claim_headers = _encrypted_request(claim_payload)
    claim = await _post(
        app,
        "/webhooks/feishu/fs_primary/card-actions",
        content=claim_body,
        headers=claim_headers,
    )
    assert claim.json()["toast"]["type"] == "success"

    session.expire_all()
    work = await session.get(models.HumanWorkItem, work_id)
    intent = await session.get(models.HandoffNotificationIntent, intent_id)
    resolve_payload = _card_action_payload(
        event_id="evt_card_wrong_resolver",
        operator_open_id="ou_agent_2",
        public_id=public_id,
        action_nonce=intent.action_nonce,
        action="resolve",
        work_version=work.version,
        card_revision=intent.desired_revision,
    )
    resolve_body, resolve_headers = _encrypted_request(resolve_payload)
    resolve = await _post(
        app,
        "/webhooks/feishu/fs_primary/card-actions",
        content=resolve_body,
        headers=resolve_headers,
    )

    assert resolve.status_code == 200
    assert resolve.json()["toast"]["type"] == "warning"
    session.expire_all()
    work = await session.get(models.HumanWorkItem, work_id)
    state = await session.get(models.AutomationState, work.conversation_id)
    assert work.status == "CLAIMED"
    assert work.resolution_evidence is None
    assert state.state == "HUMAN_ACTIVE"


async def test_card_action_feature_off_returns_maintenance_without_mutation(session):
    feishu_account_id = await _seed_account(session)
    _intent_id, work_id, public_id, nonce, _operator_ids = await _seed_card_work(
        session,
        feishu_account_id,
        "ou_agent",
    )
    payload = _card_action_payload(
        event_id="evt_card_maintenance",
        operator_open_id="ou_agent",
        public_id=public_id,
        action_nonce=nonce,
        action="claim",
        work_version=1,
        card_revision=1,
    )
    body, headers = _encrypted_request(payload)
    response = await _post(
        _app(handoff_notifications_enabled=False),
        "/webhooks/feishu/fs_primary/card-actions",
        content=body,
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["toast"]["type"] == "warning"
    session.expire_all()
    work = await session.get(models.HumanWorkItem, work_id)
    receipt = (await session.execute(select(models.FeishuCardActionReceipt))).scalar_one()
    assert work.status == "WAITING"
    assert receipt.outcome == "MAINTENANCE"
