import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, update

from social_reply.application.account_management import jobs
from social_reply.application.account_management.service import AccountConnectionResult
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.secret_crypto import decrypt_secret_bundle, encrypt_secret_bundle


async def test_submit_job_stages_secret_inline_not_in_request(migrated_db, tmp_path, monkeypatch):
    from social_reply.shared.config import get_settings

    monkeypatch.setenv("ACCOUNT_SECRETS_ROOT", str(tmp_path))
    get_settings.cache_clear()
    job_id = await jobs.submit_provisioning_job(
        tenant_id="tenant-a",
        brand_id="brand-a",
        platform="telegram",
        actor="user:admin",
        request={"name": "Bot A", "idempotency_key": "tenant-a-bot-a"},
        secrets={"token": "super-secret-token"},
    )
    async with get_session_factory()() as session:
        row = await session.get(models.ProvisioningJob, job_id)
    assert row is not None
    # request 经 _safe_request 脱敏，不含 secret
    assert row.request == {"name": "Bot A"}
    assert "super-secret-token" not in str(row.request)
    assert row.staging_secret != {"token": "super-secret-token"}
    assert "super-secret-token" not in str(row.staging_secret)
    assert decrypt_secret_bundle(row.staging_secret) == {"token": "super-secret-token"}
    # public_job 白名单输出不得暴露 staging secret
    assert "super-secret-token" not in str(jobs.public_job(row))
    get_settings.cache_clear()


async def test_process_job_completes_and_deletes_staging_secret(migrated_db, tmp_path, monkeypatch):
    from social_reply.shared.config import get_settings

    monkeypatch.setenv("ACCOUNT_SECRETS_ROOT", str(tmp_path))
    get_settings.cache_clear()
    account_id = uuid.uuid4()
    async with get_session_factory()() as session:
        session.add(
            models.PlatformAccount(
                id=account_id,
                tenant_id="tenant-a",
                brand_id="brand-a",
                platform="telegram",
                name="Bot",
                external_account_id="42",
                public_id="tg_public",
                credential_bundle=encrypt_secret_bundle({"bot_token": "not-read"}),
                config={"delivery_mode": "direct"},
                capability={},
                status="active",
            )
        )
        await session.commit()

    async def fake_connect(_job):
        return AccountConnectionResult(
            account_id=account_id,
            platform="telegram",
            external_account_id="42",
            public_id="tg_public",
            webhook_url="https://reply.example.com/webhooks/telegram/tg_public",
            name="Bot",
            automation_default="BOT_DRAFT_ONLY",
        )

    monkeypatch.setattr(jobs, "_connect", fake_connect)
    job_id = await jobs.submit_provisioning_job(
        tenant_id="tenant-a",
        brand_id="brand-a",
        platform="telegram",
        actor="user:admin",
        request={"name": "Bot", "idempotency_key": "complete-bot"},
        secrets={"token": "secret"},
    )
    async with get_session_factory()() as session:
        before = await session.get(models.ProvisioningJob, job_id)
        assert decrypt_secret_bundle(before.staging_secret) == {"token": "secret"}

    assert await jobs.process_provisioning_job(str(job_id)) == "COMPLETED"
    async with get_session_factory()() as session:
        row = await session.get(models.ProvisioningJob, job_id)
    assert row.status == "COMPLETED"
    assert row.account_id == account_id
    assert row.result["webhook_url"].endswith("/tg_public")
    # 完成后内联 staging secret 被清除
    assert row.staging_secret is None
    get_settings.cache_clear()


async def test_disabled_platform_job_pauses_without_attempt_and_recovers(migrated_db, monkeypatch):
    disabled = jobs.get_settings().model_copy(update={"facebook_messenger_enabled": False})
    monkeypatch.setattr(jobs, "get_settings", lambda: disabled)
    job_id = await jobs.submit_provisioning_job(
        tenant_id="tenant-a",
        brand_id="brand-a",
        platform="facebook",
        actor="user:admin",
        request={
            "external_account_id": "page-1",
            "idempotency_key": "disabled-facebook",
        },
        secrets={
            "access_token": "token",
            "app_secret": "secret",
            "verify_token": "verify",
        },
    )

    assert await jobs.process_provisioning_job(str(job_id)) == "PAUSED_PLATFORM_DISABLED"
    async with get_session_factory()() as session:
        paused = await session.get(models.ProvisioningJob, job_id)
    assert paused.status == "PAUSED_PLATFORM_DISABLED"
    assert paused.attempt_count == 0
    assert paused.last_error_code == "FACEBOOK_MESSENGER_DISABLED"
    assert decrypt_secret_bundle(paused.staging_secret)["access_token"] == "token"

    enabled = disabled.model_copy(update={"facebook_messenger_enabled": True})
    monkeypatch.setattr(jobs, "get_settings", lambda: enabled)
    dispatched = []

    async def dispatch(_actor, pending_id: str, **_kwargs):
        dispatched.append(pending_id)

    monkeypatch.setattr(jobs, "dispatch_actor", dispatch)
    assert job_id in await jobs.sweep_provisioning_jobs()
    assert dispatched == [str(job_id)]
    async with get_session_factory()() as session:
        recovered = await session.get(models.ProvisioningJob, job_id)
    assert recovered.status == "PENDING"
    assert recovered.attempt_count == 0
    assert recovered.last_error_code is None


async def test_xchat_failure_requires_pin_resubmission_instead_of_auto_retry(
    migrated_db, monkeypatch
):
    async def fail_connect(_job):
        request = httpx.Request("GET", "https://api.x.com/2/users/me")
        raise httpx.ConnectError("connection failed", request=request)

    monkeypatch.setattr(jobs, "_connect", fail_connect)
    job_id = await jobs.submit_provisioning_job(
        tenant_id="tenant-a",
        brand_id="brand-a",
        platform="x",
        actor="user:admin",
        request={"environment": "oauth", "idempotency_key": "xchat-retry"},
        secrets={
            "consumer_key": "ck",
            "consumer_secret": "cs",
            "access_token": "at",
            "access_token_secret": "ats",
            "xchat_pin": "1234",
        },
    )

    assert await jobs.process_provisioning_job(str(job_id)) == "NEEDS_ACTION"
    assert await jobs.process_provisioning_job(str(job_id)) == "SKIPPED_NOT_CLAIMABLE"

    async with get_session_factory()() as session:
        row = await session.get(models.ProvisioningJob, job_id)
    assert row.status == "NEEDS_ACTION"
    assert row.next_attempt_at is None
    assert row.last_error_code == "PLATFORM_UNAVAILABLE"
    assert row.account_id is None
    assert row.result == {
        "requires_secret_resubmission": True,
        "required_secret": "xchat_pin",
    }
    staged = decrypt_secret_bundle(row.staging_secret)
    assert "xchat_pin" not in staged
    assert staged["access_token"] == "at"

    with pytest.raises(ValueError, match="provisioning_secret_resubmission_required"):
        await jobs.retry_provisioning_job(job_id)

    with pytest.raises(ValueError, match="provisioning_secret_resubmission_required"):
        await jobs.submit_provisioning_job(
            tenant_id="tenant-a",
            brand_id="brand-a",
            platform="x",
            actor="user:admin",
            request={"environment": "oauth", "idempotency_key": "xchat-retry"},
            secrets={
                "consumer_key": "ck",
                "consumer_secret": "cs",
                "access_token": "at",
                "access_token_secret": "ats",
            },
        )
    async with get_session_factory()() as session:
        unchanged = await session.get(models.ProvisioningJob, job_id)
    assert unchanged.status == "NEEDS_ACTION"
    assert unchanged.result["requires_secret_resubmission"] is True

    resubmitted = await jobs.submit_provisioning_job(
        tenant_id="tenant-a",
        brand_id="brand-a",
        platform="x",
        actor="user:admin",
        request={"environment": "oauth", "idempotency_key": "xchat-retry"},
        secrets={
            "consumer_key": "ck",
            "consumer_secret": "cs",
            "access_token": "at",
            "access_token_secret": "ats",
            "xchat_pin": "5678",
        },
    )
    assert resubmitted == job_id
    async with get_session_factory()() as session:
        refreshed = await session.get(models.ProvisioningJob, job_id)
    assert refreshed.status == "PENDING"
    assert refreshed.result == {}
    assert decrypt_secret_bundle(refreshed.staging_secret)["xchat_pin"] == "5678"


async def test_stale_xchat_job_clears_pin_and_requires_resubmission(migrated_db):
    job_id = uuid.uuid4()
    async with get_session_factory()() as session:
        session.add(
            models.ProvisioningJob(
                id=job_id,
                tenant_id="tenant-a",
                brand_id="brand-a",
                platform="x",
                actor="user:admin",
                idempotency_key="stale-xchat",
                request={"environment": "oauth"},
                staging_secret=encrypt_secret_bundle(
                    {
                        "consumer_key": "ck",
                        "consumer_secret": "cs",
                        "access_token": "at",
                        "access_token_secret": "ats",
                        "xchat_pin": "1234",
                    }
                ),
                status="PROCESSING",
                current_step="VALIDATE_CREDENTIAL",
                locked_at=datetime.now(UTC) - timedelta(minutes=10),
                locked_by="dead-worker",
            )
        )
        await session.commit()

    recovered = await jobs.sweep_provisioning_jobs()

    assert job_id not in recovered
    async with get_session_factory()() as session:
        row = await session.get(models.ProvisioningJob, job_id)
    assert row.status == "NEEDS_ACTION"
    assert row.next_attempt_at is None
    assert row.locked_at is None
    assert row.last_error_code == "STALE_PROCESSING_SECRET_RESUBMISSION_REQUIRED"
    assert row.result == {
        "requires_secret_resubmission": True,
        "required_secret": "xchat_pin",
    }
    assert "xchat_pin" not in decrypt_secret_bundle(row.staging_secret)


async def test_provisioning_eighth_retryable_failure_requires_action(
    migrated_db,
    monkeypatch,
):
    async def fail_connect(_job):
        request = httpx.Request("GET", "https://api.telegram.org/getMe")
        raise httpx.ConnectError("connection failed", request=request)

    monkeypatch.setattr(jobs, "_connect", fail_connect)
    job_id = await jobs.submit_provisioning_job(
        tenant_id="tenant-a",
        brand_id="brand-a",
        platform="telegram",
        actor="user:admin",
        request={"idempotency_key": "retry-exhaustion"},
        secrets={"token": "secret"},
    )
    async with get_session_factory()() as session:
        await session.execute(
            update(models.ProvisioningJob)
            .where(models.ProvisioningJob.id == job_id)
            .values(attempt_count=jobs._MAX_ATTEMPTS - 1)
        )
        await session.commit()

    assert await jobs.process_provisioning_job(str(job_id)) == "NEEDS_ACTION"
    async with get_session_factory()() as session:
        exhausted = await session.get(models.ProvisioningJob, job_id)
    assert exhausted.attempt_count == jobs._MAX_ATTEMPTS
    assert exhausted.status == "NEEDS_ACTION"
    assert exhausted.last_error_code == "RETRY_EXHAUSTED"
    assert exhausted.next_attempt_at is None

    await jobs.retry_provisioning_job(job_id)
    async with get_session_factory()() as session:
        retried = await session.get(models.ProvisioningJob, job_id)
    assert retried.status == "PENDING"
    assert retried.attempt_count == 0
    assert await jobs.process_provisioning_job(str(job_id)) == "FAILED"
    async with get_session_factory()() as session:
        claimed = await session.get(models.ProvisioningJob, job_id)
    assert claimed.status == "FAILED"
    assert claimed.attempt_count == 1


async def test_exhausted_idempotent_resubmission_resets_attempt_budget(
    migrated_db,
):
    request = {"idempotency_key": "exhausted-resubmission"}
    job_id = await jobs.submit_provisioning_job(
        tenant_id="tenant-a",
        brand_id="brand-a",
        platform="telegram",
        actor="user:admin",
        request=request,
        secrets={"token": "secret-1"},
    )
    async with get_session_factory()() as session:
        await session.execute(
            update(models.ProvisioningJob)
            .where(models.ProvisioningJob.id == job_id)
            .values(
                status="NEEDS_ACTION",
                attempt_count=jobs._MAX_ATTEMPTS,
                last_error_code="RETRY_EXHAUSTED",
            )
        )
        await session.commit()

    assert (
        await jobs.submit_provisioning_job(
            tenant_id="tenant-a",
            brand_id="brand-a",
            platform="telegram",
            actor="user:admin",
            request=request,
            secrets={"token": "secret-2"},
        )
        == job_id
    )
    async with get_session_factory()() as session:
        reset = await session.get(models.ProvisioningJob, job_id)
    assert reset.status == "PENDING"
    assert reset.attempt_count == 0
    assert reset.last_error_code is None
    assert decrypt_secret_bundle(reset.staging_secret) == {"token": "secret-2"}


async def test_stale_provisioning_worker_cannot_overwrite_new_attempt(
    migrated_db,
    monkeypatch,
):
    account_id = uuid.uuid4()
    async with get_session_factory()() as session:
        session.add(
            models.PlatformAccount(
                id=account_id,
                tenant_id="tenant-a",
                brand_id="brand-a",
                platform="telegram",
                name="Bot",
                external_account_id="42",
                public_id="stale_fence_bot",
                credential_bundle=encrypt_secret_bundle({"bot_token": "not-read"}),
                config={"delivery_mode": "direct"},
                capability={},
                status="active",
            )
        )
        await session.commit()

    starts = [asyncio.Event(), asyncio.Event()]
    releases = [asyncio.Event(), asyncio.Event()]
    call_count = 0

    async def blocked_connect(_job):
        nonlocal call_count
        index = call_count
        call_count += 1
        starts[index].set()
        await releases[index].wait()
        return AccountConnectionResult(
            account_id=account_id,
            platform="telegram",
            external_account_id="42",
            public_id="stale_fence_bot",
            webhook_url="https://reply.example.com/webhooks/telegram/stale_fence_bot",
            name="Bot",
            automation_default="BOT_DRAFT_ONLY",
        )

    monkeypatch.setattr(jobs, "_connect", blocked_connect)
    job_id = await jobs.submit_provisioning_job(
        tenant_id="tenant-a",
        brand_id="brand-a",
        platform="telegram",
        actor="user:admin",
        request={"idempotency_key": "stale-attempt-fence"},
        secrets={"token": "secret"},
    )
    old_worker = asyncio.create_task(jobs.process_provisioning_job(str(job_id)))
    await asyncio.wait_for(starts[0].wait(), timeout=1)
    async with get_session_factory()() as session:
        await session.execute(
            update(models.ProvisioningJob)
            .where(models.ProvisioningJob.id == job_id)
            .values(locked_at=datetime.now(UTC) - timedelta(minutes=10))
        )
        await session.commit()

    async def no_dispatch(_actor, _job_id: str, **_kwargs):
        return None

    monkeypatch.setattr(jobs, "dispatch_actor", no_dispatch)
    assert job_id in await jobs.sweep_provisioning_jobs()

    new_worker = asyncio.create_task(jobs.process_provisioning_job(str(job_id)))
    await asyncio.wait_for(starts[1].wait(), timeout=1)
    releases[0].set()
    assert await old_worker == "STALE_CLAIM"
    async with get_session_factory()() as session:
        owned = await session.get(models.ProvisioningJob, job_id)
    assert owned.status == "PROCESSING"
    assert owned.attempt_count == 2

    releases[1].set()
    assert await new_worker == "COMPLETED"
    async with get_session_factory()() as session:
        completed = await session.get(models.ProvisioningJob, job_id)
    assert completed.status == "COMPLETED"
    assert completed.attempt_count == 2
    assert completed.account_id == account_id


async def test_stale_eighth_provisioning_attempt_requires_action(migrated_db, monkeypatch):
    job_id = await jobs.submit_provisioning_job(
        tenant_id="tenant-a",
        brand_id="brand-a",
        platform="telegram",
        actor="user:admin",
        request={"idempotency_key": "stale-retry-exhaustion"},
        secrets={"token": "secret"},
    )
    async with get_session_factory()() as session:
        await session.execute(
            update(models.ProvisioningJob)
            .where(models.ProvisioningJob.id == job_id)
            .values(
                status="PROCESSING",
                attempt_count=jobs._MAX_ATTEMPTS,
                locked_at=datetime.now(UTC) - timedelta(minutes=10),
                locked_by="dead-worker",
            )
        )
        await session.commit()

    dispatched: list[str] = []

    async def dispatch(_actor, pending_id: str, **_kwargs):
        dispatched.append(pending_id)

    monkeypatch.setattr(jobs, "dispatch_actor", dispatch)

    assert job_id not in await jobs.sweep_provisioning_jobs()
    assert dispatched == []
    async with get_session_factory()() as session:
        exhausted = await session.get(models.ProvisioningJob, job_id)
    assert exhausted.status == "NEEDS_ACTION"
    assert exhausted.attempt_count == jobs._MAX_ATTEMPTS
    assert exhausted.last_error_code == "RETRY_EXHAUSTED"
    assert exhausted.next_attempt_at is None


async def test_provisioning_sweep_isolates_broker_dispatch_failures(
    migrated_db,
    monkeypatch,
):
    first = await jobs.submit_provisioning_job(
        tenant_id="tenant-a",
        brand_id="brand-a",
        platform="telegram",
        actor="user:admin",
        request={"idempotency_key": "dispatch-first"},
        secrets={"token": "secret-1"},
    )
    second = await jobs.submit_provisioning_job(
        tenant_id="tenant-a",
        brand_id="brand-a",
        platform="telegram",
        actor="user:admin",
        request={"idempotency_key": "dispatch-second"},
        secrets={"token": "secret-2"},
    )
    calls: list[uuid.UUID] = []

    async def dispatch(_actor, job_id: str, **_kwargs):
        calls.append(uuid.UUID(job_id))
        if len(calls) == 1:
            raise RuntimeError("broker unavailable")

    monkeypatch.setattr(jobs, "dispatch_actor", dispatch)

    dispatched = await jobs.sweep_provisioning_jobs()
    assert set(calls) == {first, second}
    assert len(dispatched) == 1
    assert dispatched[0] == calls[1]


async def test_same_idempotency_key_returns_same_job(migrated_db, tmp_path, monkeypatch):
    from social_reply.shared.config import get_settings

    monkeypatch.setenv("ACCOUNT_SECRETS_ROOT", str(tmp_path))
    get_settings.cache_clear()
    first = await jobs.submit_provisioning_job(
        tenant_id="tenant-a",
        brand_id="brand-a",
        platform="telegram",
        actor="user:admin",
        request={"idempotency_key": "same-request", "name": "Bot"},
        secrets={"token": "secret-1"},
    )
    second = await jobs.submit_provisioning_job(
        tenant_id="tenant-a",
        brand_id="brand-a",
        platform="telegram",
        actor="user:admin",
        request={"idempotency_key": "same-request", "name": "Bot"},
        secrets={"token": "secret-2"},
    )
    assert first == second
    async with get_session_factory()() as session:
        count = len(
            list(
                (
                    await session.execute(
                        select(models.ProvisioningJob).where(
                            models.ProvisioningJob.tenant_id == "tenant-a"
                        )
                    )
                ).scalars()
            )
        )
    assert count == 1
    get_settings.cache_clear()
