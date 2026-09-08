import httpx
import pytest
from tests.integration.company_permission_support import (
    bootstrap_password,
    create_staff,
    login_client,
)

from apps.api.main import create_app
from social_reply.shared.config import get_settings

pytestmark = pytest.mark.integration

SYSTEM_PAGE_PATHS = (
    "/admin/system/overview",
    "/admin/system/health",
    "/admin/system/users",
    "/admin/system/safety",
    "/admin/system/audit",
)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://test",
        follow_redirects=False,
    )


async def test_workspace_admin_business_pages_expose_staff_without_system_entries(
    session, migrated_db
):
    staff = await create_staff(session, role="WORKSPACE_ADMIN")

    async with _client() as client:
        await login_client(client, username=staff.username, password=staff.password)
        workspace = await client.get("/app/t/default")
        users = await client.get("/admin/users")

    for response in (workspace, users):
        assert response.status_code == 200
        assert 'href="/admin/users"' in response.text
        assert 'href="/app/t/default/inbox"' in response.text
        for system_path in SYSTEM_PAGE_PATHS:
            assert f'href="{system_path}"' not in response.text
    assert staff.username in users.text


@pytest.mark.parametrize("system_path", SYSTEM_PAGE_PATHS)
async def test_workspace_admin_cannot_open_system_pages(session, migrated_db, system_path):
    staff = await create_staff(session, role="WORKSPACE_ADMIN")

    async with _client() as client:
        await login_client(client, username=staff.username, password=staff.password)
        response = await client.get(system_path)

    assert response.status_code == 403


async def test_superadmin_system_staff_page_keeps_business_navigation_separate(migrated_db):
    async with _client() as client:
        await login_client(
            client,
            username=get_settings().admin_username,
            password=bootstrap_password(),
        )
        response = await client.get("/admin/system/users")

    assert response.status_code == 200
    for system_path in SYSTEM_PAGE_PATHS:
        assert f'href="{system_path}"' in response.text
    assert 'href="/app"' in response.text
    assert 'href="/admin/users"' not in response.text
    assert 'href="/app/t/default/inbox"' not in response.text


async def test_superadmin_retains_documented_business_workspace_access(migrated_db):
    async with _client() as client:
        await login_client(
            client,
            username=get_settings().admin_username,
            password=bootstrap_password(),
        )
        workspace = await client.get("/app/t/default")
        users = await client.get("/admin/users")

    for response in (workspace, users):
        assert response.status_code == 200
        assert 'href="/admin/users"' in response.text
        assert 'href="/app/t/default/inbox"' in response.text
        assert 'href="/admin/system/overview"' in response.text
