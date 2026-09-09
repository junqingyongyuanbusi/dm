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
        page = await client.get("/admin/system/users")
        assert page.status_code == 200
        assert "创建员工账号" in page.text
        assert 'value="ADMIN"' not in page.text
        assert 'value="SUPERADMIN"' not in page.text
        assert 'value="WORKSPACE_ADMIN"' in page.text
        assert 'value="AGENT"' in page.text
        rejected_admin = await client.post(
            "/admin/system/users",
            data={
                "csrf_token": csrf,
                "username": "forbidden-admin",
                "initial_password": initial_password,
                "role": "ADMIN",
                "bootstrap_password": "test-admin-password",
            },
        )
        assert rejected_admin.status_code == 422
        assert rejected_admin.json() == {"detail": "invalid_user_role"}
        response = await client.post(
            "/admin/users",
            data={
                "csrf_token": csrf,
                "username": "alice",
                "initial_password": initial_password,
                "role": "USER",
                "bootstrap_password": "test-admin-password",
            },
        )
        assert response.status_code == 303

    user = (
        await session.execute(select(models.AdminUser).where(models.AdminUser.username == "alice"))
    ).scalar_one()
    assert user.role == "AGENT"
    assert user.tenant_id == "default"
    assert user.password_hash != initial_password
    assert await verify_password(user.password_hash, initial_password)
    assert user.must_change_password is True

    async with _client() as client:
        csrf = await _login(client, "alice", initial_password)
        assert client.cookies.get("reply_admin_session")
        dashboard = await client.get("/admin")
        assert dashboard.status_code == 303
        assert dashboard.headers["location"] == "/auth/change-password"
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
        assert still_blocked.headers["location"] == "/auth/change-password"
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
        assert change.headers["location"] == "/app"
        dashboard = await client.get("/admin")
        assert dashboard.status_code == 403
        assert dashboard.json() == {"detail": "tenant_admin_required"}

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
            role="USER",
            must_change_password=False,
            status="active",
        )
    )
    await session.commit()
    async with _client() as client:
        await _login(client, "bob", "bob-personal-password-123")
        legacy = await client.get("/admin/users")
        response = await client.get("/admin/system/users")
    assert legacy.status_code == 403
    assert response.status_code == 403


async def test_user_management_current_and_legacy_routes_are_bilingual(migrated_db):
    async with _client() as client:
        await _login(client, "admin", "test-admin-password")
        current_chinese = await client.get("/admin/system/users")
        workspace_chinese = await client.get("/admin/users")
        client.cookies.set("reply_ui_locale", "en")
        current_english = await client.get("/admin/system/users")
        workspace_english = await client.get("/admin/users")

    for response in (current_chinese, current_english):
        assert response.status_code == 200
        assert '<aside class="saas-sidebar"' in response.text
        assert 'data-page-layout="page"' in response.text
        assert "aria-current='page'" in response.text

    for response in (workspace_chinese, workspace_english):
        assert response.status_code == 200
        assert 'action="/admin/users"' in response.text
        assert 'action="/admin/system/users"' not in response.text

    assert "成员与权限" in workspace_chinese.text
    assert "Members and permissions" in workspace_english.text
    assert "创建员工账号" in current_chinese.text
    assert "Create staff account" in current_english.text
    assert "创建员工账号" not in current_english.text


async def test_system_user_lifecycle_requires_csrf_and_bootstrap_reauthentication(
    session,
    migrated_db,
):
    initial_password = "system-user-initial-password-123"
    reset_password = "system-user-reset-password-456"
    async with _client() as client:
        csrf = await _login(client, "admin", "test-admin-password")
        page = await client.get("/admin/system/users")
        assert page.status_code == 200
        assert 'name="tenant_id"' not in page.text
        assert "default" in page.text

        missing_csrf = await client.post(
            "/admin/system/users",
            data={
                "username": "system-operator",
                "initial_password": initial_password,
                "role": "USER",
                "bootstrap_password": "test-admin-password",
            },
        )
        assert missing_csrf.status_code == 403

        wrong_password = await client.post(
            "/admin/system/users",
            data={
                "csrf_token": csrf,
                "username": "system-operator",
                "initial_password": initial_password,
                "role": "USER",
                "bootstrap_password": "wrong-bootstrap-password",
            },
        )
        assert wrong_password.status_code == 401

        created = await client.post(
            "/admin/system/users",
            data={
                "csrf_token": csrf,
                "username": "system-operator",
                "initial_password": initial_password,
                "role": "USER",
                "bootstrap_password": "test-admin-password",
            },
        )
        assert created.status_code == 303
        assert created.headers["location"].startswith("/admin/system/users")

    user = await session.scalar(
        select(models.AdminUser).where(models.AdminUser.username == "system-operator")
    )
    assert user is not None
    assert user.tenant_id == "default"
    assert user.role == "AGENT"
    assert user.status == "active"
    assert user.must_change_password is True
    assert await verify_password(user.password_hash, initial_password)
    user_id = user.id

    async with _client() as user_client:
        await _login(user_client, "system-operator", initial_password)
        existing_session_id = await session.scalar(
            select(models.AdminSession.id).where(models.AdminSession.user_id == user_id)
        )
        assert existing_session_id is not None

    async with _client() as client:
        csrf = await _login(client, "admin", "test-admin-password")
        reset = await client.post(
            f"/admin/system/users/{user_id}/password-reset",
            data={
                "csrf_token": csrf,
                "initial_password": reset_password,
                "bootstrap_password": "test-admin-password",
            },
        )
        assert reset.status_code == 303

        revoked = await client.post(
            f"/admin/system/users/{user_id}/sessions/revoke",
            data={
                "csrf_token": csrf,
                "bootstrap_password": "test-admin-password",
            },
        )
        assert revoked.status_code == 303

        disabled = await client.post(
            f"/admin/system/users/{user_id}/status",
            data={
                "csrf_token": csrf,
                "status": "disabled",
                "bootstrap_password": "test-admin-password",
            },
        )
        assert disabled.status_code == 303

    session.expire_all()
    updated_user = await session.get(models.AdminUser, user_id)
    assert updated_user is not None
    assert updated_user.role == "AGENT"
    assert updated_user.status == "disabled"
    assert updated_user.must_change_password is True
    assert await verify_password(updated_user.password_hash, reset_password)
    assert (
        await session.scalar(
            select(func.count())
            .select_from(models.AdminSession)
            .where(models.AdminSession.user_id == user_id)
        )
        == 0
    )

    audit_rows = list(
        (
            await session.execute(
                select(models.AuditLog).where(models.AuditLog.subject_id == str(user_id))
            )
        ).scalars()
    )
    assert {audit.action for audit in audit_rows} >= {
        "CREATE_USER",
        "FORCE_PASSWORD_RESET",
        "REVOKE_USER_SESSIONS",
        "SET_USER_STATUS",
    }
    serialized_details = " ".join(str(audit.detail) for audit in audit_rows)
    assert initial_password not in serialized_details
    assert reset_password not in serialized_details
    assert "password_hash" not in serialized_details
