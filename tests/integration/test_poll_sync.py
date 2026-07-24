import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, select, update

from social_reply.application.event_ingestion.poll_sync import (
    CheckpointStream,
    GapSpec,
    GapType,
    claim_checkpoint,
    complete_checkpoint,
    ensure_checkpoint,
    fail_run,
    record_gap,
    renew_claim,
)
from social_reply.infrastructure.database import models

pytestmark = pytest.mark.integration


async def _seed_account(session, *, config: dict | None = None) -> uuid.UUID:
    account_id = uuid.uuid4()
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            tenant_id="tenant-a",
            brand_id="b1",
            platform="x",
            name="x",
            status="active",
            capability={"dm": True, "x_chat": True},
            config=config or {},
        )
    )
    await session.commit()
    return account_id


async def test_ensure_checkpoint_is_concurrent_and_preserves_initial_cursor(session):
    account_id = await _seed_account(session)

    first, second = await asyncio.gather(
        ensure_checkpoint(
            tenant_id="tenant-a",
            platform_account_id=account_id,
            stream=CheckpointStream.X_LEGACY_DM,
            initial_cursor="100",
        ),
        ensure_checkpoint(
            tenant_id="tenant-a",
            platform_account_id=account_id,
            stream=CheckpointStream.X_LEGACY_DM,
            initial_cursor="200",
        ),
    )

    rows = (
        (
            await session.execute(
                select(models.PlatformCheckpoint).where(
                    models.PlatformCheckpoint.platform_account_id == account_id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert first.id == second.id == rows[0].id
    assert rows[0].cursor in {"100", "200"}
    assert rows[0].bootstrapped is True


async def test_only_one_scheduler_claims_checkpoint(session):
    account_id = await _seed_account(session)
    checkpoint = await ensure_checkpoint(
        tenant_id="tenant-a",
        platform_account_id=account_id,
        stream=CheckpointStream.X_LEGACY_DM,
    )

    left, right = await asyncio.gather(
        claim_checkpoint(checkpoint.id, owner="scheduler-a"),
        claim_checkpoint(checkpoint.id, owner="scheduler-b"),
    )

    assert sum(claim is not None for claim in (left, right)) == 1
    runs = (
        (
            await session.execute(
                select(models.SyncRun).where(models.SyncRun.checkpoint_id == checkpoint.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(runs) == 1
    assert runs[0].status == "RUNNING"


async def test_expired_lease_fences_old_run(session):
    account_id = await _seed_account(session)
    checkpoint = await ensure_checkpoint(
        tenant_id="tenant-a",
        platform_account_id=account_id,
        stream=CheckpointStream.X_LEGACY_DM,
        initial_cursor="100",
    )
    old_claim = await claim_checkpoint(checkpoint.id, owner="scheduler-a")
    assert old_claim is not None
    await session.execute(
        update(models.PlatformCheckpoint)
        .where(models.PlatformCheckpoint.id == checkpoint.id)
        .values(claim_expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    await session.commit()

    new_claim = await claim_checkpoint(checkpoint.id, owner="scheduler-b")
    assert new_claim is not None
    assert new_claim.claim_token != old_claim.claim_token
    assert not await renew_claim(old_claim)
    assert await renew_claim(new_claim)
    assert not await complete_checkpoint(
        old_claim,
        cursor="200",
        bootstrapped=True,
        interval_seconds=0,
        page_count=1,
        occurrence_count=1,
    )

    session.expire_all()
    row = await session.get(models.PlatformCheckpoint, checkpoint.id)
    old_run = await session.get(models.SyncRun, old_claim.run_id)
    assert row.cursor == "100"
    assert row.claim_token == new_claim.claim_token
    assert old_run.status == "LEASE_LOST"


async def test_expired_claim_cannot_renew_or_finalize_before_takeover(session):
    account_id = await _seed_account(session)
    checkpoint = await ensure_checkpoint(
        tenant_id="tenant-a",
        platform_account_id=account_id,
        stream=CheckpointStream.X_LEGACY_DM,
        initial_cursor="100",
    )
    claim = await claim_checkpoint(checkpoint.id, owner="scheduler-a")
    assert claim is not None
    await session.execute(
        update(models.PlatformCheckpoint)
        .where(models.PlatformCheckpoint.id == checkpoint.id)
        .values(claim_expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    await session.commit()

    assert not await renew_claim(claim)
    assert not await complete_checkpoint(
        claim,
        cursor="200",
        bootstrapped=True,
        interval_seconds=0,
        page_count=1,
        occurrence_count=1,
    )
    assert not await record_gap(
        claim,
        GapSpec(gap_type=GapType.PAGE_CAP, candidate_cursor="200"),
        retry_after_seconds=0,
        page_count=1,
        occurrence_count=1,
    )
    assert not await fail_run(
        claim,
        error_code="FAILED",
        error_message="late worker",
        retry_after_seconds=0,
    )

    session.expire_all()
    row = await session.get(models.PlatformCheckpoint, checkpoint.id)
    run = await session.get(models.SyncRun, claim.run_id)
    assert row.cursor == "100"
    assert row.claim_token == claim.claim_token
    assert run.status == "LEASE_LOST"


async def test_gap_retries_and_resolves_atomically(session):
    account_id = await _seed_account(session)
    checkpoint = await ensure_checkpoint(
        tenant_id="tenant-a",
        platform_account_id=account_id,
        stream=CheckpointStream.X_LEGACY_DM,
        initial_cursor="100",
    )
    first_claim = await claim_checkpoint(checkpoint.id, owner="scheduler-a")
    assert first_claim is not None
    assert await record_gap(
        first_claim,
        GapSpec(
            gap_type=GapType.PAGE_CAP,
            candidate_cursor="300",
            resume_token="next-page",
            detail={"page_index": 2},
        ),
        retry_after_seconds=0,
        page_count=3,
        occurrence_count=3,
    )

    session.expire_all()
    row = await session.get(models.PlatformCheckpoint, checkpoint.id)
    gap = (
        await session.execute(
            select(models.SyncGap).where(models.SyncGap.checkpoint_id == checkpoint.id)
        )
    ).scalar_one()
    first_run = await session.get(models.SyncRun, first_claim.run_id)
    assert row.cursor == "100"
    assert row.claim_token is None
    assert first_run.status == "GAPPED"
    assert gap.status == "OPEN"
    assert gap.resume_token == "next-page"
    gap_id = gap.id

    retry_claim = await claim_checkpoint(checkpoint.id, owner="scheduler-b")
    assert retry_claim is not None
    assert retry_claim.mode == "BACKFILL"
    assert retry_claim.active_gap is not None
    assert retry_claim.active_gap.candidate_cursor == "300"
    assert retry_claim.active_gap.resume_token == "next-page"
    assert await complete_checkpoint(
        retry_claim,
        cursor="300",
        bootstrapped=True,
        interval_seconds=60,
        page_count=1,
        occurrence_count=1,
    )

    session.expire_all()
    row = await session.get(models.PlatformCheckpoint, checkpoint.id)
    gap = await session.get(models.SyncGap, gap_id)
    retry_run = await session.get(models.SyncRun, retry_claim.run_id)
    assert row.cursor == "300"
    assert row.claim_token is None
    assert gap.status == "RESOLVED"
    assert gap.resolved_at is not None
    assert retry_run.status == "SUCCEEDED"
    assert retry_run.cursor_after == "300"


async def test_failed_backfill_reopens_gap_without_advancing_cursor(session):
    account_id = await _seed_account(session)
    checkpoint = await ensure_checkpoint(
        tenant_id="tenant-a",
        platform_account_id=account_id,
        stream=CheckpointStream.XCHAT_CONVERSATION,
        scope_key="conversation-1",
        initial_cursor="100",
    )
    first_claim = await claim_checkpoint(checkpoint.id, owner="scheduler-a")
    assert first_claim is not None
    await record_gap(
        first_claim,
        GapSpec(gap_type=GapType.DECRYPT_ERROR, candidate_cursor="200"),
        retry_after_seconds=0,
        page_count=1,
        occurrence_count=1,
    )
    retry_claim = await claim_checkpoint(checkpoint.id, owner="scheduler-b")
    assert retry_claim is not None and retry_claim.active_gap is not None

    assert await fail_run(
        retry_claim,
        error_code="X_API_FAILED",
        error_message="temporary",
        retry_after_seconds=0,
        page_count=0,
        occurrence_count=0,
    )

    session.expire_all()
    row = await session.get(models.PlatformCheckpoint, checkpoint.id)
    gap = await session.get(models.SyncGap, retry_claim.active_gap.id)
    run = await session.get(models.SyncRun, retry_claim.run_id)
    assert row.cursor == "100"
    assert row.claim_token is None
    assert gap.status == "OPEN"
    assert run.status == "FAILED"
