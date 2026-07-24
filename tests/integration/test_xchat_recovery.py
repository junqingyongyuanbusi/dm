import uuid

import pytest
from sqlalchemy import func, insert, select

from social_reply.application.event_ingestion import xchat_recovery, xchat_webhook
from social_reply.application.event_ingestion.direct import ingest_canonical_event
from social_reply.domain.messages.canonical import CanonicalEvent
from social_reply.infrastructure.database import models
from social_reply.infrastructure.secret_crypto import encrypt_secret_bundle

pytestmark = pytest.mark.integration


async def _insert_x_account(session, *, account_id: uuid.UUID, external_id: str) -> None:
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            tenant_id="default",
            brand_id="default",
            platform="x",
            name=external_id,
            external_account_id=external_id,
            public_id=f"x_{external_id}",
            credential_bundle=encrypt_secret_bundle(
                {
                    "consumer_key": "ck",
                    "consumer_secret": "cs",
                    "access_token": "at",
                    "access_token_secret": "ats",
                    "xchat_private_keys_b64": "private",
                    "xchat_signing_key_version": "7",
                }
            ),
            config={"xchat_key_state": "READY", "xchat_registered": True},
            capability={"dm": True, "x_chat": True, "mentions": True},
            automation_default="BOT_ACTIVE",
            status="active",
        )
    )


async def test_xchat_raw_event_claim_is_account_scoped_and_single_use(session, migrated_db):
    account_id = uuid.uuid4()
    other_account_id = uuid.uuid4()
    raw_event_id = uuid.uuid4()
    await _insert_x_account(session, account_id=account_id, external_id="bot-1")
    await _insert_x_account(session, account_id=other_account_id, external_id="bot-2")
    payload = {"data": {"event_type": "chat.received", "event_uuid": "event-1"}}
    await session.execute(
        insert(models.RawEvent).values(
            id=raw_event_id,
            tenant_id="default",
            platform_account_id=account_id,
            source="x",
            ingress_kind="webhook",
            event_namespace="x.activity.chat_received",
            payload=payload,
            headers={},
            context={},
            processing_status="XCHAT_DECRYPTION_PENDING",
        )
    )
    await session.commit()

    assert await xchat_webhook._claim(raw_event_id, other_account_id, "default") is None
    claim = await xchat_webhook._claim(
        raw_event_id,
        account_id,
        "default",
    )
    assert claim is not None
    assert claim.payload == payload
    assert (
        await xchat_webhook._claim(
            raw_event_id,
            account_id,
            "default",
        )
        is None
    )

    session.expire_all()
    raw = await session.get(models.RawEvent, raw_event_id)
    assert raw.processing_status == "XCHAT_PROCESSING"


async def test_pin_recovery_requeues_only_matching_account_events(
    session,
    migrated_db,
    monkeypatch,
):
    account_id = uuid.uuid4()
    other_account_id = uuid.uuid4()
    await _insert_x_account(session, account_id=account_id, external_id="bot-1")
    await _insert_x_account(session, account_id=other_account_id, external_id="bot-2")
    matching = [uuid.uuid4(), uuid.uuid4()]
    for raw_event_id, owner, status in (
        (matching[0], account_id, "XCHAT_KEY_RECOVERY_REQUIRED"),
        (matching[1], account_id, "XCHAT_DECRYPT_FAILED"),
        (uuid.uuid4(), other_account_id, "XCHAT_KEY_RECOVERY_REQUIRED"),
        (uuid.uuid4(), account_id, "PROCESSED"),
    ):
        await session.execute(
            insert(models.RawEvent).values(
                id=raw_event_id,
                tenant_id="default",
                platform_account_id=owner,
                source="x",
                ingress_kind="webhook",
                event_namespace="x.activity.chat_received",
                payload={"data": {"event_type": "chat.received"}},
                headers={},
                context={},
                processing_status=status,
            )
        )
    await session.commit()

    dispatched = []

    async def fake_dispatch(actor, *args, **kwargs):
        dispatched.append(args)

    monkeypatch.setattr(xchat_recovery, "dispatch_actor", fake_dispatch)
    replayed = await xchat_recovery.replay_xchat_raw_events(
        account_id,
        include_permanent=True,
    )

    assert set(replayed) == {str(value) for value in matching}
    assert {args[0] for args in dispatched} == {str(value) for value in matching}
    assert {args[1] for args in dispatched} == {str(account_id)}

    rows = dict(
        (
            await session.execute(
                select(models.RawEvent.id, models.RawEvent.processing_status).where(
                    models.RawEvent.id.in_(matching)
                )
            )
        ).all()
    )
    assert rows[matching[0]] == "XCHAT_KEY_RECOVERY_REQUIRED"
    assert rows[matching[1]] == "XCHAT_RETRYABLE_ERROR"


async def test_expired_xchat_worker_claim_is_recovered(session, migrated_db):
    from datetime import UTC, datetime, timedelta

    account_id = uuid.uuid4()
    raw_event_id = uuid.uuid4()
    await _insert_x_account(session, account_id=account_id, external_id="bot-1")
    await session.execute(
        insert(models.RawEvent).values(
            id=raw_event_id,
            tenant_id="default",
            platform_account_id=account_id,
            source="x",
            ingress_kind="webhook",
            event_namespace="x.activity.chat_received",
            payload={"data": {"event_type": "chat.received"}},
            headers={},
            context={},
            processing_status="XCHAT_PROCESSING",
            processing_claim_token=uuid.uuid4(),
            processing_claim_expires_at=datetime.now(UTC) - timedelta(minutes=1),
            processing_attempt_count=1,
        )
    )
    await session.commit()

    assert await xchat_recovery._recover_expired_claims() == [str(raw_event_id)]

    session.expire_all()
    raw = await session.get(models.RawEvent, raw_event_id)
    assert raw.processing_status == "XCHAT_RETRYABLE_ERROR"
    assert raw.processing_error_code == "XCHAT_WORKER_LEASE_EXPIRED"


async def test_replay_dispatch_failure_keeps_event_recoverable(
    session,
    migrated_db,
    monkeypatch,
):
    account_id = uuid.uuid4()
    raw_event_id = uuid.uuid4()
    await _insert_x_account(session, account_id=account_id, external_id="bot-1")
    await session.execute(
        insert(models.RawEvent).values(
            id=raw_event_id,
            tenant_id="default",
            platform_account_id=account_id,
            source="x",
            ingress_kind="webhook",
            event_namespace="x.activity.chat_received",
            payload={"data": {"event_type": "chat.received"}},
            headers={},
            context={},
            processing_status="XCHAT_KEY_RECOVERY_REQUIRED",
        )
    )
    await session.commit()

    async def fail_dispatch(actor, *args, **kwargs):
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(xchat_recovery, "dispatch_actor", fail_dispatch)
    assert await xchat_recovery.replay_xchat_raw_events(account_id) == []

    session.expire_all()
    raw = await session.get(models.RawEvent, raw_event_id)
    assert raw.processing_status == "XCHAT_KEY_RECOVERY_REQUIRED"
    assert raw.processing_error_code == "XCHAT_DISPATCH_FAILED"
    assert raw.processing_next_attempt_at


async def test_stale_xchat_worker_cannot_overwrite_new_claim_or_ingest(
    session,
    migrated_db,
):
    account_id = uuid.uuid4()
    raw_event_id = uuid.uuid4()
    await _insert_x_account(session, account_id=account_id, external_id="bot-1")
    await session.execute(
        insert(models.RawEvent).values(
            id=raw_event_id,
            tenant_id="default",
            platform_account_id=account_id,
            source="x",
            ingress_kind="webhook",
            event_namespace="x.activity.chat_received",
            payload={"data": {"event_type": "chat.received"}},
            headers={},
            context={},
            processing_status="XCHAT_PROCESSING",
            processing_claim_token=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        )
    )
    await session.commit()

    await xchat_webhook._mark(
        raw_event_id,
        "old-claim",
        "XCHAT_DECRYPT_FAILED",
    )
    stale_ingest = await ingest_canonical_event(
        CanonicalEvent(
            platform="x",
            platform_account_key=str(account_id),
            external_event_id="message-1",
            external_user_id="user-1",
            conversation_key=f"x_chat:{account_id}:bot-1:user-1",
            text="hello",
            reply_target={"kind": "x_chat", "conversation_id": "bot-1:user-1"},
        ),
        raw_event_id=raw_event_id,
        raw_event_claim_token="old-claim",
    )

    assert stale_ingest is None
    session.expire_all()
    raw = await session.get(models.RawEvent, raw_event_id)
    assert raw.processing_status == "XCHAT_PROCESSING"
    assert str(raw.processing_claim_token) == "11111111-1111-1111-1111-111111111111"
    assert await session.scalar(select(func.count()).select_from(models.NormalizedEvent)) == 0


async def test_unowned_xchat_event_requires_matching_tenant_source_and_target(
    session,
    migrated_db,
):
    account_id = uuid.uuid4()
    raw_event_id = uuid.uuid4()
    await _insert_x_account(session, account_id=account_id, external_id="bot-1")
    await session.execute(
        insert(models.RawEvent).values(
            id=raw_event_id,
            tenant_id="tenant-b",
            platform_account_id=None,
            source="x",
            ingress_kind="webhook",
            event_namespace="x.activity.chat_received",
            payload={
                "data": {
                    "event_type": "chat.received",
                    "filter": {"user_id": "bot-1"},
                }
            },
            headers={},
            context={},
            processing_status="XCHAT_DECRYPTION_PENDING",
        )
    )
    await session.commit()

    assert await xchat_webhook._claim(raw_event_id, account_id, "default") is None
    session.expire_all()
    raw = await session.get(models.RawEvent, raw_event_id)
    assert raw.platform_account_id is None
    assert raw.tenant_id == "tenant-b"

    null_tenant_id = uuid.uuid4()
    await session.execute(
        insert(models.RawEvent).values(
            id=null_tenant_id,
            tenant_id=None,
            platform_account_id=None,
            source="x",
            ingress_kind="webhook",
            event_namespace="x.activity.chat_received",
            payload={
                "data": {
                    "event_type": "chat.received",
                    "filter": {"user_id": "bot-1"},
                }
            },
            headers={},
            context={},
            processing_status="XCHAT_DECRYPTION_PENDING",
        )
    )
    await session.commit()
    assert await xchat_webhook._claim(null_tenant_id, account_id, "default") is None
