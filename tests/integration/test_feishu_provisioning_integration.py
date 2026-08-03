import asyncio
import uuid

import httpx
from sqlalchemy import func, select, text

from social_reply.application.account_management import feishu, jobs
from social_reply.application.account_management.service import AccountConnectionResult
from social_reply.connectors import registry
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_engine, get_session_factory
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


async def test_concurrent_first_feishu_provisioning_returns_one_callback_identity(
    migrated_db, monkeypatch
):
    settings = feishu.get_settings().model_copy(update={"feishu_enabled": True})
    monkeypatch.setattr(feishu, "get_settings", lambda: settings)

    def transport() -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/tenant_access_token/internal"):
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "tenant_access_token": "tenant-token",
                        "expire": 7200,
                    },
                )
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "bot": {
                        "open_id": "ou_bot_first",
                        "app_name": "First Bot",
                        "activate_status": 2,
                    },
                },
            )

        return httpx.MockTransport(handler)

    engine = get_engine()
    async with engine.begin() as connection:
        await connection.execute(text("DROP FUNCTION IF EXISTS delay_first_feishu_create()"))
        await connection.execute(
            text(
                "CREATE FUNCTION delay_first_feishu_create() RETURNS trigger "
                "LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_sleep(1); RETURN NEW; END $$"
            )
        )
        await connection.execute(
            text(
                "CREATE TRIGGER delay_first_feishu_create BEFORE INSERT ON platform_accounts "
                "FOR EACH ROW WHEN (NEW.tenant_id = 'tenant-first-race') "
                "EXECUTE FUNCTION delay_first_feishu_create()"
            )
        )

    tasks: list[asyncio.Task] = []
    try:
        tasks = [
            asyncio.create_task(
                feishu.connect_feishu_account(
                    app_id="cli_first1234",
                    app_secret=app_secret,
                    verification_token="verification-secret",
                    encrypt_key="encrypt-secret",
                    public_base_url="https://reply.example",
                    tenant_id="tenant-first-race",
                    brand_id="brand-a",
                    transport=transport(),
                )
            )
            for app_secret in ("first-secret", "second-secret")
        ]
        results = await asyncio.gather(*tasks)

        assert len({result.account_id for result in results}) == 1
        assert len({result.public_id for result in results}) == 1
        assert len({result.webhook_url for result in results}) == 1
        async with get_session_factory()() as session:
            account = (
                await session.execute(
                    select(models.PlatformAccount).where(
                        models.PlatformAccount.tenant_id == "tenant-first-race",
                        models.PlatformAccount.platform == "feishu",
                        models.PlatformAccount.external_account_id == "cli_first1234",
                    )
                )
            ).scalar_one()
        assert results[0].account_id == account.id
        assert results[0].public_id == account.public_id
        assert results[0].webhook_url.endswith(f"/webhooks/feishu/{account.public_id}")
        assert account.config_version == 2
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        async with engine.begin() as connection:
            await connection.execute(
                text("DROP TRIGGER IF EXISTS delay_first_feishu_create ON platform_accounts")
            )
            await connection.execute(text("DROP FUNCTION IF EXISTS delay_first_feishu_create()"))


async def test_concurrent_feishu_reprovisioning_advances_versions_and_rotates_sender(
    migrated_db, monkeypatch
):
    settings = feishu.get_settings().model_copy(update={"feishu_enabled": True})
    monkeypatch.setattr(feishu, "get_settings", lambda: settings)

    def transport() -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/tenant_access_token/internal"):
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "tenant_access_token": "tenant-token",
                        "expire": 7200,
                    },
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

        return httpx.MockTransport(handler)

    initial = await feishu.connect_feishu_account(
        app_id="cli_12345678",
        app_secret="secret-1",
        verification_token="verification-secret",
        encrypt_key="encrypt-secret",
        public_base_url="https://reply.example",
        tenant_id="tenant-race",
        brand_id="brand-a",
        transport=transport(),
    )

    created = []

    class FakeFeishuClient:
        def __init__(self, **kwargs):
            self.app_secret = kwargs["app_secret"]
            self.closed = False
            created.append(self)

        async def send_text(self, *, target, text):
            return f"{target}:{text}"

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr(registry, "FeishuClient", FakeFeishuClient)
    registry._senders.clear()
    first_sender = await registry.get_platform_sender(initial.account_id)
    assert first_sender.app_secret == "secret-1"

    engine = get_engine()
    async with engine.begin() as connection:
        await connection.execute(text("DROP TABLE IF EXISTS feishu_version_audit"))
        await connection.execute(text("DROP FUNCTION IF EXISTS audit_feishu_version()"))
        await connection.execute(text("DROP FUNCTION IF EXISTS delay_feishu_reprovision()"))
        await connection.execute(
            text("CREATE TABLE feishu_version_audit (config_version integer NOT NULL)")
        )
        await connection.execute(
            text(
                "CREATE FUNCTION audit_feishu_version() RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN INSERT INTO feishu_version_audit VALUES (NEW.config_version); "
                "RETURN NEW; END $$"
            )
        )
        await connection.execute(
            text(
                "CREATE TRIGGER audit_feishu_version AFTER UPDATE ON platform_accounts "
                "FOR EACH ROW WHEN (OLD.platform = 'feishu') "
                "EXECUTE FUNCTION audit_feishu_version()"
            )
        )
        await connection.execute(
            text(
                "CREATE FUNCTION delay_feishu_reprovision() RETURNS trigger "
                "LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_sleep(2); RETURN NEW; END $$"
            )
        )
        await connection.execute(
            text(
                "CREATE TRIGGER delay_feishu_reprovision BEFORE INSERT ON platform_accounts "
                "FOR EACH ROW WHEN (NEW.tenant_id = 'tenant-race') "
                "EXECUTE FUNCTION delay_feishu_reprovision()"
            )
        )

    updates: list[asyncio.Task] = []
    try:
        updates = [
            asyncio.create_task(
                feishu.connect_feishu_account(
                    app_id="cli_12345678",
                    app_secret=app_secret,
                    verification_token="verification-secret",
                    encrypt_key="encrypt-secret",
                    public_base_url="https://reply.example",
                    tenant_id="tenant-race",
                    brand_id="brand-a",
                    transport=transport(),
                )
            )
            for app_secret in ("secret-2", "secret-3")
        ]
        deadline = asyncio.get_running_loop().time() + 5
        sleeping_updates = 0
        while asyncio.get_running_loop().time() < deadline:
            async with get_session_factory()() as observer_session:
                sleeping_updates = (
                    await observer_session.execute(
                        text(
                            "SELECT count(*) FROM pg_stat_activity "
                            "WHERE datname = current_database() "
                            "AND pid <> pg_backend_pid() "
                            "AND wait_event = 'PgSleep'"
                        )
                    )
                ).scalar_one()
            if sleeping_updates == 2:
                break
            await asyncio.sleep(0.01)
        assert sleeping_updates == 2

        results = await asyncio.gather(*updates)
        assert {result.account_id for result in results} == {initial.account_id}

        async with get_session_factory()() as session:
            account = await session.get(models.PlatformAccount, initial.account_id)
            versions = (
                (
                    await session.execute(
                        text(
                            "SELECT config_version FROM feishu_version_audit "
                            "ORDER BY config_version"
                        )
                    )
                )
                .scalars()
                .all()
            )
        final_secret = decrypt_secret_bundle(account.credential_bundle)["app_secret"]
        assert versions == [2, 3]
        assert account.config_version == 3
        assert final_secret in {"secret-2", "secret-3"}

        final_sender = await registry.get_platform_sender(initial.account_id)
        assert final_sender is not first_sender
        assert final_sender.app_secret == final_secret
        assert first_sender.closed is True
        assert set(registry._senders) == {("feishu", initial.account_id, 3, 0)}
    finally:
        for update_task in updates:
            if not update_task.done():
                update_task.cancel()
        if updates:
            await asyncio.gather(*updates, return_exceptions=True)
        await registry.close_platform_senders()
        async with engine.begin() as connection:
            await connection.execute(
                text("DROP TRIGGER IF EXISTS audit_feishu_version ON platform_accounts")
            )
            await connection.execute(
                text("DROP TRIGGER IF EXISTS delay_feishu_reprovision ON platform_accounts")
            )
            await connection.execute(text("DROP FUNCTION IF EXISTS audit_feishu_version()"))
            await connection.execute(text("DROP FUNCTION IF EXISTS delay_feishu_reprovision()"))
            await connection.execute(text("DROP TABLE IF EXISTS feishu_version_audit"))
