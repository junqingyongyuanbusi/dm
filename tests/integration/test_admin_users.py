import httpx
import pytest
from sqlalchemy import func, select

from apps.api.main import create_app
from social_reply.application.account_management.auth import verify_password
from social_reply.infrastructure.database import models

pytestmark = pytest.mark.integration


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://test",
        follow_redirects=False,
    )


async def _login(client: httpx.AsyncClient, username: str, password: str) -> str:
    page = await client.get("/admin/login")
    assert page.status_code == 200
    csrf = client.cookies["reply_admin_csrf"]
    response = await client.post(
        "/admin/login",
        data={"csrf_token": csrf, "username": username, "password": password},
    )
    assert response.status_code == 303
    return csrf


async def test_superadmin_creates_user_and_user_must_change_password(session, migrated_db):
    initial_password = "initial-password-123"
    async with _client() as client:
        csrf = await _login(client, "admin", "test-admin-password")
        page = await client.get("/admin/users")
        assert page.status_code == 200
        assert "创建普通用户" in page.text
        response = await client.post(
            "/admin/users",
            data={
                "csrf_token": csrf,
                "username": "alice",
                "initial_password": initial_password,
                "tenant_id": "tenant-a",
            },
        )
        assert response.status_code == 303

    user = (
        await session.execute(select(models.AdminUser).where(models.AdminUser.username == "alice"))
    ).scalar_one()
    assert user.password_hash != initial_password
    assert await verify_password(user.password_hash, initial_password)
    assert user.must_change_password is True

    async with _client() as client:
        csrf = await _login(client, "alice", initial_password)
        assert client.cookies.get("reply_admin_session")
        dashboard = await client.get("/admin")
        assert dashboard.status_code == 303
        assert dashboard.headers["location"] == "/admin/change-password"
        unchanged = await client.post(
            "/admin/change-password",
            data={
                "csrf_token": csrf,
                "current_password": initial_password,
                "new_password": initial_password,
                "confirm_password": initial_password,
            },
        )
        assert unchanged.status_code == 422
        still_blocked = await client.get("/admin")
        assert still_blocked.status_code == 303
        assert still_blocked.headers["location"] == "/admin/change-password"
        change = await client.post(
            "/admin/change-password",
            data={
                "csrf_token": csrf,
                "current_password": initial_password,
                "new_password": "alice-personal-password-456",
                "confirm_password": "alice-personal-password-456",
            },
        )
        assert change.status_code == 303
        assert change.headers["location"] == "/admin"
        dashboard = await client.get("/admin")
        assert dashboard.status_code == 200

    audit_count = (
        await session.execute(
            select(func.count())
            .select_from(models.AuditLog)
            .where(
                models.AuditLog.action == "CHANGE_PASSWORD",
                models.AuditLog.actor == "user:alice",
            )
        )
    ).scalar_one()
    assert audit_count == 1

    async with _client() as client:
        old_password_login = await client.get("/admin/login")
        assert old_password_login.status_code == 200
        csrf = client.cookies["reply_admin_csrf"]
        denied = await client.post(
            "/admin/login",
            data={
                "csrf_token": csrf,
                "username": "alice",
                "password": initial_password,
            },
        )
        assert denied.status_code == 401


async def test_tenant_user_cannot_open_user_management(session, migrated_db):
    from social_reply.application.account_management.auth import hash_password

    session.add(
        models.AdminUser(
            username="bob",
            password_hash=await hash_password("bob-personal-password-123"),
            tenant_id="tenant-b",
            must_change_password=False,
            status="active",
        )
    )
    await session.commit()
    async with _client() as client:
        await _login(client, "bob", "bob-personal-password-123")
        response = await client.get("/admin/users")
    assert response.status_code == 403
