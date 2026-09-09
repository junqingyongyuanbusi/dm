import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
import redis.asyncio as aioredis
from sqlalchemy import select

from apps.api.main import create_app
from social_reply.application.account_management import jobs
from social_reply.application.account_management.auth import authenticate, hash_password
from social_reply.application.account_management.meta_credentials import (
    MetaAppCredentials,
)
from social_reply.application.account_management.ui_i18n import LOCALE_COOKIE_NAME
from social_reply.infrastructure.database import models
from social_reply.infrastructure.secret_crypto import encrypt_secret_bundle
from social_reply.shared.config import get_settings

pytestmark = pytest.mark.integration

_USER_PASSWORD = "channel-user-password-123"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="https://test",
        follow_redirects=False,
    )


async def _seed_users(
    session, *, first_role: str = "OPERATOR"
) -> tuple[models.AdminUser, models.AdminUser]:
    first_user = models.AdminUser(
        username="channel-user-a",
        password_hash=await hash_password(_USER_PASSWORD),
        tenant_id="default",
        role=first_role,
        must_change_password=False,
        status="active",
    )
    second_user = models.AdminUser(
        username="channel-user-b",
        password_hash=await hash_password(_USER_PASSWORD),
        tenant_id="default",
        role="OPERATOR",
        must_change_password=False,
        status="active",
    )
    session.add_all([first_user, second_user])
    await session.commit()
    return first_user, second_user


async def _login(client: httpx.AsyncClient, username: str) -> str:
    await client.get("/auth/login")
    csrf = client.cookies["reply_admin_csrf"]
    response = await client.post(
        "/auth/login",
        data={
            "csrf_token": csrf,
            "username": username,
            "password": _USER_PASSWORD,
        },
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/app"
    return csrf


async def _login_superadmin(client: httpx.AsyncClient) -> str:
    await client.get("/auth/login")
    csrf = client.cookies["reply_admin_csrf"]
    response = await client.post(
        "/auth/login",
        data={
            "csrf_token": csrf,
            "username": "admin",
            "password": "test-admin-password",
        },
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/system/overview"
    return csrf


async def test_channels_page_profile_and_job_endpoint_are_owner_scoped(
    session,
    migrated_db,
) -> None:
    first_user, second_user = await _seed_users(session)
    own_account = models.PlatformAccount(
        tenant_id="default",
        brand_id="default",
        platform="x",
        owner_user_id=first_user.id,
        name="Owned X Account",
        provider_username="owned_handle",
        avatar_url="https://pbs.twimg.com/profile_images/owned.jpg",
        external_account_id="x-owned",
        public_id="x_owned",
        config={},
        capability={},
        automation_default="BOT_DRAFT_ONLY",
        status="active",
    )
    sibling_account = models.PlatformAccount(
        tenant_id="default",
        brand_id="default",
        platform="telegram",
        owner_user_id=second_user.id,
        name="Sibling Secret Account",
        provider_username="sibling_bot",
        external_account_id="telegram-sibling",
        public_id="tg_sibling",
        config={},
        capability={},
        automation_default="BOT_DRAFT_ONLY",
        status="active",
    )
    session.add_all([own_account, sibling_account])
    await session.flush()
    own_job = models.ProvisioningJob(
        tenant_id="default",
        brand_id="default",
        platform="x",
        actor="user:channel-user-a",
        owner_user_id=first_user.id,
        idempotency_key="owned-channel-job",
        request={"name": "Owned X Account"},
        staging_secret=encrypt_secret_bundle({"access_token": "owned-secret-token"}),
        status="NEEDS_ACTION",
        current_step="NEEDS_ACTION",
        result={"nested": {"access_token": "result-secret-token"}},
        last_error_code="ACCOUNT_OWNER_CONFLICT",
        last_error_message="raw provider details should never render",
    )
    sibling_job = models.ProvisioningJob(
        tenant_id="default",
        brand_id="default",
        platform="telegram",
        actor="user:channel-user-b",
        owner_user_id=second_user.id,
        idempotency_key="sibling-channel-job",
        request={"name": "Sibling Secret Account"},
        status="PENDING",
        current_step="QUEUED",
        result={},
    )
    session.add_all([own_job, sibling_job])
    await session.commit()

    async with _client() as client:
        await _login(client, first_user.username)
        channels_page = await client.get("/app/t/default/channels")
        profile_page = await client.get("/app/t/default/profile")
        own_job_response = await client.get(f"/app/t/default/channels/jobs/{own_job.id}")
        sibling_job_response = await client.get(f"/app/t/default/channels/jobs/{sibling_job.id}")
        client.cookies.set(LOCALE_COOKIE_NAME, "en")
        english_channels_page = await client.get("/app/t/default/channels")

    assert channels_page.status_code == 200
    assert channels_page.headers["cache-control"] == "no-store"
    assert "Owned X Account" in channels_page.text
    assert "@owned_handle" in channels_page.text
    assert "https://pbs.twimg.com/profile_images/owned.jpg" in channels_page.text
    assert 'referrerpolicy="no-referrer"' in channels_page.text
    assert "Sibling Secret Account" not in channels_page.text
    assert "sibling_bot" not in channels_page.text
    assert "owned-secret-token" not in channels_page.text
    assert "raw provider details" not in channels_page.text
    assert 'href="/admin' not in channels_page.text
    assert 'action="/admin' not in channels_page.text
    assert 'data-pending-label="正在连接…"' in channels_page.text
    assert "owned-secret-token" not in channels_page.text

    assert english_channels_page.status_code == 200
    assert '<html lang="en">' in english_channels_page.text
    assert 'data-pending-label="Connecting…"' in english_channels_page.text
    assert "Connected accounts" in english_channels_page.text
    assert "Add channels" in english_channels_page.text
    assert "授权进度" not in english_channels_page.text
    assert "owned-secret-token" not in english_channels_page.text
    assert "raw provider details" not in english_channels_page.text
    assert "Sibling Secret Account" not in english_channels_page.text
    assert 'href="/admin' not in english_channels_page.text
    assert 'action="/admin' not in english_channels_page.text

    assert profile_page.status_code == 200
    assert "/app/t/default/channels" in profile_page.text
    assert "/auth/change-password" in profile_page.text
    assert "Bot Token" not in profile_page.text
    assert "App Password" not in profile_page.text
    assert "/channels/accounts/" not in profile_page.text

    assert own_job_response.status_code == 200
    assert own_job_response.headers["cache-control"] == "no-store"
    assert own_job_response.json()["last_error_code"] == "ACCOUNT_OWNER_CONFLICT"
    assert "其他归属范围" in own_job_response.json()["last_error_message"]
    assert "secret-token" not in own_job_response.text
    assert sibling_job_response.status_code == 404


async def test_superadmin_channels_page_is_tenant_wide_without_legacy_admin_links(
    session,
    migrated_db,
) -> None:
    first_user, _second_user = await _seed_users(session)
    user_account = models.PlatformAccount(
        tenant_id="default",
        brand_id="default",
        platform="telegram",
        owner_user_id=first_user.id,
        name="User-owned Telegram",
        external_account_id="telegram-owned",
        public_id="tg_owned",
        config={},
        capability={},
        automation_default="BOT_DRAFT_ONLY",
        status="active",
    )
    organization_account = models.PlatformAccount(
        tenant_id="default",
        brand_id="default",
        platform="feishu",
        owner_user_id=None,
        name="Tenant Feishu",
        external_account_id="feishu-tenant",
        public_id="fs_tenant",
        config={"feishu_health_status": "READY"},
        capability={},
        automation_default="BOT_DRAFT_ONLY",
        status="active",
    )
    session.add_all([user_account, organization_account])
    await session.flush()
    session.add_all(
        [
            models.ProvisioningJob(
                tenant_id="default",
                brand_id="default",
                platform="telegram",
                actor=first_user.username,
                owner_user_id=first_user.id,
                idempotency_key="admin-visible-user-job",
                request={"name": "User-owned Telegram"},
                status="PENDING",
                current_step="QUEUED",
                result={},
            ),
            models.ProvisioningJob(
                tenant_id="default",
                brand_id="default",
                platform="feishu",
                actor="user:admin",
                owner_user_id=None,
                idempotency_key="admin-visible-tenant-job",
                request={"name": "Tenant Feishu"},
                status="FAILED",
                current_step="FAILED",
                result={},
                last_error_code="PLATFORM_HTTP_503",
                last_error_message="raw provider response must stay private",
            ),
        ]
    )
    await session.commit()

    async with _client() as client:
        await _login_superadmin(client)
        response = await client.get("/app/t/default/channels")

    assert response.status_code == 200
    assert "User-owned Telegram" in response.text
    assert "Tenant Feishu" in response.text
    assert first_user.username in response.text
    assert "raw provider response" not in response.text
    assert 'href="/admin/integrations/accounts"' not in response.text
    assert 'href="/admin/accounts"' not in response.text
    assert 'href="/admin/feishu-handoff"' not in response.text
    assert 'action="/admin' not in response.text
    assert "/app/t/default/channels/feishu/handoff" in response.text
    assert "/app/t/default/channels/jobs/" in response.text
    assert "/retry" in response.text
    for provider in ("Telegram", "Facebook", "Instagram", "WhatsApp", "X", "Feishu", "Email"):
        assert provider in response.text


async def test_user_cannot_provision_admin_managed_channels(
    session,
    migrated_db,
    monkeypatch,
) -> None:
    from social_reply.application.account_management import channel_management

    # Exercise authorization rather than Feishu's disabled-integration gate.
    monkeypatch.setattr(get_settings(), "feishu_enabled", True)
    submit_job = AsyncMock(side_effect=AssertionError("unauthorized_provisioning_submission"))
    monkeypatch.setattr(channel_management, "submit_provisioning_job", submit_job)
    user, _sibling_user = await _seed_users(session)

    async with _client() as client:
        csrf = await _login(client, user.username)
        whatsapp_response = await client.post(
            "/app/t/default/channels/accounts/whatsapp",
            data={
                "csrf_token": csrf,
                "external_account_id": "policy-whatsapp",
                "app_id": "policy-app",
                "access_token": "policy-secret-access-token",
                "app_secret": "policy-secret-app-secret",
                "verify_token": "policy-secret-verify-token",
            },
        )
        feishu_response = await client.post(
            "/app/t/default/channels/accounts/feishu",
            data={
                "csrf_token": csrf,
                "app_id": "cli_policytest",
                "app_secret": "policy-secret-app-secret",
                "verification_token": "policy-secret-verification-token",
                "encrypt_key": "policy-secret-encrypt-key",
            },
        )
        telegram_active_response = await client.post(
            "/app/t/default/channels/accounts/telegram",
            data={
                "csrf_token": csrf,
                "automation_default": "BOT_ACTIVE",
                "token": "policy-secret-telegram-token",
            },
        )
        x_active_response = await client.post(
            "/app/t/default/channels/accounts/x",
            data={
                "csrf_token": csrf,
                "automation_default": "BOT_ACTIVE",
                "consumer_key": "policy-secret-consumer-key",
                "consumer_secret": "policy-secret-consumer-secret",
                "access_token": "policy-secret-access-token",
                "access_token_secret": "policy-secret-access-token-secret",
            },
        )

    for response in (
        whatsapp_response,
        feishu_response,
        telegram_active_response,
        x_active_response,
    ):
        assert response.status_code == 403, response.request.url.path
        assert response.json() == {"detail": "tenant_admin_required"}
        assert "policy-secret" not in response.text
    submit_job.assert_not_awaited()
    assert (
        await session.scalar(
            select(models.ProvisioningJob.id).where(
                models.ProvisioningJob.owner_user_id == user.id,
                models.ProvisioningJob.platform.in_(("whatsapp", "feishu", "telegram", "x")),
            )
        )
        is None
    )


async def test_historical_user_job_is_blocked_on_retry_and_worker_execution(
    session,
    migrated_db,
) -> None:
    from social_reply.application.account_management import jobs

    user, _sibling_user = await _seed_users(session)
    authenticated = await authenticate(user.username, _USER_PASSWORD)
    assert authenticated is not None
    principal, _raw_token = authenticated
    job_id = await jobs.submit_provisioning_job(
        tenant_id="default",
        brand_id="default",
        platform="telegram",
        actor=principal.actor,
        admin_session_id=principal.session_id,
        request={"idempotency_key": "historical-user-active-job"},
        secrets={"token": "historical-secret"},
    )
    historical_job = await session.get(models.ProvisioningJob, job_id)
    assert historical_job is not None
    # Preserve valid provenance while recreating the retired unsafe policy.
    historical_job.request = {"automation_default": "BOT_ACTIVE"}
    historical_job.status = "FAILED"
    historical_job.current_step = "FAILED"
    historical_job.last_error_code = "PLATFORM_UNAVAILABLE"
    historical_job.last_error_message = "temporary"
    await session.commit()

    async with _client() as client:
        csrf = await _login(client, user.username)
        retry_response = await client.post(
            f"/app/t/default/channels/jobs/{historical_job.id}/retry",
            data={"csrf_token": csrf},
        )

    assert retry_response.status_code == 403
    assert retry_response.json() == {"detail": "tenant_admin_required"}
    worker_result = await jobs.process_provisioning_job(str(historical_job.id))
    assert worker_result == "NEEDS_ACTION"
    await session.refresh(historical_job)
    assert historical_job.status == "NEEDS_ACTION"
    assert historical_job.last_error_code == "INVALID_REQUEST"
    assert historical_job.account_id is None


async def test_channel_lifecycle_commands_are_scoped_optimistic_and_audited(
    session,
    migrated_db,
) -> None:
    first_user, second_user = await _seed_users(session)
    account = models.PlatformAccount(
        tenant_id="default",
        brand_id="default",
        platform="telegram",
        owner_user_id=first_user.id,
        name="Lifecycle account",
        external_account_id="telegram-lifecycle",
        public_id="tg_lifecycle",
        config={},
        capability={},
        automation_default="BOT_DRAFT_ONLY",
        status="active",
    )
    sibling_account = models.PlatformAccount(
        tenant_id="default",
        brand_id="default",
        platform="telegram",
        owner_user_id=second_user.id,
        name="Sibling lifecycle account",
        external_account_id="telegram-lifecycle-sibling",
        public_id="tg_lifecycle_sibling",
        config={},
        capability={},
        automation_default="BOT_DRAFT_ONLY",
        status="active",
    )
    session.add_all([account, sibling_account])
    await session.commit()

    async with _client() as client:
        csrf = await _login(client, first_user.username)
        sibling_detail = await client.get(
            f"/app/t/default/channels/accounts/{sibling_account.id}"
        )
        forbidden_active = await client.post(
            f"/app/t/default/channels/accounts/{account.id}/automation",
            data={
                "csrf_token": csrf,
                "target": "BOT_ACTIVE",
                "expected_config_version": "1",
            },
        )

    async with _client() as client:
        csrf = await _login_superadmin(client)
        renamed = await client.post(
            f"/app/t/default/channels/accounts/{account.id}/rename",
            data={
                "csrf_token": csrf,
                "name": "Renamed lifecycle account",
                "expected_config_version": "1",
            },
        )
        stale_rename = await client.post(
            f"/app/t/default/channels/accounts/{account.id}/rename",
            data={
                "csrf_token": csrf,
                "name": "Stale write must fail",
                "expected_config_version": "1",
            },
        )
        disabled = await client.post(
            f"/app/t/default/channels/accounts/{account.id}/status",
            data={
                "csrf_token": csrf,
                "enabled": "false",
                "expected_status": "active",
                "expected_config_version": "2",
            },
        )

    assert sibling_detail.status_code == 404
    assert renamed.status_code == disabled.status_code == 303
    assert stale_rename.status_code == 409
    assert forbidden_active.status_code == 403
    assert forbidden_active.json() == {"detail": "tenant_admin_required"}
    await session.refresh(account)
    assert account.name == "Renamed lifecycle account"
    assert account.status == "DISABLED"
    assert account.config_version == 3

    async with _client() as client:
        admin_csrf = await _login_superadmin(client)
        assigned = await client.post(
            f"/app/t/default/channels/accounts/{account.id}/owner",
            data={
                "csrf_token": admin_csrf,
                "owner_user_id": str(second_user.id),
                "expected_config_version": "3",
            },
        )

    assert assigned.status_code == 303
    await session.refresh(account)
    assert account.owner_user_id == second_user.id
    assert account.config_version == 4

    async with _client() as client:
        await _login(client, first_user.username)
        former_owner_detail = await client.get(
            f"/app/t/default/channels/accounts/{account.id}"
        )
    async with _client() as client:
        await _login(client, second_user.username)
        current_owner_detail = await client.get(
            f"/app/t/default/channels/accounts/{account.id}"
        )

    assert former_owner_detail.status_code == 404
    assert current_owner_detail.status_code == 200
    audits = list(
        (
            await session.execute(
                select(models.AuditLog).where(
                    models.AuditLog.tenant_id == "default",
                    models.AuditLog.subject_id == str(account.id),
                )
            )
        ).scalars()
    )
    assert {
        "RENAME_PLATFORM_ACCOUNT",
        "SET_PLATFORM_ACCOUNT_STATUS",
        "ASSIGN_PLATFORM_ACCOUNT_OWNER",
    } <= {audit.action for audit in audits}


async def test_channel_provisioning_form_rejects_extra_fields_without_secret_leak(
    session,
    migrated_db,
) -> None:
    first_user, _second_user = await _seed_users(session)
    secret_token = "123456:super-secret-telegram-token"

    async with _client() as client:
        csrf = await _login(client, first_user.username)
        response = await client.post(
            "/app/t/default/channels/accounts/telegram",
            data={
                "csrf_token": csrf,
                "tenant_id": "attacker-tenant",
                "brand_id": "default",
                "automation_default": "BOT_DRAFT_ONLY",
                "token": secret_token,
                "unexpected_field": "must-fail-closed",
            },
        )

    assert response.status_code == 422
    assert response.json() == {"detail": "invalid_channel_account_form_fields"}
    assert secret_token not in response.text
    assert (
        await session.scalar(
            select(models.ProvisioningJob).where(
                models.ProvisioningJob.tenant_id == "default"
            )
        )
        is None
    )


async def test_kill_switch_and_job_retry_are_idempotent_scoped_and_audited(
    session,
    migrated_db,
    monkeypatch,
) -> None:
    from social_reply.application.account_management import channel_management
    from social_reply.shared.config import get_settings

    first_user, _second_user = await _seed_users(session, first_role="WORKSPACE_ADMIN")
    account = models.PlatformAccount(
        tenant_id="default",
        brand_id="default",
        platform="telegram",
        owner_user_id=first_user.id,
        name="Kill switch account",
        external_account_id="telegram-kill-switch",
        public_id="tg_kill_switch",
        config={},
        capability={},
        automation_default="BOT_DRAFT_ONLY",
        status="active",
    )
    session.add(account)
    await session.commit()
    authenticated = await authenticate(first_user.username, _USER_PASSWORD)
    assert authenticated is not None
    principal, _raw_token = authenticated
    job_id = await jobs.submit_provisioning_job(
        tenant_id="default",
        brand_id="default",
        platform="telegram",
        actor=principal.actor,
        request={"name": "Kill switch account", "idempotency_key": "channel-retry-audit"},
        secrets={"token": "channel-retry-token"},
        admin_session_id=principal.session_id,
    )
    job = await session.get(models.ProvisioningJob, job_id)
    assert job is not None
    job.status = "FAILED"
    job.current_step = "FAILED"
    job.next_attempt_at = None
    job.last_error_code = "PLATFORM_UNAVAILABLE"
    job.last_error_message = "provider details stay private"
    await session.commit()

    async def ignore_dispatch(*_args, **_kwargs):
        return None

    monkeypatch.setattr(channel_management, "dispatch_actor", ignore_dispatch)
    first_command_pending = asyncio.Event()
    second_command_applied = asyncio.Event()
    reconcile_command = channel_management.reconcile_account_kill_switch_command

    async def reconcile_after_both_commands_commit(operation_id, **kwargs):
        # Force the real supersession window between durable intent and Redis apply.
        if not first_command_pending.is_set():
            first_command_pending.set()
            async with asyncio.timeout(10):
                await second_command_applied.wait()
            return await reconcile_command(operation_id, **kwargs)
        result = await reconcile_command(operation_id, **kwargs)
        second_command_applied.set()
        return result

    monkeypatch.setattr(
        channel_management,
        "reconcile_account_kill_switch_command",
        reconcile_after_both_commands_commit,
    )
    redis = aioredis.from_url(get_settings().redis_url)
    redis_key = f"killswitch:account:default:{account.id}"
    await redis.delete(redis_key)
    first_client = _client()
    second_client = _client()
    try:
        first_csrf, second_csrf = await asyncio.gather(
            _login(first_client, first_user.username),
            _login(second_client, first_user.username),
        )

        async def enable_kill_switch(client: httpx.AsyncClient, csrf: str):
            return await client.post(
                f"/app/t/default/channels/accounts/{account.id}/kill-switch",
                data={"csrf_token": csrf, "enabled": "true"},
            )

        async def enable_after_first_command_commits():
            async with asyncio.timeout(10):
                await first_command_pending.wait()
            return await enable_kill_switch(second_client, second_csrf)

        async with asyncio.TaskGroup() as requests:
            first_request = requests.create_task(enable_kill_switch(first_client, first_csrf))
            second_request = requests.create_task(enable_after_first_command_commits())
        first_response = first_request.result()
        second_response = second_request.result()
        duplicate_response = await enable_kill_switch(first_client, first_csrf)
        assert await redis.exists(redis_key) == 1
        retry_response = await first_client.post(
            f"/app/t/default/channels/jobs/{job.id}/retry",
            data={"csrf_token": first_csrf},
        )
        disable_response = await first_client.post(
            f"/app/t/default/channels/accounts/{account.id}/kill-switch",
            data={"csrf_token": first_csrf, "enabled": "false"},
        )
    finally:
        await first_client.aclose()
        await second_client.aclose()

    assert first_response.status_code == 409
    assert first_response.json() == {"detail": "kill_switch_command_requires_reconfirmation"}
    assert second_response.status_code == duplicate_response.status_code == 303
    assert retry_response.status_code == disable_response.status_code == 303
    assert await redis.exists(redis_key) == 0
    await redis.aclose()
    await session.refresh(job)
    assert job.status == "PENDING"
    assert job.last_error_code is None

    kill_switch_audits = list(
        (
            await session.execute(
                select(models.AuditLog)
                .where(
                    models.AuditLog.tenant_id == "default",
                    models.AuditLog.action == "SET_PLATFORM_ACCOUNT_KILL_SWITCH",
                    models.AuditLog.subject_id == str(account.id),
                )
                .order_by(models.AuditLog.created_at)
            )
        ).scalars()
    )
    assert len(kill_switch_audits) == 4
    assert [audit.detail["status"] for audit in kill_switch_audits] == [
        "SUPERSEDED", "APPLIED", "UNCHANGED", "APPLIED",
    ]
    assert kill_switch_audits[0].detail["superseded_by_operation_id"] == str(
        kill_switch_audits[1].id
    )
    assert [audit.detail["enabled"] for audit in kill_switch_audits].count(True) == 3
    assert [audit.detail["changed"] for audit in kill_switch_audits].count(True) == 2
    retry_audit = await session.scalar(
        select(models.AuditLog).where(
            models.AuditLog.tenant_id == "default",
            models.AuditLog.action == "RETRY_PROVISIONING_JOB",
            models.AuditLog.subject_id == str(job.id),
        )
    )
    assert retry_audit is not None
    assert "provider details" not in str(retry_audit.detail)


async def test_kill_switch_persists_unknown_audit_when_redis_apply_fails(
    session,
    migrated_db,
    monkeypatch,
) -> None:
    from social_reply.application.account_management import (
        channel_management,
        kill_switch_recovery,
    )

    first_user, _second_user = await _seed_users(session, first_role="WORKSPACE_ADMIN")
    account = models.PlatformAccount(
        tenant_id="default",
        brand_id="default",
        platform="telegram",
        owner_user_id=first_user.id,
        name="Redis failure account",
        external_account_id="telegram-redis-failure",
        public_id="tg_redis_failure",
        config={},
        capability={},
        automation_default="BOT_DRAFT_ONLY",
        status="active",
    )
    session.add(account)
    await session.commit()

    class FailingRedis:
        async def exists(self, _key):
            return 0

        async def set(self, _key, _value):
            raise RuntimeError("redis unavailable")

        async def delete(self, _key):
            raise AssertionError("delete must not run")

        async def aclose(self):
            return None

    monkeypatch.setattr(
        kill_switch_recovery.aioredis,
        "from_url",
        lambda _url: FailingRedis(),
    )

    authenticated = await authenticate(first_user.username, _USER_PASSWORD)
    assert authenticated is not None
    principal, _raw_token = authenticated

    with pytest.raises(RuntimeError, match="redis unavailable"):
        await channel_management.set_channel_account_kill_switch(
            tenant_id="default",
            account_id=account.id,
            actor=channel_management.ChannelActor(
                actor=principal.actor,
                role="ADMIN" if principal.is_workspace_admin else "USER",
                user_id=principal.user_id,
                session_id=principal.session_id,
            ),
            enabled=True,
        )

    audit = await session.scalar(
        select(models.AuditLog).where(
            models.AuditLog.action == "SET_PLATFORM_ACCOUNT_KILL_SWITCH",
            models.AuditLog.subject_id == str(account.id),
        )
    )
    assert audit is not None
    assert audit.detail["operation_id"] == str(audit.id)
    assert audit.detail["tenant_id"] == "default"
    assert audit.detail["account_id"] == str(account.id)
    assert audit.detail["target_enabled"] is True
    assert audit.detail["enabled"] is True
    assert audit.detail["status"] == "UNKNOWN"
    assert audit.detail["outcome"] == "UNKNOWN"
    assert audit.detail["error_code"] == "REDIS_APPLY_UNCERTAIN"
    assert audit.detail["fail_closed"] is False


async def test_user_can_start_all_channels_oauth_flows_with_bound_context(
    session,
    migrated_db,
    monkeypatch,
) -> None:
    from social_reply.application.account_management.oauth import instagram, meta, x

    first_user, _second_user = await _seed_users(session)
    stored_contexts: dict[str, dict] = {}

    async def fake_x_request_token(**_kwargs):
        return {
            "oauth_token": "request-token",
            "oauth_token_secret": "request-secret",
        }

    async def store_x_state(_namespace, _key, payload):
        stored_contexts["x"] = dict(payload)

    async def store_meta_state(_namespace, _key, payload):
        stored_contexts["facebook"] = dict(payload)

    async def store_instagram_state(_namespace, _key, payload):
        stored_contexts["instagram"] = dict(payload)

    async def facebook_credentials(_tenant_id):
        return MetaAppCredentials(
            app_id="facebook-app",
            app_secret="facebook-secret",
            verify_token="facebook-verify",
            public_id="meta_public",
            platform_family="meta",
        )

    async def instagram_credentials(_tenant_id):
        return MetaAppCredentials(
            app_id="instagram-app",
            app_secret="instagram-secret",
            verify_token="instagram-verify",
            public_id="instagram_public",
            platform_family="instagram",
        )

    monkeypatch.setattr(x, "x_app_credentials", lambda: ("x-key", "x-secret"))
    monkeypatch.setattr(x, "_request_token", fake_x_request_token)
    monkeypatch.setattr(x, "store_oauth_state", store_x_state)
    monkeypatch.setattr(meta, "facebook_app_credentials", facebook_credentials)
    monkeypatch.setattr(meta, "store_oauth_state", store_meta_state)
    monkeypatch.setattr(instagram, "instagram_app_credentials", instagram_credentials)
    monkeypatch.setattr(instagram, "store_oauth_state", store_instagram_state)

    async with _client() as client:
        csrf = await _login(client, first_user.username)
        x_response = await client.post(
            "/app/t/default/channels/oauth/x/start",
            data={
                "csrf_token": csrf,
                "tenant_id": "tenant-b",
                "brand_id": "default",
            },
        )
        facebook_response = await client.post(
            "/app/t/default/channels/oauth/meta/start",
            data={
                "csrf_token": csrf,
                "tenant_id": "tenant-b",
                "brand_id": "default",
                "platform": "facebook",
            },
        )
        instagram_response = await client.post(
            "/app/t/default/channels/oauth/instagram/start",
            data={
                "csrf_token": csrf,
                "tenant_id": "tenant-b",
                "brand_id": "default",
            },
        )

    assert x_response.status_code == 303
    assert x_response.headers["location"].startswith("https://api.x.com/oauth/authorize?")
    assert facebook_response.status_code == 303
    assert facebook_response.headers["location"].startswith("https://www.facebook.com/")
    assert instagram_response.status_code == 303
    assert instagram_response.headers["location"].startswith(
        "https://www.instagram.com/oauth/authorize?"
    )
    for provider, context in stored_contexts.items():
        assert context["provider"] == provider
        assert context["tenant_id"] == "default"
        assert context["initiator_user_id"] == str(first_user.id)
        assert context["initiator_session_id"]
        assert context["surface"] == "channels"
        assert context["return_to"] == "/app/t/default/channels"


async def test_channels_oauth_cancellation_returns_without_admin_links(
    session,
    migrated_db,
    monkeypatch,
) -> None:
    from social_reply.application.account_management.oauth import instagram, meta, x

    first_user, _second_user = await _seed_users(session)

    async def x_context(_namespace, _key):
        return {
            "surface": "channels",
            "tenant_id": "default",
            "return_to": "/app/t/default/channels",
        }

    async def meta_context(_namespace, _key):
        return {
            "surface": "channels",
            "tenant_id": "default",
            "platform": "facebook",
            "return_to": "/app/t/default/channels",
        }

    async def instagram_context(_namespace, _key):
        return {
            "surface": "channels",
            "tenant_id": "default",
            "return_to": "/app/t/default/channels",
        }

    monkeypatch.setattr(x, "take_oauth_state", x_context)
    monkeypatch.setattr(meta, "take_oauth_state", meta_context)
    monkeypatch.setattr(instagram, "take_oauth_state", instagram_context)

    async with _client() as client:
        await _login(client, first_user.username)
        x_response = await client.get("/admin/oauth/x/callback?denied=request-token")
        facebook_response = await client.get(
            "/admin/oauth/meta/callback?error=access_denied&state=meta-state"
        )
        instagram_response = await client.get(
            "/admin/oauth/instagram/callback?error=access_denied&state=instagram-state"
        )

    for response in (x_response, facebook_response, instagram_response):
        assert response.status_code == 303
        assert response.headers["location"].startswith("/app/t/default/channels?")
        assert "/admin" not in response.headers["location"]
        assert response.headers["cache-control"] == "no-store"
