import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select, update
from tests.integration.company_permission_support import (
    bootstrap_identity,
    bootstrap_password,
    create_staff,
)

from social_reply.application.account_management import jobs
from social_reply.application.account_management.auth import authenticate, hash_password
from social_reply.application.account_management.service import AccountConnectionResult
from social_reply.application.account_management.staff_lifecycle import revoke_staff_authority
from social_reply.application.account_management.system_user_management import (
    SystemUserActor,
    set_system_user_status,
)
from social_reply.connectors.email.imap_client import ImapClientError
from social_reply.connectors.errors import PermanentSendError
from social_reply.connectors.feishu.contracts import FEISHU_API_BASE_URL
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.secret_crypto import decrypt_secret_bundle, encrypt_secret_bundle


async def test_submit_job_stages_secret_inline_not_in_request(migrated_db, tmp_path, monkeypatch):
    from social_reply.shared.config import get_settings

    monkeypatch.setenv("ACCOUNT_SECRETS_ROOT", str(tmp_path))
    get_settings.cache_clear()
    job_id = await jobs.submit_control_provisioning_job(
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
    staged = decrypt_secret_bundle(row.staging_secret)
    assert staged["token"] == "super-secret-token"
    assert staged[jobs._CONTROL_API_SECRET_KEY] == "v1"
    # public_job 白名单输出不得暴露 staging secret
    assert "super-secret-token" not in str(jobs.public_job(row))
    get_settings.cache_clear()


@pytest.mark.parametrize("platform", ["feishu", "email"])
async def test_connect_dispatches_with_canonical_staged_secrets(
    migrated_db, monkeypatch, platform
):
    from social_reply.application.account_management import email, feishu

    settings = jobs.get_settings().model_copy(
        update={"feishu_enabled": True, "email_enabled": True}
    )
    monkeypatch.setattr(jobs, "get_settings", lambda: settings)
    if platform == "feishu":
        connector_module, connector_name = feishu, "connect_feishu_account"
        request = {
            "app_id": "cli_12345678",
            "api_base_url": FEISHU_API_BASE_URL,
            "group_mode": "mentions_only",
            "automation_default": "BOT_DRAFT_ONLY",
        }
        secrets = {
            "app_secret": "app-secret",
            "verification_token": "verification-secret",
            "encrypt_key": "encrypt-secret",
        }
    else:
        connector_module, connector_name = email, "connect_email_account"
        request = {
            "email_address": "support@example.com",
            "imap_host": "imap.example.com",
            "imap_port": 993,
            "mailbox": "INBOX",
            "smtp_host": "smtp.example.com",
            "smtp_port": 587,
            "smtp_security": "starttls",
            "from_name": "Support",
            "internal_domain_policy": "allow",
            "automation_default": "BOT_DRAFT_ONLY",
        }
        secrets = {"username": "mail-user", "password": "mail-password"}
    captured = {}

    async def capture_connect(**kwargs):
        captured.update(kwargs)
        return AccountConnectionResult(
            account_id=uuid.uuid4(),
            platform=platform,
            external_account_id=request.get("app_id", request.get("email_address")),
            public_id="dispatch_public",
            webhook_url="",
            name="Support",
            automation_default="BOT_DRAFT_ONLY",
        )

    monkeypatch.setattr(connector_module, connector_name, capture_connect)
    job_id = await jobs.submit_control_provisioning_job(
        tenant_id="tenant-a",
        brand_id="brand-a",
        platform=platform,
        actor="service:control_api",
        request={**request, "idempotency_key": uuid.uuid4().hex},
        secrets=secrets,
    )
    claimed = await jobs._claim_job(job_id)
    assert claimed is not None
    result = await jobs._connect(claimed)
    assert result.platform == platform
    for key, value in {**request, **secrets}.items():
        assert captured[key] == value
    assert captured["tenant_id"] == "tenant-a"
    assert captured["brand_id"] == "brand-a"
    assert captured["provisioning_job_id"] == claimed.id
    assert captured["provisioning_attempt_count"] == claimed.attempt_count
    assert captured["authority_kind"] == claimed.authority_kind == "CONTROL_API"
    assert captured["authority_version"] == claimed.authority_version == 1
    assert captured["trusted_control_api"] is True


async def test_new_email_job_rejects_password_only_without_retained_username(migrated_db):
    with pytest.raises(ValueError, match="^invalid_email_credentials$"):
        await jobs.submit_control_provisioning_job(
            tenant_id="tenant-a",
            brand_id="brand-a",
            platform="email",
            actor="service:control_api",
            request={"email_address": "support@example.com"},
            secrets={"password": "mail-password"},
        )
    async with get_session_factory()() as session:
        assert await session.scalar(select(models.ProvisioningJob.id)) is None


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
    job_id = await jobs.submit_control_provisioning_job(
        tenant_id="tenant-a",
        brand_id="brand-a",
        platform="telegram",
        actor="user:admin",
        request={"name": "Bot", "idempotency_key": "complete-bot"},
        secrets={"token": "secret"},
    )
    async with get_session_factory()() as session:
        before = await session.get(models.ProvisioningJob, job_id)
        staged = decrypt_secret_bundle(before.staging_secret)
        assert staged["token"] == "secret"
        assert staged[jobs._CONTROL_API_SECRET_KEY] == "v1"

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
    job_id = await jobs.submit_control_provisioning_job(
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
    job_id = await jobs.submit_control_provisioning_job(
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
    assert row.result["requires_secret_resubmission"] is True
    assert row.result["required_secret"] == "xchat_pin"
    assert jobs.PRIVATE_INPUT_FINGERPRINT_KEY in row.result
    assert jobs.PRIVATE_INPUT_FINGERPRINT_WITHOUT_REQUIRED_KEY in row.result
    assert jobs.public_job(row)["result"] == {
        "requires_secret_resubmission": True,
        "required_secret": "xchat_pin",
    }
    staged = decrypt_secret_bundle(row.staging_secret)
    assert "xchat_pin" not in staged
    assert staged["access_token"] == "at"

    with pytest.raises(ValueError, match="provisioning_secret_resubmission_required"):
        await jobs.retry_control_provisioning_job(job_id, tenant_id="tenant-a")

    with pytest.raises(ValueError, match="provisioning_secret_resubmission_required"):
        await jobs.submit_control_provisioning_job(
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

    resubmitted = await jobs.submit_control_provisioning_job(
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
    assert "requires_secret_resubmission" not in refreshed.result
    assert jobs.PRIVATE_INPUT_FINGERPRINT_KEY in refreshed.result
    assert jobs.public_job(refreshed)["result"] == {}
    assert decrypt_secret_bundle(refreshed.staging_secret)["xchat_pin"] == "5678"


async def test_stale_xchat_job_clears_pin_and_requires_resubmission(migrated_db):
    job_id = await jobs.submit_control_provisioning_job(
        tenant_id="tenant-a",
        brand_id="brand-a",
        platform="x",
        actor="service:control_api",
        request={"environment": "oauth", "idempotency_key": "stale-xchat"},
        secrets={
            "consumer_key": "ck",
            "consumer_secret": "cs",
            "access_token": "at",
            "access_token_secret": "ats",
            "xchat_pin": "1234",
        },
    )
    async with get_session_factory()() as session:
        await session.execute(
            update(models.ProvisioningJob)
            .where(models.ProvisioningJob.id == job_id)
            .values(
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
    assert row.result["requires_secret_resubmission"] is True
    assert row.result["required_secret"] == "xchat_pin"
    assert jobs.PRIVATE_INPUT_FINGERPRINT_KEY in row.result
    assert jobs.PRIVATE_INPUT_FINGERPRINT_WITHOUT_REQUIRED_KEY in row.result
    assert jobs.public_job(row)["result"] == {
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
    job_id = await jobs.submit_control_provisioning_job(
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

    await jobs.retry_control_provisioning_job(job_id, tenant_id="tenant-a")
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
    job_id = await jobs.submit_control_provisioning_job(
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
        await jobs.submit_control_provisioning_job(
            tenant_id="tenant-a",
            brand_id="brand-a",
            platform="telegram",
            actor="user:admin",
            request=request,
            secrets={"token": "secret-1"},
        )
        == job_id
    )
    async with get_session_factory()() as session:
        reset = await session.get(models.ProvisioningJob, job_id)
    assert reset.status == "PENDING"
    assert reset.attempt_count == 0
    assert reset.last_error_code is None
    staged = decrypt_secret_bundle(reset.staging_secret)
    assert staged["token"] == "secret-1"
    assert staged[jobs._CONTROL_API_SECRET_KEY] == "v1"


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
    job_id = await jobs.submit_control_provisioning_job(
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
    job_id = await jobs.submit_control_provisioning_job(
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
    first = await jobs.submit_control_provisioning_job(
        tenant_id="tenant-a",
        brand_id="brand-a",
        platform="telegram",
        actor="user:admin",
        request={"idempotency_key": "dispatch-first"},
        secrets={"token": "secret-1"},
    )
    second = await jobs.submit_control_provisioning_job(
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


async def test_same_idempotency_key_requires_same_credentials(migrated_db, tmp_path, monkeypatch):
    from social_reply.shared.config import get_settings

    monkeypatch.setenv("ACCOUNT_SECRETS_ROOT", str(tmp_path))
    get_settings.cache_clear()
    first = await jobs.submit_control_provisioning_job(
        tenant_id="tenant-a",
        brand_id="brand-a",
        platform="telegram",
        actor="user:admin",
        request={"idempotency_key": "same-request", "name": "Bot"},
        secrets={"token": "secret-1"},
    )
    with pytest.raises(ValueError, match="idempotency_key_payload_mismatch"):
        await jobs.submit_control_provisioning_job(
            tenant_id="tenant-a",
            brand_id="brand-a",
            platform="telegram",
            actor="user:admin",
            request={"idempotency_key": "same-request", "name": "Bot"},
            secrets={"token": "secret-2"},
        )
    exact_repeat = await jobs.submit_control_provisioning_job(
        tenant_id="tenant-a",
        brand_id="brand-a",
        platform="telegram",
        actor="user:admin",
        request={"idempotency_key": "same-request", "name": "Bot"},
        secrets={"token": "secret-1"},
    )
    assert exact_repeat == first
    async with get_session_factory()() as session:
        rows = list(
            (
                await session.execute(
                    select(models.ProvisioningJob).where(
                        models.ProvisioningJob.tenant_id == "tenant-a"
                    )
                )
            ).scalars()
        )
    assert len(rows) == 1
    assert jobs.PRIVATE_INPUT_FINGERPRINT_KEY in rows[0].result
    assert jobs.public_job(rows[0])["result"] == {}
    get_settings.cache_clear()


@pytest.mark.parametrize("platform", ["telegram", "x"])
async def test_applied_same_key_rejects_new_credentials_and_exact_repeat_is_read_only(
    migrated_db, platform,
):
    from social_reply.application.account_management import provisioning

    submitted_secrets = (
        {"token": "secret-1"}
        if platform == "telegram"
        else {
            "consumer_key": "consumer-key",
            "consumer_secret": "consumer-secret",
            "access_token": "access-token",
            "access_token_secret": "access-token-secret",
            "xchat_pin": "1234",
        }
    )
    applied_credentials = {
        name: value for name, value in submitted_secrets.items() if name != "xchat_pin"
    }
    changed_secrets = {
        **submitted_secrets,
        ("token" if platform == "telegram" else "access_token"): "changed-secret",
    }
    password = "applied-retry-contract-password"
    async with get_session_factory()() as session:
        user = models.AdminUser(
            username="applied-retry-contract-admin",
            password_hash=await hash_password(password),
            tenant_id="default",
            role="WORKSPACE_ADMIN",
            must_change_password=False,
            status="active",
        )
        session.add(user)
        await session.commit()
    authenticated = await authenticate("applied-retry-contract-admin", password)
    assert authenticated is not None
    principal, _token = authenticated
    account_id = uuid.uuid4()
    async with get_session_factory()() as session:
        session.add(
            models.PlatformAccount(
                id=account_id,
                tenant_id="default",
                brand_id="default",
                platform=platform,
                name="Applied reauthorization bot",
                external_account_id="telegram-applied-reauthorize",
                public_id="telegram_applied_reauthorize",
                credential_bundle=encrypt_secret_bundle({"token": "old-token"}),
                config={"delivery_mode": "direct"},
                capability={},
                config_version=1,
                status="active",
            )
        )
        await session.commit()

    job_id = await jobs.submit_provisioning_job(
        tenant_id="default",
        brand_id="default",
        platform=platform,
        actor=principal.actor,
        request={"idempotency_key": "applied-reauthorize-same-key"},
        secrets=submitted_secrets,
        operation="REAUTHORIZE",
        target_account_id=account_id,
        expected_config_version=1,
        admin_session_id=principal.session_id,
    )
    async with get_session_factory()() as session:
        job = await session.get(models.ProvisioningJob, job_id)
        account = await session.get(models.PlatformAccount, account_id)
        account.config_version = 2
        account.credential_bundle = encrypt_secret_bundle(applied_credentials)
        job.account_id = account_id
        job.status = "COMPLETED"
        job.current_step = "CREDENTIALS_APPLIED"
        job.staging_secret = None
        job.result = {
            **dict(job.result or {}),
            "bound_external_account_id": account.external_account_id,
            "bound_credential_fingerprint": provisioning.credential_fingerprint(
                applied_credentials
            ),
            "checkpoint": {
                "job_id": str(job.id),
                "operation": job.operation,
                "target_account_id": str(account_id),
                "account_id": str(account_id),
                "external_account_id": account.external_account_id,
                "public_id": account.public_id,
                "input_config_version": 1,
                "output_config_version": 2,
                "credential_fingerprint": provisioning.credential_fingerprint(
                    applied_credentials
                ),
                "phase": "CREDENTIALS_APPLIED",
            },
        }
        await session.commit()

    async with get_session_factory()() as session:
        job_before = dict((await session.execute(
            select(models.ProvisioningJob.__table__).where(models.ProvisioningJob.id == job_id)
        )).mappings().one())
        account_before = dict((await session.execute(
            select(models.PlatformAccount.__table__).where(models.PlatformAccount.id == account_id)
        )).mappings().one())
    assert job_before["staging_secret"] is None
    assert "requires_secret_resubmission" not in job_before["result"]
    assert "required_secret" not in job_before["result"]

    with pytest.raises(ValueError, match="idempotency_key_payload_mismatch"):
        await jobs.submit_provisioning_job(
            tenant_id="default",
            brand_id="default",
            platform=platform,
            actor=principal.actor,
            request={"idempotency_key": "applied-reauthorize-same-key"},
            secrets=changed_secrets,
            operation="REAUTHORIZE",
            target_account_id=account_id,
            expected_config_version=1,
            admin_session_id=principal.session_id,
        )
    exact_repeat = await jobs.submit_provisioning_job(
        tenant_id="default",
        brand_id="default",
        platform=platform,
        actor=principal.actor,
        request={"idempotency_key": "applied-reauthorize-same-key"},
        secrets=submitted_secrets,
        operation="REAUTHORIZE",
        target_account_id=account_id,
        expected_config_version=1,
        admin_session_id=principal.session_id,
    )
    assert exact_repeat == job_id
    async with get_session_factory()() as session:
        job_after = dict((await session.execute(
            select(models.ProvisioningJob.__table__).where(models.ProvisioningJob.id == job_id)
        )).mappings().one())
        account_after = dict((await session.execute(
            select(models.PlatformAccount.__table__).where(models.PlatformAccount.id == account_id)
        )).mappings().one())
    assert job_after == job_before
    assert account_after == account_before

@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        (ImapClientError("imap_authentication_failed"), "imap_authentication_failed"),
        (ImapClientError("imap_tls_invalid"), "imap_tls_invalid"),
        (PermanentSendError("smtp_tls_invalid"), "smtp_tls_invalid"),
        (ValueError("invalid_mailbox"), "INVALID_REQUEST"),
    ],
)
async def test_terminal_email_failure_clears_secret_and_requires_password_resubmission(
    migrated_db, monkeypatch, failure, expected_code
):
    settings = jobs.get_settings().model_copy(update={"email_enabled": True})
    monkeypatch.setattr(jobs, "get_settings", lambda: settings)

    async def fail_connect(_job):
        raise failure

    monkeypatch.setattr(jobs, "_connect", fail_connect)
    job_id = await jobs.submit_control_provisioning_job(
        tenant_id="tenant-a",
        brand_id="brand-a",
        platform="email",
        actor="user:admin",
        request={"email_address": "Support@example.com", "idempotency_key": uuid.uuid4().hex},
        secrets={"username": " mail-user ", "password": " mail-password "},
    )

    assert await jobs.process_provisioning_job(str(job_id)) == "NEEDS_ACTION"
    async with get_session_factory()() as session:
        row = await session.get(models.ProvisioningJob, job_id)
    assert decrypt_secret_bundle(row.staging_secret) == {
        "username": " mail-user ",
        jobs._CONTROL_API_SECRET_KEY: "v1",
    }
    assert row.last_error_code == expected_code
    assert "SECRET" not in row.last_error_message
    assert row.result["requires_secret_resubmission"] is True
    assert row.result["required_secret"] == "password"
    assert jobs.PRIVATE_INPUT_FINGERPRINT_KEY in row.result
    assert jobs.public_job(row)["result"] == {
        "requires_secret_resubmission": True,
        "required_secret": "password",
    }
    assert "mail-password" not in str(jobs.public_job(row))
    resubmitted = await jobs.submit_control_provisioning_job(
        tenant_id="tenant-a",
        brand_id="brand-a",
        platform="email",
        actor="user:admin",
        request={"email_address": "Support@example.com", "idempotency_key": row.idempotency_key},
        secrets={"password": "new-password"},
    )
    assert resubmitted == job_id
    async with get_session_factory()() as session:
        resubmitted_row = await session.get(models.ProvisioningJob, job_id)
    assert decrypt_secret_bundle(resubmitted_row.staging_secret) == {
        "username": " mail-user ",
        "password": "new-password",
        jobs._CONTROL_API_SECRET_KEY: "v1",
    }
    assert "requires_secret_resubmission" not in resubmitted_row.result
    assert jobs.public_job(resubmitted_row)["result"] == {}



async def test_retryable_email_failure_retains_secret_only_until_retry_exhaustion(
    migrated_db, monkeypatch
):
    settings = jobs.get_settings().model_copy(update={"email_enabled": True})
    monkeypatch.setattr(jobs, "get_settings", lambda: settings)

    async def fail_connect(_job):
        raise ImapClientError("imap_transport_failed", retryable=True)

    monkeypatch.setattr(jobs, "_connect", fail_connect)
    job_id = await jobs.submit_control_provisioning_job(
        tenant_id="tenant-a",
        brand_id="brand-a",
        platform="email",
        actor="user:admin",
        request={"email_address": "Support@example.com", "idempotency_key": uuid.uuid4().hex},
        secrets={"username": "mail-user", "password": "mail-password"},
    )

    assert await jobs.process_provisioning_job(str(job_id)) == "FAILED"
    async with get_session_factory()() as session:
        retryable = await session.get(models.ProvisioningJob, job_id)
        assert decrypt_secret_bundle(retryable.staging_secret)["password"] == "mail-password"
        retryable.attempt_count = jobs._MAX_ATTEMPTS - 1
        retryable.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

    assert await jobs.process_provisioning_job(str(job_id)) == "NEEDS_ACTION"
    async with get_session_factory()() as session:
        exhausted = await session.get(models.ProvisioningJob, job_id)
    assert exhausted.last_error_code == "RETRY_EXHAUSTED"
    assert decrypt_secret_bundle(exhausted.staging_secret) == {
        "username": "mail-user",
        jobs._CONTROL_API_SECRET_KEY: "v1",
    }
    assert exhausted.result["required_secret"] == "password"


async def test_disabled_email_before_claim_clears_secret_instead_of_pausing(
    migrated_db, monkeypatch
):
    settings = jobs.get_settings().model_copy(update={"email_enabled": False})
    monkeypatch.setattr(jobs, "get_settings", lambda: settings)
    job_id = await jobs.submit_control_provisioning_job(
        tenant_id="tenant-a",
        brand_id="brand-a",
        platform="email",
        actor="user:admin",
        request={"email_address": "Support@example.com", "idempotency_key": uuid.uuid4().hex},
        secrets={"username": "mail-user", "password": "mail-password"},
    )

    assert await jobs.process_provisioning_job(str(job_id)) == "NEEDS_ACTION"
    async with get_session_factory()() as session:
        row = await session.get(models.ProvisioningJob, job_id)
    assert row.status == "NEEDS_ACTION"
    assert row.last_error_code == "EMAIL_DISABLED"
    assert decrypt_secret_bundle(row.staging_secret) == {
        "username": "mail-user",
        jobs._CONTROL_API_SECRET_KEY: "v1",
    }
    assert row.result["required_secret"] == "password"
    assert jobs.PRIVATE_INPUT_FINGERPRINT_KEY in row.result
    assert jobs.public_job(row)["result"]["required_secret"] == "password"


async def test_disabled_email_race_and_stale_recovery_clear_secrets(migrated_db, monkeypatch):
    settings_ref = {"value": jobs.get_settings().model_copy(update={"email_enabled": True})}
    monkeypatch.setattr(jobs, "get_settings", lambda: settings_ref["value"])

    async def disable_during_connect(_job):
        settings_ref["value"] = settings_ref["value"].model_copy(update={"email_enabled": False})
        raise ValueError("email_integration_disabled")

    monkeypatch.setattr(jobs, "_connect", disable_during_connect)
    race_id = await jobs.submit_control_provisioning_job(
        tenant_id="tenant-a",
        brand_id="brand-a",
        platform="email",
        actor="user:admin",
        request={"email_address": "Support@example.com", "idempotency_key": uuid.uuid4().hex},
        secrets={"username": "mail-user", "password": "mail-password"},
    )
    assert await jobs.process_provisioning_job(str(race_id)) == "NEEDS_ACTION"

    stale_id = await jobs.submit_control_provisioning_job(
        tenant_id="tenant-a",
        brand_id="brand-a",
        platform="email",
        actor="service:control_api",
        request={"email_address": "Support@example.com", "idempotency_key": uuid.uuid4().hex},
        secrets={"username": "mail-user", "password": "mail-password"},
    )
    async with get_session_factory()() as session:
        await session.execute(
            update(models.ProvisioningJob)
            .where(models.ProvisioningJob.id == stale_id)
            .values(
                status="PROCESSING",
                current_step="VALIDATE_CREDENTIAL",
                locked_at=datetime.now(UTC) - timedelta(minutes=10),
                locked_by="dead-worker",
            )
        )
        await session.commit()

    await jobs.sweep_provisioning_jobs()
    async with get_session_factory()() as session:
        race = await session.get(models.ProvisioningJob, race_id)
        stale = await session.get(models.ProvisioningJob, stale_id)
    for row in (race, stale):
        assert row.status == "NEEDS_ACTION"
        staged = decrypt_secret_bundle(row.staging_secret)
        assert staged["username"] == "mail-user"
        assert staged[jobs._CONTROL_API_SECRET_KEY] == "v1"
        assert row.result["requires_secret_resubmission"] is True
        assert row.result["required_secret"] == "password"


async def test_control_wrapper_records_authority_and_legacy_job_is_quarantined(migrated_db):
    job_id = await jobs.submit_control_provisioning_job(
        tenant_id="tenant-a",
        brand_id="brand-a",
        platform="telegram",
        actor="service:control_api",
        request={"idempotency_key": "control-authority-contract", "name": "Bot"},
        secrets={"token": "secret"},
    )
    async with get_session_factory()() as session:
        job = await session.get(models.ProvisioningJob, job_id)
        assert job.authority_kind == "CONTROL_API"
        assert job.authority_version == 1
        assert decrypt_secret_bundle(job.staging_secret)[jobs._CONTROL_API_SECRET_KEY] == "v1"
        legacy_id = uuid.uuid4()
        session.add(
            models.ProvisioningJob(
                id=legacy_id,
                tenant_id="tenant-a",
                brand_id="brand-a",
                platform="telegram",
                operation="CONNECT_ACCOUNT",
                actor="legacy",
                authority_kind="UNVERIFIED",
                authority_version=0,
                idempotency_key="legacy-authority-contract",
                request={},
                staging_secret=None,
                status="PENDING",
                current_step="QUEUED",
            )
        )
        await session.commit()

    assert await jobs.process_provisioning_job(str(legacy_id)) == "SKIPPED_NOT_CLAIMABLE"
    async with get_session_factory()() as session:
        legacy = await session.get(models.ProvisioningJob, legacy_id)
        assert legacy.status == "NEEDS_ACTION"
        assert legacy.last_error_code == "PROVISIONING_AUTHORITY_INVALID"


async def test_same_control_job_checkpoint_recovers_without_overwriting_disabled_account(
    migrated_db,
):
    from social_reply.application.account_management import provisioning

    account_id = uuid.uuid4()
    job_id = uuid.uuid4()
    other_job_id = uuid.uuid4()
    marker = {jobs._CONTROL_API_SECRET_KEY: "v1"}
    new_credential_fingerprint = provisioning.credential_fingerprint({"access_token": "new"})
    other_credential_fingerprint = provisioning.credential_fingerprint({"access_token": "other"})
    async with get_session_factory()() as session:
        session.add(
            models.PlatformAccount(
                id=account_id,
                tenant_id="tenant-a",
                brand_id="admin-brand",
                platform="x",
                name="Admin configured",
                external_account_id="x-42",
                public_id="x_admin",
                credential_bundle=encrypt_secret_bundle({"access_token": "old"}),
                config={"operator_choice": "keep", "delivery_mode": "direct"},
                capability={"dm": True},
                automation_default="BOT_ACTIVE",
                status="DISABLED",
                config_version=1,
            )
        )
        session.add(
            models.ProvisioningJob(
                id=job_id,
                tenant_id="tenant-a",
                brand_id="admin-brand",
                platform="x",
                operation="CONNECT_ACCOUNT",
                actor="service:control_api",
                authority_kind="CONTROL_API",
                authority_version=1,
                idempotency_key="checkpoint-control-job",
                request={"external_account_id": "x-42"},
                staging_secret=encrypt_secret_bundle(marker),
                status="PROCESSING",
                current_step="SUBSCRIBE",
                attempt_count=1,
                account_id=account_id,
                expected_config_version=1,
                result={
                    "bound_external_account_id": "x-42",
                    "bound_credential_fingerprint": new_credential_fingerprint,
                    "bound_platform_app_id": None,
                    "checkpoint": {
                        "job_id": str(job_id),
                        "operation": "CONNECT_ACCOUNT",
                        "target_account_id": None,
                        "account_id": str(account_id),
                        "external_account_id": "x-42",
                        "public_id": "x_admin",
                        "input_config_version": None,
                        "output_config_version": 1,
                        "credential_fingerprint": new_credential_fingerprint,
                        "phase": "ACCOUNT_PERSISTED",
                    },
                },
            )
        )
        session.add(
            models.ProvisioningJob(
                id=other_job_id,
                tenant_id="tenant-a",
                brand_id="brand-b",
                platform="x",
                operation="CONNECT_ACCOUNT",
                actor="service:control_api",
                authority_kind="CONTROL_API",
                authority_version=1,
                idempotency_key="different-control-job",
                request={"external_account_id": "x-42"},
                staging_secret=encrypt_secret_bundle(marker),
                status="PROCESSING",
                result={
                    "bound_external_account_id": "x-42",
                    "bound_credential_fingerprint": other_credential_fingerprint,
                    "bound_platform_app_id": None,
                },
                current_step="VALIDATE_CREDENTIAL",
                attempt_count=1,
            )
        )
        await session.commit()

    recovered_id, recovered_public_id = await provisioning.provision_direct_account(
        platform="x",
        external_account_id="x-42",
        tenant_id="tenant-a",
        brand_id="admin-brand",
        name="ignored",
        public_id="x_admin",
        public_id_prefix="x",
        secrets_root=Path("."),
        credential_bundle={"access_token": "new"},
        webhook_secret_bundle=None,
        config={"must-not": "replace"},
        capability={"dm": True},
        automation_default="BOT_DRAFT_ONLY",
        authority_kind="CONTROL_API",
        authority_version=1,
        trusted_control_api=True,
        operation="CONNECT_ACCOUNT",
        provisioning_job_id=job_id,
        provisioning_attempt_count=1,
    )
    assert (recovered_id, recovered_public_id) == (account_id, "x_admin")

    with pytest.raises(ValueError, match="platform_account_already_exists"):
        await provisioning.provision_direct_account(
            platform="x",
            external_account_id="x-42",
            tenant_id="tenant-a",
            brand_id="brand-b",
            name="other",
            public_id="x_admin",
            public_id_prefix="x",
            secrets_root=Path("."),
            credential_bundle={"access_token": "other"},
            webhook_secret_bundle=None,
            config={},
            capability={"dm": True},
            automation_default="BOT_DRAFT_ONLY",
            authority_kind="CONTROL_API",
            authority_version=1,
            trusted_control_api=True,
            operation="CONNECT_ACCOUNT",
            provisioning_job_id=other_job_id,
            provisioning_attempt_count=1,
        )
    async with get_session_factory()() as session:
        account = await session.get(models.PlatformAccount, account_id)
        assert account.status == "DISABLED"
        assert account.brand_id == "admin-brand"
        assert account.config == {"operator_choice": "keep", "delivery_mode": "direct"}
        assert account.config_version == 1


@pytest.mark.parametrize(
    ("sweep_status", "authority_action"),
    [
        ("PROCESSING", "revoke"),
        ("PROCESSING", "disable"),
        ("PAUSED_PLATFORM_DISABLED", "disable"),
    ],
)
async def test_sweep_snapshot_race_with_real_staff_authority_change(
    migrated_db,
    session,
    monkeypatch,
    sweep_status,
    authority_action,
):
    owner = await create_staff(
        session, username=f"provisioning-sweep-{uuid.uuid4().hex}", role="OPERATOR"
    )
    bootstrap_principal = None
    if authority_action == "disable":
        bootstrap_principal, _bootstrap_token = await bootstrap_identity()

    job_id = await jobs.submit_provisioning_job(
        tenant_id="default",
        brand_id="default",
        platform="facebook",
        actor=owner.principal.actor,
        request={
            "external_account_id": f"sweep-page-{uuid.uuid4().hex}",
            "automation_default": "BOT_DRAFT_ONLY",
            "idempotency_key": f"sweep-race-{uuid.uuid4().hex}",
        },
        secrets={
            "access_token": "sweep-access-token",
            "app_secret": "sweep-app-secret",
            "verify_token": "sweep-verify-token",
        },
        admin_session_id=owner.principal.session_id,
    )
    settings = jobs.get_settings().model_copy(
        update={"facebook_messenger_enabled": sweep_status != "PAUSED_PLATFORM_DISABLED"}
    )
    monkeypatch.setattr(jobs, "get_settings", lambda: settings)
    async with get_session_factory()() as job_session:
        await job_session.execute(
            update(models.ProvisioningJob)
            .where(models.ProvisioningJob.id == job_id)
            .values(
                status=sweep_status,
                current_step=(
                    "VALIDATE_CREDENTIAL"
                    if sweep_status == "PROCESSING"
                    else "PAUSED_PLATFORM_DISABLED"
                ),
                attempt_count=1 if sweep_status == "PROCESSING" else 0,
                locked_at=(
                    datetime.now(UTC) - timedelta(minutes=10)
                    if sweep_status == "PROCESSING"
                    else None
                ),
                locked_by="dead-worker" if sweep_status == "PROCESSING" else None,
            )
        )
        await job_session.commit()

    original_lock = jobs._lock_staff_and_sessions
    sweep_lock_started = asyncio.Event()
    change_committed = asyncio.Event()
    release_sweep_lock = asyncio.Event()

    async def gated_lock(lock_session, *, staff_ids, session_ids):
        sweep_lock_started.set()
        await asyncio.wait_for(release_sweep_lock.wait(), timeout=5)
        return await original_lock(
            lock_session,
            staff_ids=staff_ids,
            session_ids=session_ids,
        )

    monkeypatch.setattr(jobs, "_lock_staff_and_sessions", gated_lock)
    sweep_task = asyncio.create_task(jobs.sweep_provisioning_jobs())
    await asyncio.wait_for(sweep_lock_started.wait(), timeout=5)

    async def change_authority():
        if authority_action == "revoke":
            async with get_session_factory()() as revoke_session:
                result = await revoke_staff_authority(
                    revoke_session,
                    user_id=owner.user_id,
                    reason="provisioning sweep race",
                )
                await revoke_session.commit()
                change_committed.set()
                return result
        assert bootstrap_principal is not None
        await set_system_user_status(
            user_id=owner.user_id,
            user_status="disabled",
            bootstrap_password=bootstrap_password(),
            actor=SystemUserActor(
                actor=bootstrap_principal.actor,
                session_id=bootstrap_principal.session_id,
                user_id=None,
            ),
        )
        change_committed.set()
        return None

    change_task = asyncio.create_task(change_authority())
    try:
        # Starting the coroutine does not prove revocation won the authority lock.
        await asyncio.wait_for(change_committed.wait(), timeout=5)
        release_sweep_lock.set()
        _change_result, sweep_result = await asyncio.wait_for(
            asyncio.gather(change_task, sweep_task), timeout=5
        )
    finally:
        release_sweep_lock.set()
        if not sweep_task.done() or not change_task.done():
            await asyncio.wait_for(
                asyncio.gather(sweep_task, change_task, return_exceptions=True), timeout=5
            )

    assert sweep_result == []
    async with get_session_factory()() as check_session:
        row = await check_session.get(models.ProvisioningJob, job_id)
    assert row is not None
    if sweep_status == "PROCESSING":
        assert row.status == "CANCELLED"
        assert row.current_step == "REVOKED"
        assert row.last_error_code == "STAFF_AUTHORITY_REVOKED"
    else:
        assert row.status == "NEEDS_ACTION"
        assert row.last_error_code == "PROVISIONING_AUTHORITY_INVALID"
    assert row.locked_at is None


async def test_staff_retry_race_commits_once_then_worker_cannot_revert(
    migrated_db,
    session,
    monkeypatch,
):
    owner = await create_staff(
        session, username=f"provisioning-retry-{uuid.uuid4().hex}", role="OPERATOR"
    )

    async def fail_connect(_job):
        request = httpx.Request("GET", "https://api.telegram.org/getMe")
        raise httpx.ConnectError("retry race failure", request=request)

    monkeypatch.setattr(jobs, "_connect", fail_connect)
    job_id = await jobs.submit_provisioning_job(
        tenant_id="default",
        brand_id="default",
        platform="telegram",
        actor=owner.principal.actor,
        request={"idempotency_key": f"retry-race-{uuid.uuid4().hex}"},
        secrets={"token": "retry-token"},
        admin_session_id=owner.principal.session_id,
    )
    assert await jobs.process_provisioning_job(str(job_id)) == "FAILED"

    original_lock = jobs._lock_staff_and_sessions
    both_discovered = asyncio.Event()
    release_retry_locks = asyncio.Event()
    lock_waiters = 0

    async def gated_retry_lock(lock_session, *, staff_ids, session_ids):
        nonlocal lock_waiters
        lock_waiters += 1
        if lock_waiters == 2:
            both_discovered.set()
        await asyncio.wait_for(release_retry_locks.wait(), timeout=5)
        return await original_lock(
            lock_session,
            staff_ids=staff_ids,
            session_ids=session_ids,
        )

    monkeypatch.setattr(jobs, "_lock_staff_and_sessions", gated_retry_lock)

    async def retry_once():
        try:
            await jobs.retry_provisioning_job(
                job_id,
                tenant_id="default",
                caller=owner.principal,
            )
        except Exception as exc:  # noqa: BLE001 - assert the losing race result below
            return exc
        return None

    first_retry = asyncio.create_task(retry_once())
    second_retry = asyncio.create_task(retry_once())
    await asyncio.wait_for(both_discovered.wait(), timeout=5)
    release_retry_locks.set()
    retry_results = await asyncio.wait_for(
        asyncio.gather(first_retry, second_retry), timeout=5
    )

    assert sum(result is None for result in retry_results) == 1
    retry_errors = [result for result in retry_results if isinstance(result, Exception)]
    assert len(retry_errors) == 1
    assert isinstance(retry_errors[0], ValueError)
    assert str(retry_errors[0]) == "provisioning_job_not_retryable"
    async with get_session_factory()() as check_session:
        retry_audits = list(
            (
                await check_session.scalars(
                    select(models.AuditLog).where(
                        models.AuditLog.subject_id == str(job_id),
                        models.AuditLog.action == "RETRY_PROVISIONING_JOB",
                    )
                )
            ).all()
        )
    assert len(retry_audits) == 1

    claimed = await jobs._claim_job(job_id)
    assert claimed is not None
    assert claimed.status == "PROCESSING"
    assert claimed.attempt_count == 2
    async with get_session_factory()() as check_session:
        claimed_row = await check_session.get(models.ProvisioningJob, job_id)
        retry_audits = list(
            (
                await check_session.scalars(
                    select(models.AuditLog).where(
                        models.AuditLog.subject_id == str(job_id),
                        models.AuditLog.action == "RETRY_PROVISIONING_JOB",
                    )
                )
            ).all()
        )
    assert claimed_row is not None
    assert claimed_row.status == "PROCESSING"
    assert claimed_row.attempt_count == 2
    assert len(retry_audits) == 1
