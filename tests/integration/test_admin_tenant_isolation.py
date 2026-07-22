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
