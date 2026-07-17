import uuid

from sqlalchemy import select

from social_reply.application.account_management import jobs
from social_reply.application.account_management.service import AccountConnectionResult
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory


async def test_submit_job_stages_secret_inline_not_in_request(
    migrated_db, tmp_path, monkeypatch
):
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
    # secret 内联暂存进 staging_secret 列，供 worker 跨容器读取
    assert row.staging_secret == {"token": "super-secret-token"}
    # public_job 白名单输出不得暴露 staging secret
    assert "super-secret-token" not in str(jobs.public_job(row))
    get_settings.cache_clear()


async def test_process_job_completes_and_deletes_staging_secret(
    migrated_db, tmp_path, monkeypatch
):
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
                credential_bundle={"bot_token": "not-read"},
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
        assert before.staging_secret == {"token": "secret"}

    assert await jobs.process_provisioning_job(str(job_id)) == "COMPLETED"
    async with get_session_factory()() as session:
        row = await session.get(models.ProvisioningJob, job_id)
    assert row.status == "COMPLETED"
    assert row.account_id == account_id
    assert row.result["webhook_url"].endswith("/tg_public")
    # 完成后内联 staging secret 被清除
    assert row.staging_secret is None
    get_settings.cache_clear()


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
