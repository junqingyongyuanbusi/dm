import asyncio
import uuid
from unittest.mock import AsyncMock

import httpx
import pytest
from tests.integration.company_permission_support import create_staff

from social_reply.application.account_management import jobs
from social_reply.application.account_management.service import AccountConnectionResult
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("winner_state", ["PENDING", "PROCESSING", "COMPLETED"])
async def test_same_key_resubmission_reloads_job_after_waiting_for_authority(
    session, migrated_db, monkeypatch, winner_state
):
    owner = await create_staff(session, role="OPERATOR")
    submission = dict(
        tenant_id="default",
        brand_id="default",
        platform="telegram",
        actor=owner.principal.actor,
        admin_session_id=owner.principal.session_id,
        request={"idempotency_key": f"resubmit-{uuid.uuid4()}"},
        secrets={"token": "test-token"},
    )
    job_id = await jobs.submit_provisioning_job(**submission)

    async def unavailable(_job):
        raise httpx.ConnectError("temporarily unavailable")

    monkeypatch.setattr(jobs, "_connect", unavailable)
    assert await jobs.process_provisioning_job(str(job_id)) == "FAILED"
    original_lock = jobs._lock_staff_and_sessions
    discovered = asyncio.Event()
    release_second = asyncio.Event()
    arrivals = set()

    async def controlled_lock(lock_session, *, staff_ids, session_ids):
        task = asyncio.current_task()
        name = task.get_name() if task is not None else ""
        if name in {"resubmit-first", "resubmit-second"}:
            arrivals.add(name)
            if len(arrivals) == 2:
                discovered.set()
            await asyncio.wait_for(discovered.wait(), timeout=5)
            if name == "resubmit-second":
                await asyncio.wait_for(release_second.wait(), timeout=10)
        await original_lock(lock_session, staff_ids=staff_ids, session_ids=session_ids)

    monkeypatch.setattr(jobs, "_lock_staff_and_sessions", controlled_lock)
    first = asyncio.create_task(jobs.submit_provisioning_job(**submission), name="resubmit-first")
    second = asyncio.create_task(jobs.submit_provisioning_job(**submission), name="resubmit-second")
    try:
        await asyncio.wait_for(discovered.wait(), timeout=5)
        assert await asyncio.wait_for(first, timeout=5) == job_id
        if winner_state == "PROCESSING":
            claimed = await jobs._claim_job(job_id)
            assert claimed is not None and claimed.status == "PROCESSING"
        elif winner_state == "COMPLETED":
            # Only the connector is a stand-in; worker claim and completion writes are real.
            account = models.PlatformAccount(
                tenant_id="default",
                brand_id="default",
                platform="telegram",
                owner_user_id=owner.user_id,
                name="Completed resubmission",
                external_account_id=f"tg-{uuid.uuid4()}",
                public_id=f"tg-{uuid.uuid4()}",
                status="active",
                config={},
                capability={},
                automation_default="BOT_DRAFT_ONLY",
            )
            session.add(account)
            await session.commit()
            result = AccountConnectionResult(
                account_id=account.id,
                platform="telegram",
                external_account_id=account.external_account_id,
                public_id=account.public_id,
                webhook_url="https://example.test/hook",
                name=account.name,
                automation_default="BOT_DRAFT_ONLY",
            )
            monkeypatch.setattr(jobs, "_connect", AsyncMock(return_value=result))
            assert await jobs.process_provisioning_job(str(job_id)) == "COMPLETED"
        before = await _snapshot(job_id)
        assert before[0] == winner_state
        release_second.set()
        with pytest.raises(ValueError, match="provisioning_job_not_retryable"):
            await asyncio.wait_for(second, timeout=5)
        assert await _snapshot(job_id) == before
    finally:
        release_second.set()
        for task in (first, second):
            if not task.done():
                task.cancel()
        await asyncio.gather(first, second, return_exceptions=True)


async def _snapshot(job_id):
    async with get_session_factory()() as session:
        job = await session.get(models.ProvisioningJob, job_id)
        return (
            job.status,
            job.current_step,
            job.attempt_count,
            job.locked_at,
            job.locked_by,
            job.staging_secret,
            job.result,
            job.completed_at,
        )
