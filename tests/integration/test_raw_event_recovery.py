import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, select, update

from social_reply.application.event_ingestion import direct as direct_ingestion
from social_reply.application.event_ingestion import raw_recovery
from social_reply.application.event_ingestion.direct_actors import (
    process_initial_direct_event,
)
from social_reply.application.reply_decision import jobs as decision_jobs
from social_reply.domain.messages.canonical import (
    CanonicalEvent,
    canonical_event_to_dict,
)
from social_reply.infrastructure.database import models

pytestmark = pytest.mark.integration


def _event(account_id: uuid.UUID, external_event_id: str = "event-1") -> dict:
    return canonical_event_to_dict(
        CanonicalEvent(
            platform="telegram",
            platform_account_key=str(account_id),
            external_event_id=external_event_id,
            external_user_id="user-1",
            conversation_key=f"telegram:{account_id}:chat-1",
            text="hello",
            external_conversation_id="chat-1",
            reply_target={"chat_id": "chat-1"},
            raw_payload={"private": "not duplicated into dispatch context"},
        )
    )


async def _seed_raw(
    session,
    *,
    source: str = "telegram",
    ingress_kind: str = "webhook",
    status: str = "PENDING",
    context: dict | None = None,
    tenant_id: str | None = "tenant-a",
    platform_account_id: uuid.UUID | None = None,
    payload: dict | None = None,
) -> uuid.UUID:
    raw_event_id = uuid.uuid4()
    await session.execute(
        insert(models.RawEvent).values(
            id=raw_event_id,
            tenant_id=tenant_id,
            platform_account_id=platform_account_id,
            source=source,
            ingress_kind=ingress_kind,
            payload=payload or {"evidence": True},
            headers={},
            context=context or {},
            processing_status=status,
        )
    )
    await session.commit()
    return raw_event_id


async def test_sweep_redispatches_persisted_work_after_queue_loss(session, monkeypatch):
    account_id = uuid.uuid4()
    raw_event_id = await _seed_raw(
        session,
        context=raw_recovery.direct_dispatch_context([_event(account_id)]),
    )
    dispatched = []

    async def capture(_actor, *args, inline=None):
        dispatched.append(args)

    monkeypatch.setattr(raw_recovery, "dispatch_actor", capture)
    assert await raw_recovery.sweep_initial_raw_events() == [str(raw_event_id)]
    session.expire_all()
    first = await session.get(models.RawEvent, raw_event_id)
    first_token = first.processing_claim_token
    assert first.processing_status == "PENDING"
    assert first.processing_attempt_count == 0
    assert first.processing_last_dispatched_at is not None
    assert dispatched == [(str(raw_event_id), str(first_token))]

    await session.execute(
        update(models.RawEvent)
        .where(models.RawEvent.id == raw_event_id)
        .values(processing_next_attempt_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    await session.commit()
    assert await raw_recovery.sweep_initial_raw_events() == [str(raw_event_id)]
    session.expire_all()
    second = await session.get(models.RawEvent, raw_event_id)
    assert second.processing_claim_token != first_token
    assert dispatched[-1] == (str(raw_event_id), str(second.processing_claim_token))


async def test_stale_dispatch_token_cannot_claim_replaced_reservation(session, monkeypatch):
    account_id = uuid.uuid4()
    raw_event_id = await _seed_raw(
        session,
        context=raw_recovery.direct_dispatch_context([_event(account_id)]),
    )
    first = await raw_recovery._reserve_specific(raw_event_id)
    assert first is not None
    first_token = first[1]
    await session.execute(
        update(models.RawEvent)
        .where(models.RawEvent.id == raw_event_id)
        .values(processing_next_attempt_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    await session.commit()
    second = await raw_recovery._reserve_specific(raw_event_id)
    assert second is not None
    second_token = second[1]

    ingested = []

    async def fake_ingest(event, *, raw_event_id, raw_event_claim_token):
        ingested.append((event.external_event_id, raw_event_claim_token))

    monkeypatch.setattr(direct_ingestion, "ingest_canonical_event", fake_ingest)
    await process_initial_direct_event(raw_event_id, first_token)
    assert ingested == []
    await process_initial_direct_event(raw_event_id, second_token)
    assert ingested == [("event-1", str(second_token))]
    session.expire_all()
    completed = await session.get(models.RawEvent, raw_event_id)
    assert completed.processing_status == "PROCESSED"
    assert completed.processing_claim_token is None
    assert completed.processing_attempt_count == 1


async def test_partial_direct_batch_retries_at_least_once(session, monkeypatch):
    account_id = uuid.uuid4()
    raw_event_id = await _seed_raw(
        session,
        context=raw_recovery.direct_dispatch_context(
            [_event(account_id, "event-1"), _event(account_id, "event-2")]
        ),
    )
    reserved = await raw_recovery._reserve_specific(raw_event_id)
    assert reserved is not None
    calls = []
    fail_once = True

    async def flaky_ingest(event, *, raw_event_id, raw_event_claim_token):
        nonlocal fail_once
        calls.append(event.external_event_id)
        if event.external_event_id == "event-2" and fail_once:
            fail_once = False
            raise RuntimeError("worker crashed between batch events")

    monkeypatch.setattr(direct_ingestion, "ingest_canonical_event", flaky_ingest)
    await process_initial_direct_event(raw_event_id, reserved[1])
    session.expire_all()
    retry = await session.get(models.RawEvent, raw_event_id)
    assert retry.processing_status == "INITIAL_DISPATCH_RETRY"
    assert retry.processing_attempt_count == 1

    await session.execute(
        update(models.RawEvent)
        .where(models.RawEvent.id == raw_event_id)
        .values(processing_next_attempt_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    await session.commit()
    reserved_again = await raw_recovery._reserve_specific(raw_event_id)
    assert reserved_again is not None
    await process_initial_direct_event(raw_event_id, reserved_again[1])
    assert calls == ["event-1", "event-2", "event-1", "event-2"]
    session.expire_all()
    completed = await session.get(models.RawEvent, raw_event_id)
    assert completed.processing_status == "PROCESSED"
    assert completed.processing_attempt_count == 2


async def test_dispatch_send_failure_releases_reservation(session, monkeypatch):
    account_id = uuid.uuid4()
    raw_event_id = await _seed_raw(
        session,
        context=raw_recovery.direct_dispatch_context([_event(account_id)]),
    )

    async def fail_dispatch(*_args, **_kwargs):
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(raw_recovery, "dispatch_actor", fail_dispatch)
    assert await raw_recovery.dispatch_initial_raw_event(raw_event_id) is False
    session.expire_all()
    row = await session.get(models.RawEvent, raw_event_id)
    assert row.processing_status == "INITIAL_DISPATCH_RETRY"
    assert row.processing_claim_token is None
    assert row.processing_attempt_count == 0
    assert row.processing_error_code == "INITIAL_DISPATCH_SEND_FAILED"


async def test_expired_worker_claim_recovers_and_exhausts(session, monkeypatch):
    account_id = uuid.uuid4()
    raw_event_id = await _seed_raw(
        session,
        context=raw_recovery.direct_dispatch_context([_event(account_id)]),
    )
    token = uuid.uuid4()
    await session.execute(
        update(models.RawEvent)
        .where(models.RawEvent.id == raw_event_id)
        .values(
            processing_status="INITIAL_DISPATCHING",
            processing_claim_token=token,
            processing_claim_expires_at=datetime.now(UTC) - timedelta(seconds=1),
            processing_attempt_count=8,
        )
    )
    await session.commit()

    async def unexpected_dispatch(*_args, **_kwargs):
        raise AssertionError("exhausted work must not dispatch")

    monkeypatch.setattr(raw_recovery, "dispatch_actor", unexpected_dispatch)
    assert await raw_recovery.sweep_initial_raw_events() == []
    session.expire_all()
    row = await session.get(models.RawEvent, raw_event_id)
    assert row.processing_status == "INITIAL_DISPATCH_DEAD"
    assert row.processing_claim_token is None
    assert row.processing_error_code == "INITIAL_DISPATCH_WORKER_LEASE_EXPIRED"
    assert row.processed_at is not None


async def test_historical_and_poll_pending_rows_are_not_guessed(session, monkeypatch):
    historical_id = await _seed_raw(session, context={})
    poll_id = await _seed_raw(
        session,
        source="x_dm_poll",
        ingress_kind="poll",
        context={"poll_run_id": str(uuid.uuid4())},
    )

    async def unexpected_dispatch(*_args, **_kwargs):
        raise AssertionError("rows without dispatch metadata must not dispatch")

    monkeypatch.setattr(raw_recovery, "dispatch_actor", unexpected_dispatch)
    assert await raw_recovery.sweep_initial_raw_events() == []
    session.expire_all()
    assert (await session.get(models.RawEvent, historical_id)).processing_status == "PENDING"
    assert (await session.get(models.RawEvent, poll_id)).processing_status == "PENDING"


async def test_invalid_versioned_metadata_goes_to_dead_letter(session, monkeypatch):
    raw_event_id = await _seed_raw(
        session,
        context={"initial_dispatch": {"version": 99, "kind": "direct", "events": []}},
    )

    async def unexpected_dispatch(*_args, **_kwargs):
        raise AssertionError("invalid metadata must not dispatch")

    monkeypatch.setattr(raw_recovery, "dispatch_actor", unexpected_dispatch)
    assert await raw_recovery.sweep_initial_raw_events() == []
    session.expire_all()
    row = await session.get(models.RawEvent, raw_event_id)
    assert row.processing_status == "INITIAL_DISPATCH_DEAD"
    assert row.processing_error_code == "INITIAL_DISPATCH_VERSION_INVALID"


async def test_cross_tenant_direct_metadata_fails_closed(session):
    account_id = uuid.uuid4()
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            tenant_id="tenant-b",
            brand_id="brand-b",
            platform="telegram",
            name="tenant-b-account",
            status="active",
        )
    )
    await session.commit()
    raw_event_id = await _seed_raw(
        session,
        tenant_id="tenant-a",
        context=raw_recovery.direct_dispatch_context([_event(account_id)]),
    )
    reserved = await raw_recovery._reserve_specific(raw_event_id)
    assert reserved is not None

    await process_initial_direct_event(raw_event_id, reserved[1])

    session.expire_all()
    row = await session.get(models.RawEvent, raw_event_id)
    assert row.processing_status == "INITIAL_DISPATCH_RETRY"
    assert row.processing_error_code == "INITIAL_DISPATCH_WORKER_FAILED"
    assert (
        await session.execute(
            select(models.NormalizedEvent).where(
                models.NormalizedEvent.raw_event_id == raw_event_id
            )
        )
    ).first() is None


@pytest.mark.parametrize(
    "initial_status",
    ["PENDING", "INITIAL_DISPATCH_RETRY", "INITIAL_DISPATCHING"],
)
async def test_decision_completion_does_not_overwrite_initial_dispatch(
    session,
    monkeypatch,
    initial_status,
):
    account_id, contact_id, conversation_id, message_id, job_id = (
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
    )
    raw_event_id = await _seed_raw(
        session,
        status=initial_status,
        context=raw_recovery.direct_dispatch_context([_event(account_id)]),
    )
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            tenant_id="tenant-a",
            brand_id="brand-a",
            platform="telegram",
            name="account",
            config={"delivery_mode": "direct"},
            status="active",
        )
    )
    await session.execute(
        insert(models.Contact).values(
            id=contact_id,
            tenant_id="tenant-a",
            platform="telegram",
            platform_account_id=account_id,
            external_user_id="user-1",
        )
    )
    await session.execute(
        insert(models.Conversation).values(
            id=conversation_id,
            tenant_id="tenant-a",
            brand_id="brand-a",
            platform="telegram",
            platform_account_id=account_id,
            contact_id=contact_id,
            conversation_key=f"telegram:{account_id}:chat-1",
            decision_generation=1,
        )
    )
    await session.execute(
        insert(models.Message).values(
            id=message_id,
            conversation_id=conversation_id,
            direction="inbound",
            sender_type="contact",
            text="hello",
            decision_generation=1,
        )
    )
    await session.execute(
        insert(models.DecisionJob).values(
            id=job_id,
            raw_event_id=raw_event_id,
            conversation_id=conversation_id,
            message_id=message_id,
            account_id=account_id,
            decision_generation=1,
            snapshot={
                "text": "hello",
                "platform": "telegram",
                "tenant_id": "tenant-a",
                "brand_id": "brand-a",
                "account_id": str(account_id),
                "conversation_key": f"telegram:{account_id}:chat-1",
                "automation_state": "BOT_DRAFT_ONLY",
                "state_version": 1,
            },
            status="PENDING",
        )
    )
    await session.commit()

    monkeypatch.setattr(
        decision_jobs,
        "ensure_decision_delivery_available",
        lambda **_kwargs: None,
    )

    async def complete_decision(*_args, **_kwargs):
        return None

    monkeypatch.setattr(decision_jobs, "run_and_persist_decision", complete_decision)
    assert await decision_jobs.process_decision_job(str(job_id)) is True
    session.expire_all()
    raw = await session.get(models.RawEvent, raw_event_id)
    assert raw.processing_status == initial_status
    assert (await session.get(models.DecisionJob, job_id)).status == "COMPLETED"


async def test_direct_claim_completion_reaggregates_jobs_under_raw_lock(session):
    raw_event_id = uuid.uuid4()
    token = uuid.uuid4()
    account_id, contact_id, conversation_id, message_id = (
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
    )
    await session.execute(
        insert(models.RawEvent).values(
            id=raw_event_id,
            source="telegram",
            ingress_kind="webhook",
            payload={},
            context=raw_recovery.direct_dispatch_context([_event(account_id)]),
            processing_status="INITIAL_DISPATCHING",
            processing_claim_token=token,
            processing_claim_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            processing_attempt_count=1,
        )
    )
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            tenant_id="tenant-a",
            brand_id="brand-a",
            platform="telegram",
            name="account",
            status="active",
        )
    )
    await session.execute(
        insert(models.Contact).values(
            id=contact_id,
            tenant_id="tenant-a",
            platform="telegram",
            platform_account_id=account_id,
            external_user_id="user-1",
        )
    )
    await session.execute(
        insert(models.Conversation).values(
            id=conversation_id,
            tenant_id="tenant-a",
            brand_id="brand-a",
            platform="telegram",
            platform_account_id=account_id,
            contact_id=contact_id,
            conversation_key=f"telegram:{account_id}:chat-1",
        )
    )
    await session.execute(
        insert(models.Message).values(
            id=message_id,
            conversation_id=conversation_id,
            direction="inbound",
            sender_type="contact",
            text="hello",
        )
    )
    await session.execute(
        insert(models.DecisionJob).values(
            raw_event_id=raw_event_id,
            conversation_id=conversation_id,
            message_id=message_id,
            account_id=account_id,
            snapshot={},
            status="COMPLETED",
        )
    )
    await session.commit()

    assert await raw_recovery.complete_initial_direct_claim(raw_event_id, token) is True
    session.expire_all()
    raw = await session.get(models.RawEvent, raw_event_id)
    assert raw.processing_status == "PROCESSED"
    assert raw.processing_claim_token is None


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        (("COMPLETED", "SUPERSEDED"), "PROCESSED"),
        (("COMPLETED", "FAILED"), "DECISION_PENDING"),
        (("FAILED", "DEFERRED_CHATWOOT"), "DECISION_DEFERRED"),
        (("DEFERRED_CHATWOOT", "NEEDS_REVIEW"), "DECISION_NEEDS_REVIEW"),
    ],
)
async def test_direct_claim_completion_aggregates_job_priorities(session, statuses, expected):
    raw_event_id = uuid.uuid4()
    token = uuid.uuid4()
    account_id, contact_id, conversation_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await session.execute(
        insert(models.RawEvent).values(
            id=raw_event_id,
            source="telegram",
            ingress_kind="webhook",
            payload={},
            context=raw_recovery.direct_dispatch_context([_event(account_id)]),
            processing_status="INITIAL_DISPATCHING",
            processing_claim_token=token,
            processing_claim_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            processing_attempt_count=1,
        )
    )
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            tenant_id="tenant-a",
            brand_id="brand-a",
            platform="telegram",
            name="account",
            status="active",
        )
    )
    await session.execute(
        insert(models.Contact).values(
            id=contact_id,
            tenant_id="tenant-a",
            platform="telegram",
            platform_account_id=account_id,
            external_user_id="user-1",
        )
    )
    await session.execute(
        insert(models.Conversation).values(
            id=conversation_id,
            tenant_id="tenant-a",
            brand_id="brand-a",
            platform="telegram",
            platform_account_id=account_id,
            contact_id=contact_id,
            conversation_key=f"telegram:{account_id}:chat-1",
        )
    )
    for status in statuses:
        message_id = uuid.uuid4()
        await session.execute(
            insert(models.Message).values(
                id=message_id,
                conversation_id=conversation_id,
                direction="inbound",
                sender_type="contact",
                text=status,
            )
        )
        await session.execute(
            insert(models.DecisionJob).values(
                raw_event_id=raw_event_id,
                conversation_id=conversation_id,
                message_id=message_id,
                account_id=account_id,
                snapshot={},
                status=status,
            )
        )
    await session.commit()

    assert await raw_recovery.complete_initial_direct_claim(raw_event_id, token) is True
    session.expire_all()
    raw = await session.get(models.RawEvent, raw_event_id)
    assert raw.processing_status == expected
    assert raw.processing_claim_token is None


async def test_direct_ingest_rejects_non_message_canonical_events(session):
    event = CanonicalEvent(
        platform="telegram",
        platform_account_key=str(uuid.uuid4()),
        external_event_id="receipt-1",
        external_user_id="user-1",
        conversation_key="telegram:account:chat-1",
        text=None,
        event_kind="delivery_receipt",  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match="canonical_event_not_reply_eligible"):
        await direct_ingestion.ingest_canonical_event(event)
    assert (await session.execute(select(models.Message))).first() is None
    assert (await session.execute(select(models.DecisionJob))).first() is None


async def test_chatwoot_claimed_dispatch_reaches_terminal_status(session):
    raw_event_id = await _seed_raw(
        session,
        source="chatwoot",
        tenant_id=None,
        context=raw_recovery.chatwoot_dispatch_context(),
        payload={
            "event": "message_created",
            "id": 55,
            "content": "hello",
            "message_type": "incoming",
            "private": False,
            "sender": {"id": 9, "type": "contact"},
            "conversation": {"id": 77, "inbox_id": 999, "status": "open"},
            "account": {"id": 1},
        },
    )

    assert await raw_recovery.dispatch_initial_raw_event(raw_event_id) is True
    session.expire_all()
    row = await session.get(models.RawEvent, raw_event_id)
    assert row.processing_status == "SKIPPED_UNKNOWN_INBOX"
    assert row.processing_attempt_count == 1
    assert row.processing_claim_token is None
    assert row.processed_at is not None
