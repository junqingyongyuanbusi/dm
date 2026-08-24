import uuid

import pytest
from sqlalchemy import insert, select

from social_reply.application.event_ingestion.direct_actors import _mark_failed, _process_events
from social_reply.infrastructure.database import models

pytestmark = pytest.mark.integration


async def _seed_job(session, *, status: str, raw_status: str = "DECISION_PENDING") -> uuid.UUID:
    raw_event_id = uuid.uuid4()
    account_id = uuid.uuid4()
    contact_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    message_id = uuid.uuid4()
    await session.execute(
        insert(models.RawEvent).values(
            id=raw_event_id,
            source="telegram",
            payload={},
            processing_status=raw_status,
        )
    )
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            tenant_id="default",
            brand_id="b1",
            platform="telegram",
            name="account",
        )
    )
    await session.execute(
        insert(models.Contact).values(
            id=contact_id,
            tenant_id="default",
            platform="telegram",
            platform_account_id=account_id,
            external_user_id="u1",
        )
    )
    await session.execute(
        insert(models.Conversation).values(
            id=conversation_id,
            tenant_id="default",
            brand_id="b1",
            platform="telegram",
            platform_account_id=account_id,
            contact_id=contact_id,
            conversation_key="telegram:account:u1",
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
            status=status,
        )
    )
    await session.commit()
    return raw_event_id


async def _add_job(session, raw_event_id: uuid.UUID, status: str) -> None:
    first_job = (
        await session.execute(
            select(models.DecisionJob).where(models.DecisionJob.raw_event_id == raw_event_id)
        )
    ).scalar_one()
    message_id = uuid.uuid4()
    await session.execute(
        insert(models.Message).values(
            id=message_id,
            conversation_id=first_job.conversation_id,
            direction="inbound",
            sender_type="contact",
            text=status,
        )
    )
    await session.execute(
        insert(models.DecisionJob).values(
            raw_event_id=raw_event_id,
            conversation_id=first_job.conversation_id,
            message_id=message_id,
            account_id=first_job.account_id,
            snapshot={},
            status=status,
        )
    )
    await session.commit()


@pytest.mark.parametrize(
    ("job_status", "initial_raw_status", "expected_raw_status"),
    [
        ("NEEDS_REVIEW", "DECISION_PENDING", "DECISION_NEEDS_REVIEW"),
        ("FAILED", "DECISION_PENDING", "DECISION_PENDING"),
        ("DEFERRED_CHATWOOT", "DECISION_PENDING", "DECISION_DEFERRED"),
        ("COMPLETED", "DECISION_PENDING", "PROCESSED"),
        ("SUPERSEDED", "DECISION_PENDING", "PROCESSED"),
        # A concurrent decision finalizer may commit review after this actor read
        # PROCESSING. The atomic CASE must never downgrade that terminal state.
        ("PROCESSING", "DECISION_NEEDS_REVIEW", "DECISION_NEEDS_REVIEW"),
    ],
)
async def test_direct_actor_preserves_decision_status_priority(
    session, job_status, initial_raw_status, expected_raw_status
):
    raw_event_id = await _seed_job(session, status=job_status, raw_status=initial_raw_status)

    await _process_events(raw_event_id, [])

    session.expire_all()
    raw_status = (
        await session.execute(
            select(models.RawEvent.processing_status).where(models.RawEvent.id == raw_event_id)
        )
    ).scalar_one()
    assert raw_status == expected_raw_status


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        (("COMPLETED", "SUPERSEDED"), "PROCESSED"),
        (("COMPLETED", "FAILED"), "DECISION_PENDING"),
        (("FAILED", "DEFERRED_CHATWOOT"), "DECISION_DEFERRED"),
        (("DEFERRED_CHATWOOT", "NEEDS_REVIEW"), "DECISION_NEEDS_REVIEW"),
    ],
)
async def test_direct_actor_aggregates_mixed_job_priorities(session, statuses, expected):
    raw_event_id = await _seed_job(session, status=statuses[0])
    await _add_job(session, raw_event_id, statuses[1])

    await _process_events(raw_event_id, [])

    session.expire_all()
    assert (await session.get(models.RawEvent, raw_event_id)).processing_status == expected


@pytest.mark.parametrize(
    "initial_status",
    ["PENDING", "INITIAL_DISPATCH_RETRY", "INITIAL_DISPATCHING"],
)
async def test_direct_actor_does_not_override_initial_dispatch_priority(session, initial_status):
    raw_event_id = await _seed_job(
        session,
        status="NEEDS_REVIEW",
        raw_status=initial_status,
    )

    await _process_events(raw_event_id, [])

    session.expire_all()
    assert (await session.get(models.RawEvent, raw_event_id)).processing_status == initial_status


async def test_direct_actor_failure_does_not_erase_review_status(session):
    raw_event_id = await _seed_job(
        session,
        status="NEEDS_REVIEW",
        raw_status="DECISION_NEEDS_REVIEW",
    )

    await _mark_failed(raw_event_id)

    session.expire_all()
    raw_status = (
        await session.execute(
            select(models.RawEvent.processing_status).where(models.RawEvent.id == raw_event_id)
        )
    ).scalar_one()
    assert raw_status == "DECISION_NEEDS_REVIEW"
