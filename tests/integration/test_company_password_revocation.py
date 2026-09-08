from __future__ import annotations

import asyncio

import httpx
import pytest
from sqlalchemy import func, select
from tests.integration.company_permission_support import (
    bootstrap_identity,
    bootstrap_password,
    create_staff,
    login_client,
)

from apps.api.main import create_app
from social_reply.application.account_management import admin as admin_module
from social_reply.application.account_management.auth import Principal, verify_password
from social_reply.application.account_management.system_user_management import (
    SystemUserActor,
    revoke_system_user_sessions,
    set_system_user_status,
)
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.shared.config import get_settings

pytestmark = pytest.mark.integration


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://test",
        follow_redirects=False,
    )


@pytest.mark.parametrize("revocation", ["sessions", "disabled"])
async def test_password_change_revalidates_after_bootstrap_revokes_initial_session(
    session, monkeypatch, revocation
):
    staff = await create_staff(session, username=f"password-race-{revocation}")
    principal_seen = asyncio.Event()
    continue_request = asyncio.Event()
    original_web_principal = admin_module._web_principal
    captured: list[Principal] = []

    async def paused_web_principal(request, **kwargs):
        result = await original_web_principal(request, **kwargs)
        if request.method == "POST" and request.url.path == "/auth/change-password":
            assert isinstance(result, Principal)
            captured.append(result)
            principal_seen.set()
            await continue_request.wait()
        return result

    monkeypatch.setattr(admin_module, "_web_principal", paused_web_principal)
    async with _client() as client:
        csrf = await login_client(client, username=staff.username, password=staff.password)
        request_task = asyncio.create_task(
            client.post(
                "/auth/change-password",
                data={
                    "csrf_token": csrf,
                    "current_password": staff.password,
                    "new_password": "replacement-password-456",
                    "confirm_password": "replacement-password-456",
                },
            )
        )
        await asyncio.wait_for(principal_seen.wait(), timeout=5)
        assert captured[0].user_id == staff.user_id

        bootstrap, _bootstrap_token = await bootstrap_identity()
        actor = SystemUserActor(actor=bootstrap.actor, session_id=bootstrap.session_id)
        try:
            if revocation == "sessions":
                await revoke_system_user_sessions(
                    user_id=staff.user_id,
                    bootstrap_password=bootstrap_password(),
                    actor=actor,
                )
            else:
                await set_system_user_status(
                    user_id=staff.user_id,
                    user_status="disabled",
                    bootstrap_password=bootstrap_password(),
                    actor=actor,
                    emergency_reason="Concurrent credential revocation test",
                )
            async with get_session_factory()() as committed:
                assert await committed.get(models.AdminSession, captured[0].session_id) is None
        finally:
            continue_request.set()

        response = await asyncio.wait_for(request_task, timeout=5)
        assert response.status_code == 401
        assert response.json() == {"detail": "password_change_session_revoked"}

    async with get_session_factory()() as fresh:
        user = await fresh.get(models.AdminUser, staff.user_id)
        assert user is not None
        if revocation == "disabled":
            assert user.status == "disabled"
        else:
            assert user.status == "active"
        assert await verify_password(user.password_hash, staff.password)
        assert (
            await fresh.scalar(
                select(func.count())
                .select_from(models.AdminSession)
                .where(models.AdminSession.user_id == staff.user_id)
            )
            == 0
        )
        assert (
            await fresh.scalar(
                select(models.AuditLog.id).where(
                    models.AuditLog.action == "CHANGE_PASSWORD",
                    models.AuditLog.subject_id == str(staff.user_id),
                )
            )
            is None
        )


async def test_first_forced_password_change_issues_new_session(session):
    initial_password = "first-login-initial-password-123"
    new_password = "first-login-personal-password-456"
    staff = await create_staff(
        session,
        username="company-first-login",
        must_change_password=True,
        password=initial_password,
    )

    async with _client() as client:
        csrf = await login_client(client, username=staff.username, password=initial_password)
        change = await client.post(
            "/auth/change-password",
            data={
                "csrf_token": csrf,
                "current_password": initial_password,
                "new_password": new_password,
                "confirm_password": new_password,
            },
        )
        assert change.status_code == 303
        assert change.headers["location"] == "/app"
        assert client.cookies.get("reply_admin_session")
        dashboard = await client.get("/app")
        assert dashboard.status_code == 303
        assert dashboard.headers["location"] == "/app/t/default"

    async with get_session_factory()() as fresh:
        user = await fresh.get(models.AdminUser, staff.user_id)
        assert user is not None
        assert user.must_change_password is False
        assert await verify_password(user.password_hash, new_password)
        assert not await verify_password(user.password_hash, initial_password)
        assert (
            await fresh.scalar(
                select(func.count())
                .select_from(models.AdminSession)
                .where(models.AdminSession.user_id == staff.user_id)
            )
            == 1
        )


async def test_bootstrap_default_guard_and_database_workspace_admin_boundaries(session):
    workspace_admin = await create_staff(
        session,
        username="company-boundary-workspace-admin",
        role="WORKSPACE_ADMIN",
    )
    settings = get_settings()

    async with _client() as bootstrap_client:
        await login_client(
            bootstrap_client,
            username=settings.admin_username,
            password=settings.admin_password.get_secret_value(),
        )
        default_workspace = await bootstrap_client.get("/app/t/default")
        system_users = await bootstrap_client.get("/admin/system/users")
        nondefault_workspace = await bootstrap_client.get("/app/t/tenant-not-default")

    assert default_workspace.status_code == 200
    assert system_users.status_code == 200
    assert nondefault_workspace.status_code == 404
    assert nondefault_workspace.json() == {"detail": "tenant_workspace_not_found"}

    async with _client() as workspace_client:
        await login_client(
            workspace_client,
            username=workspace_admin.username,
            password=workspace_admin.password,
        )
        system_users = await workspace_client.get("/admin/system/users")
        nondefault_workspace = await workspace_client.get("/app/t/tenant-not-default")
        default_workspace = await workspace_client.get("/app/t/default")

    assert system_users.status_code == 403
    assert system_users.json() == {"detail": "superadmin_required"}
    assert nondefault_workspace.status_code == 404
    assert nondefault_workspace.json() == {"detail": "tenant_workspace_not_found"}
    assert default_workspace.status_code == 200
