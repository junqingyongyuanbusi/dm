import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, select

from social_reply.application.reply_decision.jobs import snapshot_to_dict, sweep_decision_jobs
from social_reply.application.reply_decision.pipeline import DecisionSnapshot
from social_reply.domain.automation.state_machine import ensure_state
from social_reply.infrastructure.database import models

pytestmark = pytest.mark.integration


async def _seed_job(session, *, status="PENDING", next_attempt_at=None, locked_at=None):
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
            chatwoot_inbox_id=uuid.uuid4().int % 10**9,
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
            status=status,
            next_attempt_at=next_attempt_at,
            locked_at=locked_at,
        )
    )
    await session.commit()
    return job_id


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
