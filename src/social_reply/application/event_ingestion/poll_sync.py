import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory


class LeaseLostError(RuntimeError):
    pass


class CheckpointStream(StrEnum):
    X_LEGACY_DM = "X_LEGACY_DM"
    XCHAT_DISCOVERY = "XCHAT_DISCOVERY"
    XCHAT_CONVERSATION = "XCHAT_CONVERSATION"


class GapType(StrEnum):
    PAGE_CAP = "PAGE_CAP"
    PAGINATION_ERROR = "PAGINATION_ERROR"
    DECRYPT_ERROR = "DECRYPT_ERROR"


@dataclass(frozen=True)
class ActiveGap:
    id: uuid.UUID
    gap_type: str
    candidate_cursor: str | None
    resume_token: str | None
    detail: dict[str, Any]
    attempt_count: int


@dataclass(frozen=True)
class ClaimedCheckpoint:
    id: uuid.UUID
    run_id: uuid.UUID
    claim_token: uuid.UUID
    revision: int
    cursor: str | None
    bootstrapped: bool
    mode: str
    active_gap: ActiveGap | None


@dataclass(frozen=True)
class GapSpec:
    gap_type: GapType
    candidate_cursor: str | None = None
    resume_token: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


async def ensure_checkpoint(
    *,
    tenant_id: str,
    platform_account_id: uuid.UUID,
    stream: CheckpointStream,
    scope_key: str = "",
    initial_cursor: str | None = None,
    initial_bootstrapped: bool = False,
) -> models.PlatformCheckpoint:
    values = {
        "id": uuid.uuid4(),
        "tenant_id": tenant_id,
        "platform_account_id": platform_account_id,
        "stream": stream.value,
        "scope_key": scope_key,
        "cursor": initial_cursor,
        "bootstrapped": initial_bootstrapped or initial_cursor is not None,
        "revision": 0,
    }
    async with get_session_factory()() as session:
        await session.execute(
            insert(models.PlatformCheckpoint)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=[
                    models.PlatformCheckpoint.platform_account_id,
                    models.PlatformCheckpoint.stream,
                    models.PlatformCheckpoint.scope_key,
                ]
            )
        )
        checkpoint = (
            await session.execute(
                select(models.PlatformCheckpoint).where(
                    models.PlatformCheckpoint.platform_account_id == platform_account_id,
                    models.PlatformCheckpoint.stream == stream.value,
                    models.PlatformCheckpoint.scope_key == scope_key,
                )
            )
        ).scalar_one()
        await session.commit()
        return checkpoint


async def claim_checkpoint(
    checkpoint_id: uuid.UUID,
    *,
    owner: str,
    lease_seconds: int = 300,
) -> ClaimedCheckpoint | None:
    claim_token = uuid.uuid4()
    run_id = uuid.uuid4()
    async with get_session_factory()() as session:
        now = await _database_now(session)
        checkpoint = (
            await session.execute(
                update(models.PlatformCheckpoint)
                .where(
                    models.PlatformCheckpoint.id == checkpoint_id,
                    or_(
                        models.PlatformCheckpoint.next_attempt_at.is_(None),
                        models.PlatformCheckpoint.next_attempt_at <= now,
                    ),
                    or_(
                        models.PlatformCheckpoint.claim_expires_at.is_(None),
                        models.PlatformCheckpoint.claim_expires_at <= now,
                    ),
                )
                .values(
                    claim_token=claim_token,
                    claimed_by=owner,
                    claim_expires_at=now + timedelta(seconds=lease_seconds),
                    revision=models.PlatformCheckpoint.revision + 1,
                    updated_at=now,
                )
                .returning(models.PlatformCheckpoint)
            )
        ).scalar_one_or_none()
        if checkpoint is None:
            await session.rollback()
            return None

        await session.execute(
            update(models.SyncRun)
            .where(
                models.SyncRun.checkpoint_id == checkpoint_id,
                models.SyncRun.status == "RUNNING",
            )
            .values(
                status="LEASE_LOST",
                error_code="LEASE_EXPIRED",
                finished_at=now,
            )
        )
        gap = (
            await session.execute(
                select(models.SyncGap)
                .where(
                    models.SyncGap.checkpoint_id == checkpoint_id,
                    models.SyncGap.status.in_(("OPEN", "RETRYING")),
                    or_(
                        models.SyncGap.next_attempt_at.is_(None),
                        models.SyncGap.next_attempt_at <= now,
                    ),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        active_gap = None
        mode = "POLL"
        if gap is not None:
            mode = "BACKFILL"
            gap.status = "RETRYING"
            gap.attempt_count += 1
            gap.updated_at = now
            active_gap = ActiveGap(
                id=gap.id,
                gap_type=gap.gap_type,
                candidate_cursor=gap.candidate_cursor,
                resume_token=gap.resume_token,
                detail=dict(gap.detail or {}),
                attempt_count=gap.attempt_count,
            )
        session.add(
            models.SyncRun(
                id=run_id,
                checkpoint_id=checkpoint_id,
                claim_token=claim_token,
                mode=mode,
                status="RUNNING",
                cursor_before=checkpoint.cursor,
                resume_token=active_gap.resume_token if active_gap else None,
            )
        )
        await session.commit()
        return ClaimedCheckpoint(
            id=checkpoint.id,
            run_id=run_id,
            claim_token=claim_token,
            revision=checkpoint.revision,
            cursor=checkpoint.cursor,
            bootstrapped=checkpoint.bootstrapped,
            mode=mode,
            active_gap=active_gap,
        )


async def renew_claim(
    claim: ClaimedCheckpoint,
    *,
    lease_seconds: int = 300,
) -> bool:
    async with get_session_factory()() as session:
        now = await _database_now(session)
        result = await session.execute(
            update(models.PlatformCheckpoint)
            .where(
                models.PlatformCheckpoint.id == claim.id,
                models.PlatformCheckpoint.claim_token == claim.claim_token,
                models.PlatformCheckpoint.revision == claim.revision,
                models.PlatformCheckpoint.claim_expires_at > now,
            )
            .values(
                claim_expires_at=now + timedelta(seconds=lease_seconds),
                updated_at=now,
            )
        )
        await session.commit()
        return result.rowcount == 1


async def require_claim(claim: ClaimedCheckpoint) -> None:
    if not await renew_claim(claim):
        raise LeaseLostError(f"sync_lease_lost:{claim.id}")


async def complete_checkpoint(
    claim: ClaimedCheckpoint,
    *,
    cursor: str | None,
    bootstrapped: bool,
    interval_seconds: int,
    page_count: int,
    occurrence_count: int,
) -> bool:
    async with get_session_factory()() as session:
        now = await _database_now(session)
        result = await session.execute(
            update(models.PlatformCheckpoint)
            .where(
                models.PlatformCheckpoint.id == claim.id,
                models.PlatformCheckpoint.claim_token == claim.claim_token,
                models.PlatformCheckpoint.revision == claim.revision,
                models.PlatformCheckpoint.claim_expires_at > now,
            )
            .values(
                cursor=cursor,
                bootstrapped=bootstrapped,
                next_attempt_at=now + timedelta(seconds=max(interval_seconds, 0)),
                claim_token=None,
                claimed_by=None,
                claim_expires_at=None,
                last_success_at=now,
                revision=models.PlatformCheckpoint.revision + 1,
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            await _mark_run_lease_lost(session, claim.run_id, now)
            await session.commit()
            return False
        await session.execute(
            update(models.SyncRun)
            .where(models.SyncRun.id == claim.run_id, models.SyncRun.status == "RUNNING")
            .values(
                status="SUCCEEDED",
                cursor_after=cursor,
                page_count=page_count,
                occurrence_count=occurrence_count,
                finished_at=now,
            )
        )
        await session.execute(
            update(models.SyncGap)
            .where(
                models.SyncGap.checkpoint_id == claim.id,
                models.SyncGap.status.in_(("OPEN", "RETRYING")),
            )
            .values(status="RESOLVED", resolved_at=now, updated_at=now)
        )
        await session.commit()
        return True


async def record_gap(
    claim: ClaimedCheckpoint,
    gap_spec: GapSpec,
    *,
    retry_after_seconds: int,
    page_count: int,
    occurrence_count: int,
) -> bool:
    async with get_session_factory()() as session:
        now = await _database_now(session)
        next_attempt_at = now + timedelta(seconds=max(retry_after_seconds, 0))
        result = await session.execute(
            update(models.PlatformCheckpoint)
            .where(
                models.PlatformCheckpoint.id == claim.id,
                models.PlatformCheckpoint.claim_token == claim.claim_token,
                models.PlatformCheckpoint.revision == claim.revision,
                models.PlatformCheckpoint.claim_expires_at > now,
            )
            .values(
                next_attempt_at=next_attempt_at,
                claim_token=None,
                claimed_by=None,
                claim_expires_at=None,
                revision=models.PlatformCheckpoint.revision + 1,
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            await _mark_run_lease_lost(session, claim.run_id, now)
            await session.commit()
            return False

        gap = (
            await session.execute(
                select(models.SyncGap)
                .where(
                    models.SyncGap.checkpoint_id == claim.id,
                    models.SyncGap.status.in_(("OPEN", "RETRYING")),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        candidate_cursor = _max_cursor(
            gap.candidate_cursor if gap is not None else None,
            gap_spec.candidate_cursor,
        )
        if gap is None:
            session.add(
                models.SyncGap(
                    id=uuid.uuid4(),
                    checkpoint_id=claim.id,
                    sync_run_id=claim.run_id,
                    gap_type=gap_spec.gap_type.value,
                    status="OPEN",
                    cursor_before=claim.cursor,
                    candidate_cursor=candidate_cursor,
                    resume_token=gap_spec.resume_token,
                    detail=dict(gap_spec.detail),
                    next_attempt_at=next_attempt_at,
                )
            )
        else:
            gap.sync_run_id = claim.run_id
            gap.gap_type = gap_spec.gap_type.value
            gap.status = "OPEN"
            gap.candidate_cursor = candidate_cursor
            gap.resume_token = gap_spec.resume_token
            gap.detail = dict(gap_spec.detail)
            gap.next_attempt_at = next_attempt_at
            gap.updated_at = now
        await session.execute(
            update(models.SyncRun)
            .where(models.SyncRun.id == claim.run_id, models.SyncRun.status == "RUNNING")
            .values(
                status="GAPPED",
                page_count=page_count,
                occurrence_count=occurrence_count,
                error_code=gap_spec.gap_type.value,
                finished_at=now,
            )
        )
        await session.commit()
        return True


async def fail_run(
    claim: ClaimedCheckpoint,
    *,
    error_code: str,
    error_message: str,
    retry_after_seconds: int,
    page_count: int = 0,
    occurrence_count: int = 0,
) -> bool:
    async with get_session_factory()() as session:
        now = await _database_now(session)
        result = await session.execute(
            update(models.PlatformCheckpoint)
            .where(
                models.PlatformCheckpoint.id == claim.id,
                models.PlatformCheckpoint.claim_token == claim.claim_token,
                models.PlatformCheckpoint.revision == claim.revision,
                models.PlatformCheckpoint.claim_expires_at > now,
            )
            .values(
                next_attempt_at=now + timedelta(seconds=max(retry_after_seconds, 0)),
                claim_token=None,
                claimed_by=None,
                claim_expires_at=None,
                revision=models.PlatformCheckpoint.revision + 1,
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            await _mark_run_lease_lost(session, claim.run_id, now)
            await session.commit()
            return False
        await session.execute(
            update(models.SyncRun)
            .where(models.SyncRun.id == claim.run_id, models.SyncRun.status == "RUNNING")
            .values(
                status="FAILED",
                page_count=page_count,
                occurrence_count=occurrence_count,
                error_code=error_code[:128],
                error_message=error_message[:1000],
                finished_at=now,
            )
        )
        if claim.active_gap is not None:
            await session.execute(
                update(models.SyncGap)
                .where(models.SyncGap.id == claim.active_gap.id)
                .values(
                    status="OPEN",
                    next_attempt_at=now + timedelta(seconds=max(retry_after_seconds, 0)),
                    updated_at=now,
                )
            )
        await session.commit()
        return True


async def _database_now(session) -> datetime:
    return (await session.execute(select(func.clock_timestamp()))).scalar_one()


async def _mark_run_lease_lost(session, run_id: uuid.UUID, now: datetime) -> None:
    await session.execute(
        update(models.SyncRun)
        .where(models.SyncRun.id == run_id, models.SyncRun.status == "RUNNING")
        .values(status="LEASE_LOST", error_code="LEASE_LOST", finished_at=now)
    )


def _max_cursor(left: str | None, right: str | None) -> str | None:
    values = [value for value in (left, right) if value is not None]
    if not values:
        return None
    try:
        return str(max(int(value) for value in values))
    except ValueError:
        return right or left
