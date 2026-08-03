import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.application.reply_decision.jobs import (
    load_raw_event_decision_statuses,
    raw_event_decision_status,
)
from social_reply.domain.messages.canonical import canonical_event_from_dict
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.queue.dispatch import dispatch_actor

logger = logging.getLogger(__name__)

_DISPATCH_CONTEXT_KEY = "initial_dispatch"
_DISPATCH_VERSION = 1
_CLAIMABLE_STATUSES = ("PENDING", "INITIAL_DISPATCH_RETRY")
_DISPATCH_BATCH_SIZE = 100
_DISPATCH_RESERVATION = timedelta(minutes=5)
_WORKER_LEASE = timedelta(minutes=5)
_MAX_ATTEMPTS = 8


@dataclass(frozen=True)
class InitialDispatchClaim:
    raw_event_id: uuid.UUID
    token: uuid.UUID
    kind: str
    events: tuple[dict[str, Any], ...]


def direct_dispatch_context(events: list[dict[str, Any]]) -> dict[str, Any]:
    stored_events = []
    for event in events:
        stored_event = dict(event)
        stored_event["raw_payload"] = {}
        stored_events.append(stored_event)
    return {
        _DISPATCH_CONTEXT_KEY: {
            "version": _DISPATCH_VERSION,
            "kind": "direct",
            "events": stored_events,
        }
    }


def chatwoot_dispatch_context() -> dict[str, Any]:
    return {
        _DISPATCH_CONTEXT_KEY: {
            "version": _DISPATCH_VERSION,
            "kind": "chatwoot",
        }
    }


async def _database_now(session: AsyncSession) -> datetime:
    return (await session.execute(select(func.clock_timestamp()))).scalar_one()


def _dispatch_spec(row: models.RawEvent) -> tuple[str, tuple[dict[str, Any], ...]]:
    dispatch = dict((row.context or {}).get(_DISPATCH_CONTEXT_KEY) or {})
    if dispatch.get("version") != _DISPATCH_VERSION:
        raise ValueError("INITIAL_DISPATCH_VERSION_INVALID")
    kind = dispatch.get("kind")
    if kind == "direct":
        if row.source not in {"telegram", "meta", "x", "feishu"} or row.ingress_kind != "webhook":
            raise ValueError("INITIAL_DISPATCH_SOURCE_INVALID")
        values = dispatch.get("events")
        if not isinstance(values, list) or not values:
            raise ValueError("INITIAL_DISPATCH_EVENTS_INVALID")
        events: list[dict[str, Any]] = []
        for value in values:
            if not isinstance(value, dict):
                raise ValueError("INITIAL_DISPATCH_EVENTS_INVALID")
            event = canonical_event_from_dict(value)
            uuid.UUID(event.platform_account_key)
            events.append(dict(value))
        return kind, tuple(events)
    if kind == "chatwoot":
        if row.source not in {"chatwoot", "chatwoot_reconcile"}:
            raise ValueError("INITIAL_DISPATCH_SOURCE_INVALID")
        if row.ingress_kind not in {"webhook", "reconcile"}:
            raise ValueError("INITIAL_DISPATCH_SOURCE_INVALID")
        return kind, ()
    raise ValueError("INITIAL_DISPATCH_KIND_INVALID")


def _mark_dead(row: models.RawEvent, *, now: datetime, error_code: str) -> None:
    row.processing_status = "INITIAL_DISPATCH_DEAD"
    row.processing_claim_token = None
    row.processing_claim_expires_at = None
    row.processing_next_attempt_at = None
    row.processing_error_code = error_code
    row.processed_at = now


async def _reserve_row(row: models.RawEvent, *, now: datetime) -> uuid.UUID | None:
    if row.processing_status not in _CLAIMABLE_STATUSES:
        return None
    if _DISPATCH_CONTEXT_KEY not in dict(row.context or {}):
        return None
    if row.processing_next_attempt_at and row.processing_next_attempt_at > now:
        return None
    try:
        _dispatch_spec(row)
    except (KeyError, TypeError, ValueError) as exc:
        _mark_dead(row, now=now, error_code=str(exc))
        return None
    token = uuid.uuid4()
    row.processing_claim_token = token
    row.processing_claim_expires_at = None
    row.processing_last_dispatched_at = now
    row.processing_next_attempt_at = now + _DISPATCH_RESERVATION
    row.processing_error_code = None
    row.processed_at = None
    return token


async def _reserve_specific(raw_event_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID] | None:
    async with get_session_factory()() as session:
        row = (
            await session.execute(
                select(models.RawEvent).where(models.RawEvent.id == raw_event_id).with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        now = await _database_now(session)
        token = await _reserve_row(row, now=now)
        await session.commit()
        return (row.id, token) if token is not None else None


async def _reserve_due(*, limit: int = _DISPATCH_BATCH_SIZE) -> list[tuple[uuid.UUID, uuid.UUID]]:
    async with get_session_factory()() as session:
        now = await _database_now(session)
        rows = list(
            (
                await session.execute(
                    select(models.RawEvent)
                    .where(
                        models.RawEvent.processing_status.in_(_CLAIMABLE_STATUSES),
                        models.RawEvent.context.op("?")(_DISPATCH_CONTEXT_KEY),
                        or_(
                            models.RawEvent.processing_next_attempt_at.is_(None),
                            models.RawEvent.processing_next_attempt_at <= now,
                        ),
                    )
                    .order_by(models.RawEvent.received_at)
                    .limit(limit * 4)
                    .with_for_update(skip_locked=True)
                )
            ).scalars()
        )
        reserved: list[tuple[uuid.UUID, uuid.UUID]] = []
        for row in rows:
            token = await _reserve_row(row, now=now)
            if token is None:
                continue
            reserved.append((row.id, token))
            if len(reserved) >= limit:
                break
        await session.commit()
        return reserved


async def _release_reservation(raw_event_id: uuid.UUID, token: uuid.UUID) -> None:
    async with get_session_factory()() as session:
        row = (
            await session.execute(
                select(models.RawEvent).where(models.RawEvent.id == raw_event_id).with_for_update()
            )
        ).scalar_one_or_none()
        if (
            row is None
            or row.processing_status not in _CLAIMABLE_STATUSES
            or row.processing_claim_token != token
        ):
            return
        now = await _database_now(session)
        row.processing_status = "INITIAL_DISPATCH_RETRY"
        row.processing_claim_token = None
        row.processing_claim_expires_at = None
        row.processing_next_attempt_at = now + timedelta(seconds=30)
        row.processing_error_code = "INITIAL_DISPATCH_SEND_FAILED"
        await session.commit()


async def _dispatch_reserved(raw_event_id: uuid.UUID, token: uuid.UUID) -> bool:
    async with get_session_factory()() as session:
        row = await session.get(models.RawEvent, raw_event_id)
        if row is None or row.processing_claim_token != token:
            return False
        try:
            kind, _events = _dispatch_spec(row)
        except (KeyError, TypeError, ValueError):
            return False

    if kind == "direct":
        from social_reply.application.event_ingestion.direct_actors import (
            process_initial_direct_event,
            process_initial_direct_event_actor,
        )

        async def inline():
            await process_initial_direct_event(raw_event_id, token)

        actor = process_initial_direct_event_actor
    else:
        from social_reply.application.event_ingestion.actors import (
            process_initial_chatwoot_event_actor,
        )
        from social_reply.application.event_ingestion.processor import (
            process_claimed_raw_event,
        )

        async def inline():
            await process_claimed_raw_event(raw_event_id, token)

        actor = process_initial_chatwoot_event_actor

    try:
        await dispatch_actor(
            actor,
            str(raw_event_id),
            str(token),
            inline=inline,
        )
    except Exception:  # noqa: BLE001 - PostgreSQL reservation remains the recovery authority
        await _release_reservation(raw_event_id, token)
        logger.exception("initial RawEvent dispatch failed raw_event_id=%s", raw_event_id)
        return False
    return True


async def dispatch_initial_raw_event(raw_event_id: uuid.UUID) -> bool:
    reserved = await _reserve_specific(raw_event_id)
    if reserved is None:
        return False
    return await _dispatch_reserved(*reserved)


async def claim_initial_raw_event(
    raw_event_id: uuid.UUID,
    dispatch_token: uuid.UUID,
    *,
    expected_kind: str,
) -> InitialDispatchClaim | None:
    async with get_session_factory()() as session:
        row = (
            await session.execute(
                select(models.RawEvent).where(models.RawEvent.id == raw_event_id).with_for_update()
            )
        ).scalar_one_or_none()
        if row is None or row.processing_status not in _CLAIMABLE_STATUSES:
            return None
        now = await _database_now(session)
        if (
            row.processing_claim_token != dispatch_token
            or row.processing_next_attempt_at is None
            or row.processing_next_attempt_at <= now
        ):
            return None
        try:
            kind, events = _dispatch_spec(row)
        except (KeyError, TypeError, ValueError) as exc:
            _mark_dead(row, now=now, error_code=str(exc))
            await session.commit()
            return None
        if kind != expected_kind:
            _mark_dead(row, now=now, error_code="INITIAL_DISPATCH_KIND_MISMATCH")
            await session.commit()
            return None
        row.processing_status = "INITIAL_DISPATCHING"
        row.processing_claim_expires_at = now + _WORKER_LEASE
        row.processing_next_attempt_at = None
        row.processing_attempt_count = int(row.processing_attempt_count or 0) + 1
        row.processing_error_code = None
        await session.commit()
        return InitialDispatchClaim(row.id, dispatch_token, kind, events)


async def renew_initial_claim(raw_event_id: uuid.UUID, token: uuid.UUID) -> bool:
    async with get_session_factory()() as session:
        row = (
            await session.execute(
                select(models.RawEvent).where(models.RawEvent.id == raw_event_id).with_for_update()
            )
        ).scalar_one_or_none()
        now = await _database_now(session)
        if (
            row is None
            or row.processing_status != "INITIAL_DISPATCHING"
            or row.processing_claim_token != token
            or row.processing_claim_expires_at is None
            or row.processing_claim_expires_at <= now
        ):
            return False
        row.processing_claim_expires_at = now + _WORKER_LEASE
        await session.commit()
        return True


def _retry_delay(attempt_count: int) -> timedelta:
    return timedelta(seconds=min(30 * 2 ** max(attempt_count - 1, 0), 3600))


async def fail_initial_claim(
    raw_event_id: uuid.UUID,
    token: uuid.UUID,
    *,
    error_code: str,
) -> bool:
    async with get_session_factory()() as session:
        row = (
            await session.execute(
                select(models.RawEvent).where(models.RawEvent.id == raw_event_id).with_for_update()
            )
        ).scalar_one_or_none()
        if (
            row is None
            or row.processing_status != "INITIAL_DISPATCHING"
            or row.processing_claim_token != token
        ):
            return False
        now = await _database_now(session)
        row.processing_claim_token = None
        row.processing_claim_expires_at = None
        row.processing_error_code = error_code
        if int(row.processing_attempt_count or 0) >= _MAX_ATTEMPTS:
            row.processing_status = "INITIAL_DISPATCH_DEAD"
            row.processing_next_attempt_at = None
            row.processed_at = now
        else:
            row.processing_status = "INITIAL_DISPATCH_RETRY"
            row.processing_next_attempt_at = now + _retry_delay(row.processing_attempt_count)
            row.processed_at = None
        await session.commit()
        return True


async def complete_initial_direct_claim(
    raw_event_id: uuid.UUID,
    token: uuid.UUID,
) -> bool:
    async with get_session_factory()() as session:
        row = (
            await session.execute(
                select(models.RawEvent).where(models.RawEvent.id == raw_event_id).with_for_update()
            )
        ).scalar_one_or_none()
        now = await _database_now(session)
        if (
            row is None
            or row.processing_status != "INITIAL_DISPATCHING"
            or row.processing_claim_token != token
            or row.processing_claim_expires_at is None
            or row.processing_claim_expires_at <= now
        ):
            return False
        statuses = await load_raw_event_decision_statuses(session, raw_event_id)
        processing_status = raw_event_decision_status(statuses)
        row.processing_status = processing_status
        row.processing_claim_token = None
        row.processing_claim_expires_at = None
        row.processing_next_attempt_at = None
        row.processing_error_code = None
        if processing_status not in {"DECISION_PENDING", "DECISION_DEFERRED"}:
            row.processed_at = now
        await session.commit()
        return True


async def _recover_expired_claims(*, limit: int = _DISPATCH_BATCH_SIZE) -> list[uuid.UUID]:
    async with get_session_factory()() as session:
        now = await _database_now(session)
        rows = list(
            (
                await session.execute(
                    select(models.RawEvent)
                    .where(
                        models.RawEvent.processing_status == "INITIAL_DISPATCHING",
                        or_(
                            models.RawEvent.processing_claim_expires_at.is_(None),
                            models.RawEvent.processing_claim_expires_at <= now,
                        ),
                    )
                    .order_by(models.RawEvent.received_at)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            ).scalars()
        )
        recovered: list[uuid.UUID] = []
        for row in rows:
            row.processing_claim_token = None
            row.processing_claim_expires_at = None
            row.processing_error_code = "INITIAL_DISPATCH_WORKER_LEASE_EXPIRED"
            if int(row.processing_attempt_count or 0) >= _MAX_ATTEMPTS:
                row.processing_status = "INITIAL_DISPATCH_DEAD"
                row.processing_next_attempt_at = None
                row.processed_at = now
            else:
                row.processing_status = "INITIAL_DISPATCH_RETRY"
                row.processing_next_attempt_at = now
                row.processed_at = None
            recovered.append(row.id)
        await session.commit()
        return recovered


async def sweep_initial_raw_events() -> list[str]:
    await _recover_expired_claims()
    reserved = await _reserve_due()
    dispatched: list[str] = []
    for raw_event_id, token in reserved:
        if await _dispatch_reserved(raw_event_id, token):
            dispatched.append(str(raw_event_id))
    return dispatched
