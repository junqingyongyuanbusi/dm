import uuid

from sqlalchemy import func, select

from social_reply.application.account_management import feishu, jobs
from social_reply.application.account_management.service import AccountConnectionResult
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.secret_crypto import decrypt_secret_bundle


async def test_feishu_job_stages_secrets_and_clears_them_on_completion(migrated_db, monkeypatch):
    settings = jobs.get_settings().model_copy(update={"feishu_enabled": True})
    monkeypatch.setattr(jobs, "get_settings", lambda: settings)
    captured = {}
    account_id = uuid.uuid4()
    async with get_session_factory()() as session:
        session.add(
            models.PlatformAccount(
                id=account_id,
                tenant_id="tenant-a",
                brand_id="brand-a",
                platform="feishu",
                name="Support Bot",
                external_account_id="cli_12345678",
                public_id="fs_public",
                credential_bundle={},
                config={"delivery_mode": "direct"},
                capability={"dm": True, "mentions": True, "max_text_length": 4000},
                automation_default="BOT_DRAFT_ONLY",
                status="active",
            )
        )
        await session.commit()

    async def fake_connect(job):
        captured["request"] = dict(job.request or {})
        captured["secrets"] = decrypt_secret_bundle(job.staging_secret)
        return AccountConnectionResult(
            account_id=account_id,
            platform="feishu",
            external_account_id="cli_12345678",
            public_id="fs_public",
            webhook_url="https://reply.example/webhooks/feishu/fs_public",
            name="Support Bot",
            automation_default="BOT_DRAFT_ONLY",
            bot_name="Support Bot",
            bot_status=2,
            manual_steps=("Subscribe to im.message.receive_v1.",),
        )

    monkeypatch.setattr(jobs, "_connect", fake_connect)
    job_id = await jobs.submit_provisioning_job(
        tenant_id="tenant-a",
        brand_id="brand-a",
        platform="feishu",
        actor="user:tenant-admin",
        request={
            "app_id": "cli_12345678",
            "api_base_url": "https://open.feishu.cn",
            "group_mode": "mentions_only",
            "automation_default": "BOT_DRAFT_ONLY",
            "app_secret": "must-not-persist-in-request",
            "idempotency_key": "feishu-job-staging",
        },
        secrets={
            "app_secret": "app-secret",
            "verification_token": "verification-secret",
            "encrypt_key": "encrypt-secret",
        },
    )
    async with get_session_factory()() as session:
        staged = await session.get(models.ProvisioningJob, job_id)
    assert staged.request == {
        "app_id": "cli_12345678",
        "api_base_url": "https://open.feishu.cn",
        "group_mode": "mentions_only",
        "automation_default": "BOT_DRAFT_ONLY",
    }
    assert decrypt_secret_bundle(staged.staging_secret) == {
        "app_secret": "app-secret",
        "verification_token": "verification-secret",
        "encrypt_key": "encrypt-secret",
    }

    assert await jobs.process_provisioning_job(str(job_id)) == "COMPLETED"
    async with get_session_factory()() as session:
        completed = await session.get(models.ProvisioningJob, job_id)
    assert completed.staging_secret is None
    assert completed.result["callback_url"].endswith("/webhooks/feishu/fs_public")
    assert completed.result["bot_name"] == "Support Bot"
    assert completed.result["bot_status"] == 2
    assert "secret" not in str(jobs.public_job(completed)).lower()
    assert captured["request"] == staged.request
    assert captured["secrets"]["encrypt_key"] == "encrypt-secret"


async def test_connect_feishu_persists_direct_account_contract(migrated_db, monkeypatch):
    settings = feishu.get_settings().model_copy(update={"feishu_enabled": True})
    monkeypatch.setattr(feishu, "get_settings", lambda: settings)

    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "tenant-token", "expire": 7200},
            )
        return httpx.Response(
            200,
            json={
                "code": 0,
                "bot": {
                    "open_id": "ou_bot_1",
                    "app_name": "Support Bot",
                    "activate_status": 2,
                },
            },
        )

    result = await feishu.connect_feishu_account(
        app_id="cli_12345678",
        app_secret="app-secret",
        verification_token="verification-secret",
        encrypt_key="encrypt-secret",
        public_base_url="https://reply.example",
        tenant_id="tenant-a",
        brand_id="brand-a",
        transport=httpx.MockTransport(handler),
    )

    async with get_session_factory()() as session:
        account = await session.get(models.PlatformAccount, result.account_id)
        platform_app_count = (
            await session.execute(select(func.count()).select_from(models.PlatformApp))
        ).scalar_one()
    assert account.platform == "feishu"
    assert account.external_account_id == "cli_12345678"
    assert account.public_id.startswith("fs_")
    assert account.platform_app_id is None
    assert account.status == "active"
    assert account.automation_default == "BOT_DRAFT_ONLY"
    assert account.capability == {
        "dm": True,
        "mentions": True,
        "max_text_length": 4000,
    }
    assert account.config["delivery_mode"] == "direct"
    assert account.config["api_base_url"] == "https://open.feishu.cn"
    assert account.config["feishu_bot_open_id"] == "ou_bot_1"
    assert account.config["feishu_bot_activate_status"] == 2
    assert account.config["feishu_health_status"] == "READY"
    assert account.config["feishu_health_checked_at"]
    assert decrypt_secret_bundle(account.credential_bundle) == {
        "app_id": "cli_12345678",
        "app_secret": "app-secret",
    }
    assert decrypt_secret_bundle(account.webhook_secret_bundle) == {
        "verification_token": "verification-secret",
        "encrypt_key": "encrypt-secret",
    }
    assert platform_app_count == 0
