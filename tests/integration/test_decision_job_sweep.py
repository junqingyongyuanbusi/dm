import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, select

from social_reply.application.reply_decision import jobs as decision_jobs
from social_reply.application.reply_decision import persist as persist_module
from social_reply.application.reply_decision.jobs import (
    process_decision_job,
    snapshot_to_dict,
    sweep_decision_jobs,
)
from social_reply.application.reply_decision.pipeline import DecisionSnapshot
from social_reply.domain.automation.state_machine import ensure_state
from social_reply.infrastructure.database import models

pytestmark = pytest.mark.integration


async def _seed_job(
    session,
    *,
    status="PENDING",
    next_attempt_at=None,
    locked_at=None,
    chatwoot_inbox_id: int | None = None,
    attempt_count: int = 0,
):
    account_id, contact_id, conversation_id, message_id, raw_event_id = (
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
    )
    key = f"telegram:x:{uuid.uuid4().hex[:8]}"
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            brand_id="b1",
            platform="telegram",
            name="a",
            chatwoot_inbox_id=(
                chatwoot_inbox_id if chatwoot_inbox_id is not None else uuid.uuid4().int % 10**9
            ),
        )
    )
    await session.execute(
        insert(models.Contact).values(
            id=contact_id,
            platform="telegram",
            platform_account_id=account_id,
            external_user_id=uuid.uuid4().hex,
        )
    )
    await session.execute(
        insert(models.Conversation).values(
            id=conversation_id,
            brand_id="b1",
            platform="telegram",
            platform_account_id=account_id,
            contact_id=contact_id,
            conversation_key=key,
            decision_generation=1,
        )
    )
    await ensure_state(session, conversation_id, "BOT_ACTIVE")
    await session.execute(
        insert(models.Message).values(
            id=message_id,
            conversation_id=conversation_id,
            direction="inbound",
            sender_type="contact",
            text="hi",
            decision_generation=1,
        )
    )
    await session.execute(
        insert(models.RawEvent).values(
            id=raw_event_id,
            source="chatwoot",
            payload={},
            processing_status="PROCESSED",
        )
    )
    job_id = uuid.uuid4()
    snapshot = DecisionSnapshot(
        text="hi",
        platform="telegram",
        tenant_id="default",
        brand_id="b1",
        account_id=str(account_id),
        conversation_key=key,
        automation_state="BOT_ACTIVE",
        state_version=1,
    )
    await session.execute(
        insert(models.DecisionJob).values(
            id=job_id,
            raw_event_id=raw_event_id,
            conversation_id=conversation_id,
            message_id=message_id,
            account_id=account_id,
            snapshot=snapshot_to_dict(snapshot),
            decision_generation=1,
            status=status,
            attempt_count=attempt_count,
            next_attempt_at=next_attempt_at,
            locked_at=locked_at,
        )
    )
    await session.commit()
    return job_id


async def _add_job(session, first_job_id: uuid.UUID, *, status: str) -> uuid.UUID:
    first_job = await session.get(models.DecisionJob, first_job_id)
    message_id = uuid.uuid4()
    job_id = uuid.uuid4()
    await session.execute(
        insert(models.Message).values(
            id=message_id,
            conversation_id=first_job.conversation_id,
            direction="inbound",
            sender_type="contact",
            text=status,
            decision_generation=first_job.decision_generation,
        )
    )
    await session.execute(
        insert(models.DecisionJob).values(
            id=job_id,
            raw_event_id=first_job.raw_event_id,
            conversation_id=first_job.conversation_id,
            message_id=message_id,
            account_id=first_job.account_id,
            snapshot=first_job.snapshot,
            decision_generation=first_job.decision_generation,
            status=status,
        )
    )
    await session.commit()
    return job_id


async def test_concurrent_job_finalizers_commit_complete_raw_aggregate(session, monkeypatch):
    first_job_id = await _seed_job(session)
    second_job_id = await _add_job(session, first_job_id, status="PENDING")
    real_aggregate = decision_jobs.aggregate_raw_event_decisions
    aggregate_count = 0
    both_finalizers_ready = asyncio.Event()
    count_lock = asyncio.Lock()

    async def synchronize_aggregate(aggregate_session, raw_event_id):
        nonlocal aggregate_count
        async with count_lock:
            aggregate_count += 1
            if aggregate_count == 2:
                both_finalizers_ready.set()
        await asyncio.wait_for(both_finalizers_ready.wait(), timeout=1)
        await real_aggregate(aggregate_session, raw_event_id)

    async def complete_decision(*_args, **_kwargs):
        return None

    monkeypatch.setattr(decision_jobs, "aggregate_raw_event_decisions", synchronize_aggregate)
    monkeypatch.setattr(decision_jobs, "run_and_persist_decision", complete_decision)

    results = await asyncio.wait_for(
        asyncio.gather(
            process_decision_job(str(first_job_id)),
            process_decision_job(str(second_job_id)),
        ),
        timeout=5,
    )

    assert results == [True, True]
    session.expire_all()
    jobs = (
        (
            await session.execute(
                select(models.DecisionJob)
                .where(models.DecisionJob.id.in_((first_job_id, second_job_id)))
                .order_by(models.DecisionJob.id)
            )
        )
        .scalars()
        .all()
    )
    assert [job.status for job in jobs] == ["COMPLETED", "COMPLETED"]
    raw = await session.get(models.RawEvent, jobs[0].raw_event_id)
    assert raw.processing_status == "PROCESSED"


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        (("COMPLETED", "SUPERSEDED"), "PROCESSED"),
        (("COMPLETED", "FAILED"), "DECISION_PENDING"),
        (("FAILED", "DEFERRED_CHATWOOT"), "DECISION_DEFERRED"),
        (("DEFERRED_CHATWOOT", "NEEDS_REVIEW"), "DECISION_NEEDS_REVIEW"),
    ],
)
async def test_raw_event_aggregate_status_priority(session, statuses, expected):
    first_job_id = await _seed_job(session, status=statuses[0])
    await _add_job(session, first_job_id, status=statuses[1])
    raw_event_id = await session.scalar(
        select(models.DecisionJob.raw_event_id).where(models.DecisionJob.id == first_job_id)
    )

    await decision_jobs.aggregate_raw_event_decisions(session, raw_event_id)
    await session.commit()

    session.expire_all()
    assert (await session.get(models.RawEvent, raw_event_id)).processing_status == expected


async def test_jobless_raw_event_aggregate_is_noop(session):
    raw_event_id = uuid.uuid4()
    await session.execute(
        insert(models.RawEvent).values(
            id=raw_event_id,
            source="chatwoot",
            payload={},
            processing_status="DECISION_PENDING",
        )
    )
    await session.commit()

    await decision_jobs.aggregate_raw_event_decisions(session, raw_event_id)
    await session.commit()

    session.expire_all()
    assert (await session.get(models.RawEvent, raw_event_id)).processing_status == (
        "DECISION_PENDING"
    )
    assert decision_jobs.raw_event_decision_status(set()) == "PROCESSED"


async def test_chatwoot_decision_is_deferred_and_resumed_after_reenable(session, monkeypatch):
    job_id = await _seed_job(session)

    def disabled():
        return type("Settings", (), {"chatwoot_enabled": False})()

    def enabled():
        return type("Settings", (), {"chatwoot_enabled": True})()

    real_run_and_persist = decision_jobs.run_and_persist_decision

    async def unexpected_decision_run(*_args, **_kwargs):
        raise AssertionError("decision pipeline must not run while Chatwoot is disabled")

    monkeypatch.setattr(persist_module, "get_settings", disabled)
    monkeypatch.setattr(decision_jobs, "get_settings", disabled)
    monkeypatch.setattr(
        decision_jobs,
        "run_and_persist_decision",
        unexpected_decision_run,
    )

    assert await process_decision_job(str(job_id)) is False
    session.expire_all()
    job = (
        await session.execute(select(models.DecisionJob).where(models.DecisionJob.id == job_id))
    ).scalar_one()
    raw_event_id = job.raw_event_id
    raw = await session.get(models.RawEvent, raw_event_id)
    assert job.status == "DEFERRED_CHATWOOT"
    assert raw.processing_status == "DECISION_DEFERRED"
    assert (await session.execute(select(models.ReplyDecision))).first() is None
    assert (await session.execute(select(models.OutboxMessage))).first() is None

    monkeypatch.setattr(persist_module, "get_settings", enabled)
    monkeypatch.setattr(decision_jobs, "get_settings", enabled)
    monkeypatch.setattr(
        decision_jobs,
        "run_and_persist_decision",
        real_run_and_persist,
    )
    assert job_id in await sweep_decision_jobs()
    assert await process_decision_job(str(job_id)) is True
    session.expire_all()
    resumed = await session.get(models.DecisionJob, job_id)
    resumed_raw = await session.get(models.RawEvent, raw_event_id)
    assert resumed.status == "COMPLETED"
    assert resumed_raw.processing_status == "PROCESSED"
    assert (await session.execute(select(models.OutboxMessage))).scalar_one().destination_type == (
        "chatwoot_conversation"
    )


async def test_missing_delivery_route_needs_review(session):
    job_id = await _seed_job(session, chatwoot_inbox_id=0)
    account_id = await session.scalar(
        select(models.DecisionJob.account_id).where(models.DecisionJob.id == job_id)
    )
    await session.execute(
        models.PlatformAccount.__table__.update()
        .where(models.PlatformAccount.id == account_id)
        .values(chatwoot_inbox_id=None)
    )
    await session.commit()

    assert await process_decision_job(str(job_id)) is False
    session.expire_all()
    job = await session.get(models.DecisionJob, job_id)
    raw = await session.get(models.RawEvent, job.raw_event_id)
    assert job.status == "NEEDS_REVIEW"
    assert job.last_error == "chatwoot_inbox_id_missing"
    assert raw.processing_status == "DECISION_NEEDS_REVIEW"


async def test_sweep_enqueues_pending_due_failed_and_recovers_stale(session):
    now = datetime.now(UTC)
    pending = await _seed_job(session)
    due_failed = await _seed_job(
        session, status="FAILED", next_attempt_at=now - timedelta(seconds=1)
    )
    not_due = await _seed_job(session, status="FAILED", next_attempt_at=now + timedelta(minutes=5))
    stale = await _seed_job(session, status="PROCESSING", locked_at=now - timedelta(minutes=6))
    completed = await _seed_job(session, status="COMPLETED")

    enqueued = await sweep_decision_jobs()

    assert set(enqueued) == {pending, due_failed, stale}
    assert not_due not in enqueued and completed not in enqueued
    session.expire_all()
    recovered = (
        await session.execute(select(models.DecisionJob).where(models.DecisionJob.id == stale))
    ).scalar_one()
    assert recovered.status == "FAILED"
    assert recovered.last_error == "stale PROCESSING recovered by sweep"


async def test_decision_retry_exhaustion_transitions_to_needs_review(session, monkeypatch):
    job_id = await _seed_job(session, attempt_count=decision_jobs._MAX_ATTEMPTS - 1)

    async def fail_decision(*_args, **_kwargs):
        raise RuntimeError("persistent decision failure")

    monkeypatch.setattr(decision_jobs, "run_and_persist_decision", fail_decision)

    assert await process_decision_job(str(job_id)) is False
    session.expire_all()
    job = await session.get(models.DecisionJob, job_id)
    raw = await session.get(models.RawEvent, job.raw_event_id)
    assert job.attempt_count == decision_jobs._MAX_ATTEMPTS
    assert job.status == "NEEDS_REVIEW"
    assert job.next_attempt_at is None
    assert job.locked_at is None
    assert job.last_error.startswith("RETRY_EXHAUSTED: RuntimeError")
    assert raw.processing_status == "DECISION_NEEDS_REVIEW"


async def test_stale_eighth_decision_worker_cannot_revert_terminal_raw_event(
    session,
    monkeypatch,
):
    job_id = await _seed_job(session, attempt_count=decision_jobs._MAX_ATTEMPTS - 1)
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked_decision(*_args, **_kwargs):
        started.set()
        await release.wait()

    monkeypatch.setattr(decision_jobs, "run_and_persist_decision", blocked_decision)
    worker = asyncio.create_task(process_decision_job(str(job_id)))
    await asyncio.wait_for(started.wait(), timeout=1)
    await session.execute(
        models.DecisionJob.__table__.update()
        .where(models.DecisionJob.id == job_id)
        .values(locked_at=datetime.now(UTC) - timedelta(minutes=6))
    )
    await session.commit()

    assert job_id not in await sweep_decision_jobs()
    release.set()
    assert await worker is False

    session.expire_all()
    job = await session.get(models.DecisionJob, job_id)
    raw = await session.get(models.RawEvent, job.raw_event_id)
    assert job.status == "NEEDS_REVIEW"
    assert job.attempt_count == decision_jobs._MAX_ATTEMPTS
    assert raw.processing_status == "DECISION_NEEDS_REVIEW"


async def test_sweep_terminalizes_exhausted_stale_decision(session):
    stale = await _seed_job(
        session,
        status="PROCESSING",
        locked_at=datetime.now(UTC) - timedelta(minutes=6),
        attempt_count=decision_jobs._MAX_ATTEMPTS,
    )

    assert stale not in await sweep_decision_jobs()
    session.expire_all()
    job = await session.get(models.DecisionJob, stale)
    raw = await session.get(models.RawEvent, job.raw_event_id)
    assert job.status == "NEEDS_REVIEW"
    assert job.next_attempt_at is None
    assert job.last_error == "RETRY_EXHAUSTED: recovered by sweep"
    assert raw.processing_status == "DECISION_NEEDS_REVIEW"


async def test_decision_sweep_isolates_broker_dispatch_failures(session, monkeypatch):
    first = await _seed_job(session)
    second = await _seed_job(session)
    calls: list[uuid.UUID] = []

    async def dispatch(_actor, job_id: str, **_kwargs):
        calls.append(uuid.UUID(job_id))
        if len(calls) == 1:
            raise RuntimeError("broker unavailable")

    monkeypatch.setattr(decision_jobs, "dispatch_actor", dispatch)

    dispatched = await sweep_decision_jobs()
    assert set(calls) == {first, second}
    assert len(dispatched) == 1
    assert dispatched[0] == calls[1]
