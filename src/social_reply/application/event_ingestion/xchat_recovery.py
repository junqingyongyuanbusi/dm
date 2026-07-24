import logging
import os
import time
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from social_reply.application.event_ingestion.xchat_subscription import (
    ensure_xchat_subscriptions,
)
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.queue.dispatch import dispatch_actor

logger = logging.getLogger(__name__)

_SCHEDULER_STATUSES = (
    "XCHAT_DECRYPTION_PENDING",
    "XCHAT_PIN_REQUIRED",
    "XCHAT_KEY_RECOVERY_REQUIRED",
    "XCHAT_RETRYABLE_ERROR",
)
_ACTIVATION_ONLY_STATUSES = ("XCHAT_DECRYPT_FAILED",)
_REPLAY_BATCH_SIZE = 100
_DISPATCH_RESERVATION = timedelta(minutes=5)
_SWEEP_INTERVAL_SECONDS = int(os.getenv("XCHAT_RECOVERY_SWEEP_INTERVAL_SECONDS", "30"))
_MAX_RETRY_ATTEMPTS = 8
_last_sweep_at: float | None = None


async def recover_xchat_account_state(account_id: uuid.UUID) -> list[str]:
    await ensure_xchat_subscriptions(account_ids={account_id}, force=True)
    return await replay_xchat_raw_events(account_id, include_permanent=True)


async def note_xchat_dispatch(raw_event_id: uuid.UUID) -> None:
    now = datetime.now(UTC)
    async with get_session_factory()() as session:
        row = (
            await session.execute(
                select(models.RawEvent)
                .where(
                    models.RawEvent.id == raw_event_id,
                    models.RawEvent.processing_status.in_(_SCHEDULER_STATUSES),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            return
        row.processing_last_dispatched_at = now
        row.processing_next_attempt_at = now + _DISPATCH_RESERVATION
        await session.commit()


async def replay_xchat_raw_events(
    account_id: uuid.UUID,
    *,
    limit: int = _REPLAY_BATCH_SIZE,
    include_permanent: bool = False,
) -> list[str]:
    raw_event_ids = await _reserve_replay(
        account_id,
        limit=limit,
        include_permanent=include_permanent,
    )

    from social_reply.application.event_ingestion.xchat_actors import process_xchat_event
    from social_reply.application.event_ingestion.xchat_webhook import process_xchat_raw_event

    dispatched: list[str] = []
    for raw_event_id in raw_event_ids:
        try:
            await dispatch_actor(
                process_xchat_event,
                str(raw_event_id),
                str(account_id),
                inline=lambda raw_event_id=raw_event_id: process_xchat_raw_event(
                    raw_event_id,
                    account_id,
                ),
            )
        except Exception:  # noqa: BLE001 - keep the remaining replay batch dispatchable
            await _release_dispatch_reservation(raw_event_id)
            logger.exception(
                "XChat replay dispatch failed raw_event_id=%s account=%s",
                raw_event_id,
                account_id,
            )
            continue
        dispatched.append(str(raw_event_id))
    return dispatched


async def _reserve_replay(
    account_id: uuid.UUID,
    *,
    limit: int,
    include_permanent: bool,
) -> list[uuid.UUID]:
    now = datetime.now(UTC)
    statuses = (
        (*_SCHEDULER_STATUSES, *_ACTIVATION_ONLY_STATUSES)
        if include_permanent
        else _SCHEDULER_STATUSES
    )
    async with get_session_factory()() as session:
        rows = list(
            (
                await session.execute(
                    select(models.RawEvent)
                    .where(
                        models.RawEvent.platform_account_id == account_id,
                        models.RawEvent.processing_status.in_(statuses),
                    )
                    .order_by(models.RawEvent.received_at)
                    .limit(max(limit * 4, limit))
                    .with_for_update(skip_locked=True)
                )
            ).scalars()
        )
        selected: list[uuid.UUID] = []
        for row in rows:
            if row.processing_next_attempt_at and row.processing_next_attempt_at > now:
                continue
            if row.processing_status in _ACTIVATION_ONLY_STATUSES:
                row.processing_status = "XCHAT_RETRYABLE_ERROR"
                row.processing_error_code = "XCHAT_ACTIVATION_REPLAY"
            row.processing_last_dispatched_at = now
            row.processing_next_attempt_at = now + _DISPATCH_RESERVATION
            selected.append(row.id)
            if len(selected) >= limit:
                break
        await session.commit()
    return selected


async def _release_dispatch_reservation(raw_event_id: uuid.UUID) -> None:
    async with get_session_factory()() as session:
        row = (
            await session.execute(
                select(models.RawEvent)
                .where(models.RawEvent.id == raw_event_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            return
        row.processing_error_code = "XCHAT_DISPATCH_FAILED"
        row.processing_next_attempt_at = datetime.now(UTC) + timedelta(seconds=30)
        await session.commit()


async def _recover_expired_claims(*, limit: int = _REPLAY_BATCH_SIZE) -> list[str]:
    now = datetime.now(UTC)
    recovered: list[str] = []
    async with get_session_factory()() as session:
        rows = list(
            (
                await session.execute(
                    select(models.RawEvent)
                    .where(models.RawEvent.processing_status == "XCHAT_PROCESSING")
                    .order_by(models.RawEvent.received_at)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            ).scalars()
        )
        for row in rows:
            if row.processing_claim_expires_at and row.processing_claim_expires_at > now:
                continue
            row.processing_claim_token = None
            row.processing_claim_expires_at = None
            row.processing_error_code = "XCHAT_WORKER_LEASE_EXPIRED"
            if int(row.processing_attempt_count or 0) >= _MAX_RETRY_ATTEMPTS:
                row.processing_status = "XCHAT_RETRY_EXHAUSTED"
                row.processing_next_attempt_at = None
                row.processed_at = now
            else:
                row.processing_status = "XCHAT_RETRYABLE_ERROR"
                row.processing_next_attempt_at = now
                row.processed_at = None
            recovered.append(str(row.id))
        await session.commit()
    return recovered


async def sweep_xchat_recovery() -> list[str]:
    global _last_sweep_at
    now = time.monotonic()
    if _last_sweep_at is not None and now - _last_sweep_at < _SWEEP_INTERVAL_SECONDS:
        return []
    _last_sweep_at = now

    recovered = await _recover_expired_claims()
    async with get_session_factory()() as session:
        account_ids = list(
            (
                await session.execute(
                    select(models.RawEvent.platform_account_id)
                    .join(
                        models.PlatformAccount,
                        models.PlatformAccount.id == models.RawEvent.platform_account_id,
                    )
                    .where(
                        models.RawEvent.processing_status.in_(_SCHEDULER_STATUSES),
                        models.RawEvent.platform_account_id.is_not(None),
                        models.PlatformAccount.status.in_(("active", "CONNECTED")),
                        models.PlatformAccount.capability["x_chat"].as_boolean().is_(True),
                    )
                    .distinct()
                    .limit(20)
                )
            ).scalars()
        )
    replayed = list(recovered)
    for account_id in account_ids:
        replayed.extend(await replay_xchat_raw_events(account_id))
    return replayed
