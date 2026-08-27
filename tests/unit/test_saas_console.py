import uuid

import httpx

from apps.api.main import create_app
from social_reply.application.account_management.auth import Principal
from social_reply.application.account_management.saas_ui import render_saas_page


async def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://test",
        follow_redirects=False,
    )


def _tenant_principal() -> Principal:
    return Principal(
        session_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        username="tenant-admin",
        actor="user:tenant-admin",
        tenant_id="tenant-a",
        allowed_tenants=frozenset({"tenant-a"}),
    )


async def test_saas_workspace_redirects_unauthenticated_users_to_login() -> None:
    async with await _client() as client:
        response = await client.get("/app")

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


async def test_tenant_workspace_rejects_another_tenant_before_querying_database(
    monkeypatch,
) -> None:
    from social_reply.application.account_management import saas_console

    async def fake_current_principal(_request):
        return _tenant_principal()

    monkeypatch.setattr(saas_console, "current_principal", fake_current_principal)
    async with await _client() as client:
        response = await client.get("/app/t/tenant-b")

    assert response.status_code == 403
    assert response.json() == {"detail": "tenant_access_denied"}


async def test_tenant_user_cannot_open_system_admin_pages(monkeypatch) -> None:
    from social_reply.application.account_management import saas_console

    async def fake_current_principal(_request):
        return _tenant_principal()

    monkeypatch.setattr(saas_console, "current_principal", fake_current_principal)
    async with await _client() as client:
        response = await client.get("/admin/system/overview")

    assert response.status_code == 403
    assert response.json() == {"detail": "system_admin_required"}


def test_saas_shell_keeps_tenant_and_system_navigation_distinct() -> None:
    tenant_html = render_saas_page(
        principal=_tenant_principal(),
        title="首页",
        description="Tenant 工作区",
        body="<p>tenant body</p>",
        active_navigation="home",
        tenant_id="tenant-a",
    )
    system_principal = Principal(
        session_id=uuid.uuid4(),
        username="system-admin",
        actor="bootstrap:system-admin",
        allowed_tenants=frozenset({"tenant-a"}),
    )
    system_html = render_saas_page(
        principal=system_principal,
        title="系统总览",
        description="系统控制面",
        body="<p>system body</p>",
        active_navigation="system-overview",
        tenant_id=None,
        system_admin=True,
    )

    assert "/app/t/tenant-a/agents" in tenant_html
    assert "跨租户审计" not in tenant_html
    assert "SYSTEM ADMIN" in system_html
    assert "/admin/system/audit" in system_html
    assert "/app/t/tenant-a/agents" not in system_html
