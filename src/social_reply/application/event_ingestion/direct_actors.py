import uuid

import dramatiq
from sqlalchemy import update

import social_reply.infrastructure.queue.broker  # noqa: F401  确保 broker 先初始化
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.queue.actor_loop import run_on_actor_loop


async def _process_events(raw_event_id: uuid.UUID, events: list[dict]) -> None:
    from social_reply.application.event_ingestion.direct import ingest_canonical_event
    from social_reply.domain.messages.canonical import canonical_event_from_dict

    for event in events:
        await ingest_canonical_event(
            canonical_event_from_dict(event),
            raw_event_id=raw_event_id,
        )

    async with get_session_factory()() as session:
        jobs_pending = (
            await session.execute(
                models.DecisionJob.__table__.select()
                .with_only_columns(models.DecisionJob.id)
                .where(
                    models.DecisionJob.raw_event_id == raw_event_id,
                    models.DecisionJob.status != "COMPLETED",
                )
                .limit(1)
            )
        ).first()
        await session.execute(
            update(models.RawEvent)
            .where(models.RawEvent.id == raw_event_id)
            .values(
                processing_status="DECISION_PENDING" if jobs_pending else "PROCESSED"
            )
        )
        await session.commit()


async def _mark_failed(raw_event_id: uuid.UUID) -> None:
    async with get_session_factory()() as session:
        await session.execute(
            update(models.RawEvent)
            .where(models.RawEvent.id == raw_event_id)
            .values(processing_status="FAILED")
        )
        await session.commit()


@dramatiq.actor(max_retries=3)
def process_direct_event(raw_event_id: str, events: list[dict]) -> None:
    event_id = uuid.UUID(raw_event_id)
    try:
        run_on_actor_loop(_process_events(event_id, events))
    except Exception:
        run_on_actor_loop(_mark_failed(event_id))
        raise
