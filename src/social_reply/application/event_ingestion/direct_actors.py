import logging
import uuid

import dramatiq
from sqlalchemy import case, select, update

import social_reply.infrastructure.queue.broker  # noqa: F401  确保 broker 先初始化
from social_reply.application.reply_decision.jobs import raw_event_decision_status
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.queue.actor_loop import run_on_actor_loop

logger = logging.getLogger(__name__)


async def _processing_status(raw_event_id: uuid.UUID) -> str:
    async with get_session_factory()() as session:
        statuses = set(
            (
                await session.execute(
                    select(models.DecisionJob.status).where(
                        models.DecisionJob.raw_event_id == raw_event_id
                    )
                )
            )
            .scalars()
            .all()
        )
    return raw_event_decision_status(statuses)


async def _process_events(
    raw_event_id: uuid.UUID,
    events: list[dict],
    *,
    claim_token: uuid.UUID | None = None,
) -> None:
    from social_reply.application.event_ingestion.direct import ingest_canonical_event
    from social_reply.application.event_ingestion.raw_recovery import (
        complete_initial_direct_claim,
        renew_initial_claim,
    )
    from social_reply.domain.messages.canonical import canonical_event_from_dict

    for event in events:
        if claim_token is not None and not await renew_initial_claim(raw_event_id, claim_token):
            return
        await ingest_canonical_event(
            canonical_event_from_dict(event),
            raw_event_id=raw_event_id,
            raw_event_claim_token=str(claim_token) if claim_token is not None else None,
        )

    if claim_token is not None:
        await complete_initial_direct_claim(raw_event_id, claim_token)
        return

    processing_status = await _processing_status(raw_event_id)
    async with get_session_factory()() as session:
        await session.execute(
            update(models.RawEvent)
            .where(models.RawEvent.id == raw_event_id)
            .values(
                processing_status=case(
                    (
                        models.RawEvent.processing_status.in_(
                            (
                                "PENDING",
                                "INITIAL_DISPATCH_RETRY",
                                "INITIAL_DISPATCHING",
                                "DECISION_NEEDS_REVIEW",
                            )
                        ),
                        models.RawEvent.processing_status,
                    ),
                    else_=processing_status,
                )
            )
        )
        await session.commit()


async def _mark_failed(
    raw_event_id: uuid.UUID,
    *,
    claim_token: uuid.UUID | None = None,
) -> None:
    if claim_token is not None:
        from social_reply.application.event_ingestion.raw_recovery import (
            fail_initial_claim,
        )

        await fail_initial_claim(
            raw_event_id,
            claim_token,
            error_code="INITIAL_DISPATCH_WORKER_FAILED",
        )
        return
    async with get_session_factory()() as session:
        await session.execute(
            update(models.RawEvent)
            .where(models.RawEvent.id == raw_event_id)
            .values(
                processing_status=case(
                    (
                        models.RawEvent.processing_status.in_(
                            (
                                "PENDING",
                                "INITIAL_DISPATCH_RETRY",
                                "INITIAL_DISPATCHING",
                                "DECISION_NEEDS_REVIEW",
                            )
                        ),
                        models.RawEvent.processing_status,
                    ),
                    else_="FAILED",
                )
            )
        )
        await session.commit()


async def process_initial_direct_event(
    raw_event_id: uuid.UUID,
    dispatch_token: uuid.UUID,
) -> None:
    from social_reply.application.event_ingestion.raw_recovery import (
        claim_initial_raw_event,
    )

    claim = await claim_initial_raw_event(
        raw_event_id,
        dispatch_token,
        expected_kind="direct",
    )
    if claim is None:
        return
    try:
        await _process_events(
            raw_event_id,
            list(claim.events),
            claim_token=claim.token,
        )
    except Exception:  # noqa: BLE001 - durable RawEvent retry replaces broker retries
        await _mark_failed(raw_event_id, claim_token=claim.token)
        logger.exception("initial direct RawEvent processing failed raw_event_id=%s", raw_event_id)


@dramatiq.actor(max_retries=3)
def process_direct_event(raw_event_id: str, events: list[dict]) -> None:
    event_id = uuid.UUID(raw_event_id)
    try:
        run_on_actor_loop(_process_events(event_id, events))
    except Exception:
        run_on_actor_loop(_mark_failed(event_id))
        raise


@dramatiq.actor(
    actor_name="process_initial_direct_event_v1",
    queue_name="initial_raw_v1",
    max_retries=0,
)
def process_initial_direct_event_actor(raw_event_id: str, dispatch_token: str) -> None:
    run_on_actor_loop(
        process_initial_direct_event(
            uuid.UUID(raw_event_id),
            uuid.UUID(dispatch_token),
        )
    )
