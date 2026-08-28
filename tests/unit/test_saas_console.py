import uuid
from types import SimpleNamespace

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


def _ordinary_user_principal() -> Principal:
    return Principal(
        session_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        username="operator",
        actor="user:operator",
        tenant_id="tenant-a",
        allowed_tenants=frozenset({"tenant-a"}),
        role="USER",
    )


async def test_saas_workspace_redirects_unauthenticated_users_to_login() -> None:
    async with await _client() as client:
        response = await client.get("/app")

    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login?next=%2Fapp"


async def test_saas_workspace_login_redirect_preserves_path_and_query() -> None:
    async with await _client() as client:
        response = await client.get("/app/t/tenant-a/inbox?queue=drafts")

    assert response.status_code == 303
    assert response.headers["location"] == (
        "/auth/login?next=%2Fapp%2Ft%2Ftenant-a%2Finbox%3Fqueue%3Ddrafts"
    )


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


def test_ordinary_user_shell_only_shows_authorized_navigation_items() -> None:
    html = render_saas_page(
        principal=_ordinary_user_principal(),
        title="首页",
        description="普通用户工作区",
        body="<p>body</p>",
        active_navigation="home",
        tenant_id="tenant-a",
    )

    for label in (
        "首页",
        "收件箱",
        "对话",
        "Agents",
        "知识查询",
        "我的活动",
        "Channels",
        "个人中心",
    ):
        assert f">{label}<" in html
    for forbidden_label in ("审计中心", "Processing Journey", "工作区设置", "打开系统后台"):
        assert forbidden_label not in html
    assert 'href="/admin"' not in html


def test_ordinary_user_agent_sections_never_link_to_admin_pages() -> None:
    from social_reply.application.account_management import saas_console

    account = SimpleNamespace(
        name="My Telegram",
        platform="telegram",
        status="active",
        automation_default="BOT_DRAFT_ONLY",
        config_version=1,
    )
    channels_html = saas_console._render_agent_channels(
        [account],
        tenant_id="tenant-a",
        is_admin=False,
    )
    instructions_html = saas_console._render_agent_instructions(
        tenant_id="tenant-a",
        agent_id="default",
        prompt_pointer=None,
        prompt_version=None,
        is_admin=False,
    )
    knowledge_html = saas_console._render_agent_knowledge(
        "tenant-a",
        "default",
        {"published": 0, "draft": 0},
        is_admin=False,
    )

    combined_html = channels_html + instructions_html + knowledge_html
    assert "/admin" not in combined_html
    assert "/app/t/tenant-a/channels" in channels_html
    assert "/app/t/tenant-a/knowledge-query" in knowledge_html


def test_ordinary_user_without_accounts_gets_direct_authorization_action() -> None:
    from social_reply.application.account_management import saas_console

    summary = saas_console.InboxSummary(
        human_count=0,
        draft_count=0,
        delivery_count=0,
        oldest_human_at=None,
        oldest_draft_at=None,
        oldest_delivery_at=None,
    )

    html = saas_console._home_next_action(
        _ordinary_user_principal(),
        "tenant-a",
        summary,
        0,
    )

    assert "授权你的第一个平台账号" in html
    assert "/app/t/tenant-a/channels" in html
    assert "授权新账号" in html


def test_channel_avatar_only_accepts_known_https_provider_hosts() -> None:
    from social_reply.application.account_management import saas_console

    assert (
        saas_console._safe_channel_avatar_url(
            "https://pbs.twimg.com/profile_images/account.jpg"
        )
        == "https://pbs.twimg.com/profile_images/account.jpg"
    )
    assert (
        saas_console._safe_channel_avatar_url(
            "https://scontent.example.fbcdn.net/avatar.jpg"
        )
        == "https://scontent.example.fbcdn.net/avatar.jpg"
    )
    for unsafe_url in (
        "http://pbs.twimg.com/avatar.jpg",
        "https://pbs.twimg.com.evil.example/avatar.jpg",
        "https://user:password@pbs.twimg.com/avatar.jpg",
        "javascript:alert(1)",
        "https://example.com/avatar.jpg",
    ):
        assert saas_console._safe_channel_avatar_url(unsafe_url) is None


def test_channel_avatar_falls_back_without_rendering_unsafe_url() -> None:
    from social_reply.application.account_management import saas_console

    account = SimpleNamespace(
        avatar_url="https://attacker.example/avatar.jpg",
        name="Owned Account",
        platform="instagram",
    )

    avatar_html = saas_console._channel_avatar(account)

    assert "attacker.example" not in avatar_html
    assert "fallback" in avatar_html
    assert ">O<" in avatar_html


async def test_ordinary_user_cannot_open_admin_or_tenant_governance_pages(monkeypatch) -> None:
    from social_reply.application.account_management import admin, saas_console

    principal = _ordinary_user_principal()

    async def fake_current_principal(_request):
        return principal

    monkeypatch.setattr(admin, "current_principal", fake_current_principal)
    monkeypatch.setattr(saas_console, "current_principal", fake_current_principal)
    async with await _client() as client:
        admin_response = await client.get("/admin")
        audit_response = await client.get("/app/t/tenant-a/audit")
        draft_queue_response = await client.get(
            "/app/t/tenant-a/inbox?queue=drafts"
        )

    assert admin_response.status_code == 403
    assert admin_response.json() == {"detail": "admin_required"}
    assert audit_response.status_code == 403
    assert audit_response.json() == {"detail": "admin_required"}
    assert draft_queue_response.status_code == 403
    assert draft_queue_response.json() == {
        "detail": "inbox_queue_access_denied"
    }
