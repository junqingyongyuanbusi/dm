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

    from social_reply.application.reply_decision.actors import process_reply_decision

    def dispatch(job_id: str):
        calls.append(uuid.UUID(job_id))
        if len(calls) == 1:
            raise RuntimeError("broker unavailable")

    monkeypatch.setattr(process_reply_decision, "send", dispatch)

    dispatched = await sweep_decision_jobs()
    assert set(calls) == {first, second}
    assert len(dispatched) == 1
    assert dispatched[0] == calls[1]
