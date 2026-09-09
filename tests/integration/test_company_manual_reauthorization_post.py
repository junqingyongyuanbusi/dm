import uuid

import httpx
import pytest
from sqlalchemy import select
from tests.integration.test_human_feishu_outbox_contracts import _seed_conversation

from apps.api.main import create_app
from social_reply.application.account_management import channel_management
from social_reply.application.account_management.auth import hash_password
from social_reply.infrastructure.database import models

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("has_owner", [True, False])
async def test_manual_reauthorization_post_creates_canonical_job_without_inbox_access(
    session, monkeypatch, has_owner
):
    password = "manual-reconnection-password"
    password_hash = await hash_password(password)
    owner = models.AdminUser(
        username=f"connection-owner-{uuid.uuid4().hex}",
        password_hash=password_hash,
        tenant_id="default",
        role="USER",
        status="active",
        must_change_password=False,
    )
    operator = models.AdminUser(
        username=f"connection-operator-{uuid.uuid4().hex}",
        password_hash=password_hash,
        tenant_id="default",
        role="OPERATOR",
        status="active",
        must_change_password=False,
    )
    session.add_all([owner, operator])
    await session.commit()
    owner_id, operator_id, username = owner.id, operator.id, operator.username
    account_id, conversation_id, _message_id, _work_id = await _seed_conversation(
        session, tenant_id="default", shared_with_support=False, state="BOT_DRAFT_ONLY"
    )
    account = await session.get(models.PlatformAccount, account_id)
    expected_owner = owner_id if has_owner else None
    account.owner_user_id = expected_owner
    account.automation_default = "BOT_DRAFT_ONLY"
    version = account.config_version
    session.add(
        models.AccountReauthorizationGrant(
            tenant_id="default",
            platform_account_id=account_id,
            user_id=operator_id,
            active=True,
        )
    )
    await session.commit()
    dispatched = []

    async def capture_dispatch(actor, *args, **kwargs):
        dispatched.append(args)

    monkeypatch.setattr(channel_management, "dispatch_actor", capture_dispatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        login_page = await client.get("/auth/login")
        assert login_page.status_code == 200
        csrf = client.cookies["reply_admin_csrf"]
        login = await client.post(
            "/auth/login",
            data={
                "csrf_token": csrf,
                "username": username,
                "password": password,
            },
        )
        assert login.status_code == 303
        assert await session.scalar(select(models.ProvisioningJob.id).limit(1)) is None
        maintenance = await client.get("/app/t/default/channels")
        assert maintenance.status_code == 200
        assert 'id="maintenance-channels"' in maintenance.text
        assert f"/channels/accounts/{account_id}" in maintenance.text
        private = await client.get(f"/app/t/default/conversations/{conversation_id}")
        assert private.status_code in {403, 404}
        grant = await session.scalar(
            select(models.AccountReauthorizationGrant).where(
                models.AccountReauthorizationGrant.user_id == operator_id,
                models.AccountReauthorizationGrant.platform_account_id == account_id,
            )
        )
        grant.active = False
        await session.commit()
        removed = await client.get("/app/t/default/channels")
        assert removed.status_code == 200
        assert 'id="maintenance-channels"' not in removed.text
        grant.active = True
        await session.commit()
        for forbidden_option in ("rotate_webhook_secret", "drop_pending_updates"):
            rejected = await client.post(
                "/app/t/default/channels/accounts/telegram",
                params={"target_account_id": str(account_id), "expected_config_version": version},
                data={
                    "csrf_token": csrf,
                    "token": "replacement-token-for-test",
                    forbidden_option: "true",
                },
            )
            assert rejected.status_code == 422
            assert rejected.json() == {"detail": "reauthorization_scope_fields_forbidden"}
            assert await session.scalar(select(models.ProvisioningJob.id).limit(1)) is None
            assert dispatched == []
        response = await client.post(
            "/app/t/default/channels/accounts/telegram",
            params={"target_account_id": str(account_id), "expected_config_version": version},
            data={"csrf_token": csrf, "token": "replacement-token-for-test"},
        )
        assert response.status_code == 303, response.text
        job = await session.scalar(
            select(models.ProvisioningJob).where(
                models.ProvisioningJob.initiator_user_id == operator_id,
                models.ProvisioningJob.target_account_id == account_id,
            )
        )
        assert job is not None
        assert job.operation == "REAUTHORIZE"
        assert job.authority_kind == "STAFF_SESSION"
        assert job.status == "PENDING"
        assert job.expected_config_version == version
        assert job.staging_secret is not None
        assert "replacement-token-for-test" not in str(job.staging_secret)
        assert len(dispatched) == 1
        private_conversation = await client.get(f"/app/t/default/conversations/{conversation_id}")
        assert private_conversation.status_code in {403, 404}
    await session.refresh(account)
    assert account.owner_user_id == expected_owner
    assert account.shared_with_support is False
