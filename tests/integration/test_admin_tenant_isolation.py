import uuid

import httpx
import pytest
from sqlalchemy import insert

from apps.api.main import create_app
from social_reply.application.account_management.auth import hash_password
from social_reply.infrastructure.database import models

pytestmark = pytest.mark.integration


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://test",
        follow_redirects=False,
    )


async def _login(client: httpx.AsyncClient) -> str:
    await client.get("/admin/login")
    csrf = client.cookies["reply_admin_csrf"]
    response = await client.post(
        "/admin/login",
        data={
            "csrf_token": csrf,
            "username": "tenant-a-user",
            "password": "tenant-a-password-123",
        },
    )
    assert response.status_code == 303
    return csrf


async def _seed_user(session) -> None:
    session.add(
        models.AdminUser(
            username="tenant-a-user",
            password_hash=await hash_password("tenant-a-password-123"),
            tenant_id="tenant-a",
            must_change_password=False,
            status="active",
        )
    )
    await session.commit()


async def test_tenant_user_only_sees_own_knowledge_and_fixed_tenant_form(session, migrated_db):
    await _seed_user(session)
    session.add_all(
        [
            models.KnowledgeDocument(
                tenant_id="tenant-a",
                brand_id="default",
                question="tenant-a-visible-question",
                reply="visible",
            ),
            models.KnowledgeDocument(
                tenant_id="tenant-b",
                brand_id="default",
                question="tenant-b-secret-question",
                reply="secret",
            ),
        ]
    )
    await session.commit()

    async with _client() as client:
        csrf = await _login(client)
        page = await client.get("/admin/knowledge")
        assert page.status_code == 200
        assert "tenant-a-visible-question" in page.text
        assert "tenant-b-secret-question" not in page.text
        assert 'name="tenant_id" value="tenant-a"' in page.text
        assert "readonly" in page.text
        assert 'href="/admin/users"' not in page.text

        forbidden = await client.post(
            "/admin/knowledge/add",
            data={
                "csrf_token": csrf,
                "tenant_id": "tenant-b",
                "question": "cross tenant",
                "reply": "must fail",
            },
        )
        assert forbidden.status_code == 403


async def test_tenant_user_can_authorize_accounts_without_global_switch(
    session, migrated_db
):
    await _seed_user(session)
    account_id = uuid.uuid4()
    session.add(
        models.PlatformAccount(
            id=account_id,
            tenant_id="tenant-a",
            brand_id="default",
            platform="telegram",
            name="Tenant A Bot",
            public_id="tenant-a-bot",
            automation_default="BOT_DRAFT_ONLY",
            status="CONNECTED",
        )
    )
    await session.commit()

    async with _client() as client:
        csrf = await _login(client)
        page = await client.get("/admin/accounts")
        assert page.status_code == 200
        assert "账号授权" in page.text
        assert '<details class="collapse" open>' in page.text
        assert 'action="/admin/oauth/x/start"' in page.text
        assert 'action="/admin/oauth/meta/start"' in page.text
        assert 'action="/admin/oauth/instagram/start"' in page.text
        assert 'name="tenant_id" value="tenant-a"' in page.text
        assert f'action="/admin/accounts/{account_id}/automation"' in page.text
        assert 'name="scope" value="account"' in page.text
        assert "自动回复总开关" not in page.text
        assert "全局急停" not in page.text

        forbidden = await client.post(
            "/admin/killswitch/toggle",
            data={"csrf_token": csrf, "scope": "global", "tenant_id": "tenant-a"},
        )
        assert forbidden.status_code == 403
        assert forbidden.json()["detail"] == "superadmin_required"


async def test_tenant_user_can_start_oauth_only_for_own_tenant(
    session, migrated_db, monkeypatch
):
    from social_reply.application.account_management.oauth import x as x_oauth

    await _seed_user(session)
    captured: dict = {}

    async def fake_request_token(**_kwargs):
        return {
            "oauth_token": "tenant-request-token",
            "oauth_token_secret": "tenant-request-secret",
        }

    async def fake_store(namespace, key, payload):
        captured.update(namespace=namespace, key=key, payload=payload)

    monkeypatch.setattr(x_oauth, "x_app_credentials", lambda: ("app-key", "app-secret"))
    monkeypatch.setattr(x_oauth, "_request_token", fake_request_token)
    monkeypatch.setattr(x_oauth, "store_oauth_state", fake_store)

    async with _client() as client:
        csrf = await _login(client)
        allowed = await client.post(
            "/admin/oauth/x/start",
            data={"csrf_token": csrf, "tenant_id": "tenant-a", "brand_id": "default"},
        )
        assert allowed.status_code == 303
        assert "oauth_token=tenant-request-token" in allowed.headers["location"]
        assert captured["namespace"] == "x"
        assert captured["payload"]["tenant_id"] == "tenant-a"
        assert captured["payload"]["session_id"]

        denied = await client.post(
            "/admin/oauth/x/start",
            data={"csrf_token": csrf, "tenant_id": "tenant-b", "brand_id": "default"},
        )
        assert denied.status_code == 403


async def test_tenant_user_cannot_open_other_tenant_job(session, migrated_db):
    await _seed_user(session)
    job_id = uuid.uuid4()
    await session.execute(
        insert(models.ProvisioningJob).values(
            id=job_id,
            tenant_id="tenant-b",
            brand_id="default",
            platform="telegram",
            idempotency_key="tenant-b-job",
            request={},
            status="COMPLETED",
            actor="user:admin",
        )
    )
    await session.commit()

    async with _client() as client:
        await _login(client)
        response = await client.get(f"/admin/jobs/{job_id}")
    assert response.status_code == 404


async def test_tenant_user_does_not_see_unscoped_raw_event_health(session, migrated_db):
    await _seed_user(session)
    await session.execute(
        insert(models.RawEvent).values(
            source="secret-unscoped-source",
            payload={"secret": True},
            processing_status="FAILED_BEFORE_NORMALIZATION",
        )
    )
    await session.commit()

    async with _client() as client:
        await _login(client)
        response = await client.get("/admin/delivery")
    assert response.status_code == 200
    assert "secret-unscoped-source" not in response.text
