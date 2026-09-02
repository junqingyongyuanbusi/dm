import uuid

import httpx
import pytest
from fastapi import HTTPException, Request

from apps.api.main import create_app
from social_reply.application.account_management.auth import Principal
from social_reply.application.account_management.ui_i18n import reset_locale, set_locale


async def _client():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://test",
        follow_redirects=False,
    )


def _database_principal(
    *,
    role: str = "USER",
    tenant_id: str = "default",
    username: str = "database-user",
) -> Principal:
    return Principal(
        session_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        username=username,
        actor=f"user:{username}",
        tenant_id=tenant_id,
        allowed_tenants=frozenset({tenant_id}),
        role=role,
    )


def _superadmin_principal() -> Principal:
    return Principal(
        session_id=uuid.uuid4(),
        username="system-admin",
        actor="bootstrap:system-admin",
        tenant_id="default",
        allowed_tenants=frozenset({"default"}),
        role="SUPERADMIN",
    )


def _request(path: str) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [],
            "client": ("test", 1234),
            "server": ("test", 80),
        }
    )


async def test_admin_dashboard_redirects_to_login():
    async with await _client() as client:
        response = await client.get("/admin")
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login"


async def test_admin_login_uses_full_width_auth_shell():
    async with await _client() as client:
        response = await client.get("/auth/login")

    assert response.status_code == 200
    assert '<div class="app-shell app-shell-auth"><main id="main-content">' in response.text
    assert 'class="sidebar"' not in response.text
    assert '<script src="/static/theme.js?v=' in response.text
    assert '<link rel="stylesheet" href="/static/saas.css?v=' in response.text
    assert '<script src="/static/app.js?v=' in response.text
    assert '" defer></script>' in response.text
    assert "<style>" not in response.text
    assert "data-sidebar-toggle" not in response.text
    assert "data-sidebar-close" not in response.text
    assert 'href="/auth/login?ui_lang=en"' in response.text
    assert 'class="saas-brand" href="/auth/login"' in response.text


async def test_admin_login_renders_english_locale_in_shared_auth_shell():
    async with await _client() as client:
        switch_response = await client.get("/auth/login?ui_lang=en")
        response = await client.get(switch_response.headers["location"])

    assert switch_response.status_code == 303
    assert response.status_code == 200
    assert '<html lang="en">' in response.text
    assert "Sign in to Reply Core" in response.text
    assert "Username" in response.text
    assert "Password" in response.text
    assert 'href="/auth/login?ui_lang=zh-CN"' in response.text
    assert 'class="sidebar"' not in response.text


async def test_language_switch_link_preserves_login_return_target_and_filters():
    async with await _client() as client:
        response = await client.get(
            "/auth/login?next=%2Fapp%2Ft%2Ftenant-a%2Finbox%3Fqueue%3Ddrafts"
            "&view=compact"
        )

    assert response.status_code == 200
    assert (
        'href="/auth/login?next=%2Fapp%2Ft%2Ftenant-a%2Finbox%3Fqueue%3Ddrafts'
        '&amp;view=compact&amp;ui_lang=en"'
        in response.text
    )


def test_admin_shared_shell_groups_navigation_and_marks_active_page():
    from social_reply.application.account_management import admin

    principal = Principal(
        session_id=uuid.uuid4(),
        username="root-admin",
        actor="bootstrap:root-admin",
        allowed_tenants=frozenset({"default"}),
        role="SUPERADMIN",
    )

    page_html = admin._page(
        "Overview",
        "<h1>Overview</h1>",
        active="overview",
        show_users=True,
        principal=principal,
    )

    assert ">系统<" in page_html
    for href in (
        "/admin/system/overview",
        "/admin/system/safety",
        "/admin/system/users",
        "/admin/system/audit",
    ):
        assert f'href="{href}"' in page_html
    for forbidden_href in (
        "/admin/system/health",
        "/admin/inbox",
        "/admin/content/knowledge",
        "/admin/integrations/accounts",
    ):
        assert f'href="{forbidden_href}"' not in page_html
    assert 'href="/app"' in page_html
    assert "进入租户工作区" in page_html
    assert "root-admin" in page_html
    assert "data-sidebar-toggle" in page_html
    assert "data-sidebar-close" in page_html
    assert "data-sidebar" in page_html
    sidebar_start = page_html.index('<aside class="sidebar">')
    sidebar_end = page_html.index("</aside>", sidebar_start)
    topbar_start = page_html.index('<header class="saas-topbar')
    topbar_end = page_html.index("</header>", topbar_start)
    assert sidebar_start < page_html.index('class="saas-brand"') < sidebar_end
    assert "saas-brand" not in page_html[topbar_start:topbar_end]
    assert '<svg class="saas-nav-icon" aria-hidden="true"' in page_html
    assert page_html.count('class="saas-nav-icon"') >= 4
    assert "<style>" not in page_html


def test_tenant_admin_shell_hides_superadmin_system_controls():
    from social_reply.application.account_management import admin

    principal = _database_principal()

    page_html = admin._page(
        "Overview",
        "<h1>Overview</h1>",
        active="overview",
        principal=principal,
    )

    for forbidden_href in (
        "/admin/inbox",
        "/admin/conversations",
        "/admin/content/knowledge",
        "/admin/integrations/accounts",
        "/admin/system/health",
        "/admin/system/overview",
        "/admin/system/safety",
        "/admin/system/users",
        "/admin/system/audit",
    ):
        assert forbidden_href not in page_html


def test_admin_shell_and_status_labels_follow_english_locale():
    from social_reply.application.account_management import admin

    principal = _database_principal()
    locale_token = set_locale("en")
    try:
        page_html = admin._page(
            "Overview",
            "<h1>Overview</h1>",
            active="overview",
            principal=principal,
        )
        failed_badge = admin._pill("FAILED")
    finally:
        reset_locale(locale_token)

    for group_label in ("Operations", "Content &amp; AI", "Integrations"):
        assert f">{group_label}<" not in page_html
    assert ">System<" not in page_html
    assert '<html lang="en">' in page_html
    assert "database-user" in page_html
    assert 'title="FAILED">Failed<' in failed_badge


async def test_admin_login_sets_http_only_session_cookie(monkeypatch):
    from social_reply.application.account_management import admin

    async def fake_authenticate(username, password):
        assert username == "admin"
        assert password == "test-admin-password"
        return (
            Principal(
                session_id=__import__("uuid").uuid4(),
                username="admin",
                actor="user:admin",
                allowed_tenants=frozenset({"default"}),
                role="SUPERADMIN",
            ),
            "opaque-session-token",
        )

    monkeypatch.setattr(admin, "authenticate", fake_authenticate)
    async with await _client() as client:
        page = await client.get("/auth/login")
        csrf = client.cookies["reply_admin_csrf"]
        assert page.status_code == 200
        response = await client.post(
            "/auth/login",
            data={
                "csrf_token": csrf,
                "username": "admin",
                "password": "test-admin-password",
            },
        )
    assert response.status_code == 303
    cookie = response.headers["set-cookie"]
    assert "reply_admin_session=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "opaque-session-token" in cookie
    assert "admin" not in cookie.split("reply_admin_session=", 1)[1].split(";", 1)[0]
    assert response.headers["location"] == "/admin/system/overview"


async def test_database_user_login_lands_on_workspace(monkeypatch):
    from social_reply.application.account_management import admin

    async def fake_authenticate(_username, _password):
        return _database_principal(), "opaque-session-token"

    monkeypatch.setattr(admin, "authenticate", fake_authenticate)
    async with await _client() as client:
        await client.get("/auth/login")
        csrf = client.cookies["reply_admin_csrf"]
        response = await client.post(
            "/auth/login",
            data={
                "csrf_token": csrf,
                "username": "database-user",
                "password": "password",
            },
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/app"


async def test_web_principal_separates_database_user_and_superadmin_routes(monkeypatch):
    from social_reply.application.account_management import admin

    database_user = _database_principal()
    superadmin = _superadmin_principal()

    async def current_database_user(_request):
        return database_user

    monkeypatch.setattr(admin, "current_principal", current_database_user)
    with pytest.raises(HTTPException, match="admin_required"):
        await admin._web_principal(_request("/admin"))
    with pytest.raises(HTTPException, match="admin_required"):
        await admin._web_principal(_request("/admin/killswitch/toggle"))
    with pytest.raises(HTTPException, match="superadmin_required"):
        await admin._web_principal(_request("/admin/system/health"))

    async def current_superadmin(_request):
        return superadmin

    monkeypatch.setattr(admin, "current_principal", current_superadmin)
    assert await admin._web_principal(_request("/admin/system/health")) == superadmin
    assert await admin._web_principal(_request("/admin/killswitch/toggle")) == superadmin
    assert await admin._web_principal(_request("/admin")) == superadmin


async def test_admin_login_accepts_only_whitelisted_next(monkeypatch):
    from social_reply.application.account_management import admin

    async def fake_authenticate(_username, _password):
        return _database_principal(), "opaque-session-token"

    monkeypatch.setattr(admin, "authenticate", fake_authenticate)
    async with await _client() as client:
        page = await client.get(
            "/auth/login?next=%2Fadmin%2Faccounts%3Fprovider%3Dx%26status%3Dconnected"
        )
        csrf = client.cookies["reply_admin_csrf"]
        assert "SameSite=lax" in page.headers["set-cookie"]
        safe = await client.post(
            "/auth/login",
            data={
                "csrf_token": csrf,
                "username": "admin",
                "password": "password",
                "next": "/admin/accounts?provider=x&status=connected",
            },
        )
        assert safe.headers["location"] == "/admin/accounts?provider=x&status=connected"
        new_page = await client.get(
            "/auth/login?next=%2Fadmin%2Fintegrations%2Faccounts%3Fprovider%3Dx%26status%3Dconnected"
        )
        new_csrf = client.cookies["reply_admin_csrf"]
        assert new_page.status_code == 200
        new_safe = await client.post(
            "/auth/login",
            data={
                "csrf_token": new_csrf,
                "username": "admin",
                "password": "password",
                "next": "/admin/integrations/accounts?provider=x&status=connected",
            },
        )
        assert new_safe.headers["location"] == (
            "/admin/integrations/accounts?provider=x&status=connected"
        )
    async with await _client() as client:
        await client.get("/auth/login")
        csrf = client.cookies["reply_admin_csrf"]
        unsafe = await client.post(
            "/admin/login",
            data={
                "csrf_token": csrf,
                "username": "admin",
                "password": "password",
                "next": "https://evil.example/steal",
            },
        )
        assert unsafe.headers["location"] == "/app"


async def test_admin_login_failure_preserves_whitelisted_next(monkeypatch):
    from social_reply.application.account_management import admin

    async def reject_authentication(_username, _password):
        return None

    monkeypatch.setattr(admin, "authenticate", reject_authentication)
    async with await _client() as client:
        await client.get("/auth/login")
        csrf = client.cookies["reply_admin_csrf"]
        response = await client.post(
            "/auth/login",
            data={
                "csrf_token": csrf,
                "username": "admin",
                "password": "wrong",
                "next": "/admin/accounts?provider=x&status=processing",
            },
        )
    assert response.status_code == 401
    assert (
        'href="/auth/login?next=%2Fadmin%2Faccounts%3Fprovider%3Dx%26status%3Dprocessing"'
        in response.text
    )


async def test_auth_login_preserves_saas_deep_link_and_rejects_external_next(monkeypatch):
    from social_reply.application.account_management import admin

    async def fake_authenticate(_username, _password):
        return _database_principal(role="USER", username="workspace-user"), (
            "opaque-session-token"
        )

    monkeypatch.setattr(admin, "authenticate", fake_authenticate)
    async with await _client() as client:
        await client.get("/auth/login?next=%2Fapp%2Ft%2Fdefault%2Finbox%3Fqueue%3Ddrafts")
        csrf = client.cookies["reply_admin_csrf"]
        safe_response = await client.post(
            "/auth/login",
            data={
                "csrf_token": csrf,
                "username": "admin",
                "password": "password",
                "next": "/app/t/default/inbox?queue=drafts",
            },
        )
        unsafe_response = await client.post(
            "/auth/login",
            data={
                "csrf_token": csrf,
                "username": "admin",
                "password": "password",
                "next": "//evil.example/steal",
            },
        )

    assert safe_response.headers["location"] == "/app/t/default/inbox?queue=drafts"
    assert unsafe_response.headers["location"] == "/app"


async def test_admin_meta_submission_parses_form_once(monkeypatch):
    from social_reply.application.account_management import admin

    captured = {}

    async def fake_submit(command):
        captured["command"] = command
        return __import__("uuid").UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

    principal = _superadmin_principal()

    async def fake_authenticate(_username, _password):
        return principal, "opaque-session-token"

    async def fake_current_principal(_request):
        return principal

    monkeypatch.setattr(admin, "authenticate", fake_authenticate)
    monkeypatch.setattr(admin, "current_principal", fake_current_principal)
    monkeypatch.setattr(admin, "submit_channel_provisioning", fake_submit)
    async with await _client() as client:
        page = await client.get("/admin/login")
        csrf = client.cookies["reply_admin_csrf"]
        assert page.status_code == 200
        await client.post(
            "/admin/login",
            data={
                "csrf_token": csrf,
                "username": "admin",
                "password": "test-admin-password",
            },
        )
        response = await client.post(
            "/admin/connect/meta",
            data={
                "csrf_token": csrf,
                "tenant_id": "default",
                "brand_id": "default",
                "platform": "instagram",
                "external_account_id": "ig-1",
                "access_token": "access",
                "app_secret": "secret",
                "app_id": "app-1",
                "page_id": "page-1",
                "verify_token": "verify",
            },
        )
    assert response.status_code == 303
    command = captured["command"]
    assert command.platform == "instagram"
    assert command.public_values["external_account_id"] == "ig-1"
    assert command.secret_values == {
        "access_token": "access",
        "app_secret": "secret",
        "verify_token": "verify",
    }


async def test_admin_login_rejects_bad_csrf():
    async with await _client() as client:
        response = await client.post(
            "/admin/login",
            data={"csrf_token": "bad", "username": "admin", "password": "x"},
        )
    assert response.status_code == 403


async def test_admin_feishu_submission_preserves_csrf_tenant_and_secret_boundaries(monkeypatch):
    from social_reply.application.account_management import admin, channel_management

    captured = {}
    principal = _superadmin_principal()
    settings = admin.get_settings().model_copy(update={"feishu_enabled": True})

    async def fake_current_principal(_request):
        return principal

    monkeypatch.setattr(admin, "get_settings", lambda: settings)
    monkeypatch.setattr(channel_management, "get_settings", lambda: settings)
    monkeypatch.setattr(admin, "current_principal", fake_current_principal)

    async def fake_submit(command):
        captured["command"] = command
        return __import__("uuid").UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")

    monkeypatch.setattr(admin, "submit_channel_provisioning", fake_submit)
    async with await _client() as client:
        await client.get("/admin/login")
        csrf = client.cookies["reply_admin_csrf"]
        response = await client.post(
            "/admin/connect/feishu",
            data={
                "csrf_token": csrf,
                "tenant_id": "default",
                "brand_id": "default",
                "app_id": "cli_12345678",
                "app_secret": "app-secret",
                "verification_token": "verification-secret",
                "encrypt_key": "encrypt-secret",
                "group_mode": "mentions_only",
                "automation_default": "BOT_DRAFT_ONLY",
            },
        )
    assert response.status_code == 303
    command = captured["command"]
    assert command.tenant_id == "default"
    assert command.public_values["app_id"] == "cli_12345678"
    assert command.public_values["group_mode"] == "mentions_only"
    assert command.public_values["automation_default"] == "BOT_DRAFT_ONLY"
    assert command.secret_values == {
        "app_secret": "app-secret",
        "verification_token": "verification-secret",
        "encrypt_key": "encrypt-secret",
    }


async def test_admin_feishu_rejects_bad_csrf_and_other_tenant(monkeypatch):
    from social_reply.application.account_management import admin

    principal = _superadmin_principal()
    settings = admin.get_settings().model_copy(update={"feishu_enabled": True})

    async def fake_current_principal(_request):
        return principal

    monkeypatch.setattr(admin, "get_settings", lambda: settings)
    monkeypatch.setattr(admin, "current_principal", fake_current_principal)
    payload = {
        "tenant_id": "tenant-b",
        "app_id": "cli_12345678",
        "app_secret": "app-secret",
        "verification_token": "verification-secret",
        "encrypt_key": "encrypt-secret",
        "automation_default": "BOT_DRAFT_ONLY",
    }
    async with await _client() as client:
        bad_csrf = await client.post(
            "/admin/connect/feishu",
            data={"csrf_token": "bad", **payload},
        )
        await client.get("/admin/login")
        csrf = client.cookies["reply_admin_csrf"]
        wrong_tenant = await client.post(
            "/admin/connect/feishu",
            data={"csrf_token": csrf, **payload},
        )
    assert bad_csrf.status_code == 403
    assert wrong_tenant.status_code == 404


async def test_admin_feishu_enforces_gate_and_draft_only(monkeypatch):
    from social_reply.application.account_management import admin, channel_management

    principal = _superadmin_principal()

    async def fake_current_principal(_request):
        return principal

    monkeypatch.setattr(admin, "current_principal", fake_current_principal)
    base = {
        "tenant_id": "default",
        "app_id": "cli_12345678",
        "app_secret": "app-secret",
        "verification_token": "verification-secret",
        "encrypt_key": "encrypt-secret",
    }
    async with await _client() as client:
        await client.get("/admin/login")
        csrf = client.cookies["reply_admin_csrf"]
        disabled = await client.post(
            "/admin/connect/feishu",
            data={"csrf_token": csrf, **base},
        )
        settings = admin.get_settings().model_copy(update={"feishu_enabled": True})
        monkeypatch.setattr(admin, "get_settings", lambda: settings)
        monkeypatch.setattr(channel_management, "get_settings", lambda: settings)
        active = await client.post(
            "/admin/connect/feishu",
            data={
                "csrf_token": csrf,
                **base,
                "automation_default": "BOT_ACTIVE",
            },
        )
        tampered_origin = await client.post(
            "/admin/connect/feishu",
            data={
                "csrf_token": csrf,
                **base,
                "api_base_url": "https://attacker.example",
            },
        )
    assert disabled.status_code == 503
    assert active.status_code == 422
    assert tampered_origin.status_code == 422


async def test_legacy_detail_and_job_gets_redirect_before_business_queries(monkeypatch):
    from social_reply.application.account_management import admin, admin_console

    principal = _superadmin_principal()

    async def fake_current_principal(_request):
        return principal

    def unexpected_session_factory():
        raise AssertionError("legacy GET must redirect before database access")

    monkeypatch.setattr(admin, "current_principal", fake_current_principal)
    monkeypatch.setattr(admin_console, "current_principal", fake_current_principal)
    monkeypatch.setattr(admin, "get_session_factory", unexpected_session_factory)
    monkeypatch.setattr(admin_console, "get_session_factory", unexpected_session_factory)
    conversation_id = uuid.uuid4()
    job_id = uuid.uuid4()

    async with await _client() as client:
        conversation_response = await client.get(
            f"/admin/conversations/{conversation_id}"
        )
        job_response = await client.get(f"/admin/jobs/{job_id}")

    assert conversation_response.status_code == 303
    assert conversation_response.headers["location"] == (
        f"/app/t/default/conversations/{conversation_id}"
    )
    assert job_response.status_code == 303
    assert job_response.headers["location"] == (
        f"/app/t/default/channels/jobs/{job_id}"
    )


async def test_database_user_cannot_retry_legacy_admin_job(monkeypatch):
    from social_reply.application.account_management import admin

    principal = _database_principal(tenant_id="tenant-a")

    async def fake_current_principal(_request):
        return principal

    def unexpected_session_factory():
        raise AssertionError("database user must fail before database access")

    monkeypatch.setattr(admin, "current_principal", fake_current_principal)
    monkeypatch.setattr(admin, "get_session_factory", unexpected_session_factory)

    async with await _client() as client:
        response = await client.post(f"/admin/jobs/{uuid.uuid4()}/retry")

    assert response.status_code == 403
    assert response.json() == {"detail": "admin_required"}


async def test_superadmin_channels_oauth_reaches_csrf_boundary(monkeypatch):
    from social_reply.application.account_management import admin

    principal = _superadmin_principal()

    async def fake_current_principal(_request):
        return principal

    monkeypatch.setattr(admin, "current_principal", fake_current_principal)

    async with await _client() as client:
        responses = [
            await client.post("/app/t/default/channels/oauth/x/start"),
            await client.post("/app/t/default/channels/oauth/meta/start"),
            await client.post("/app/t/default/channels/oauth/instagram/start"),
            await client.post("/app/t/default/channels/oauth/meta/select"),
        ]

    for response in responses:
        assert response.status_code == 403
        assert response.json() == {"detail": "invalid_csrf_token"}


async def test_database_user_cannot_use_legacy_tenant_post_adapters(monkeypatch):
    from social_reply.application.account_management import admin

    principal = _database_principal(tenant_id="tenant-a")

    async def fake_current_principal(_request):
        return principal

    monkeypatch.setattr(admin, "current_principal", fake_current_principal)
    resource_id = uuid.uuid4()

    async with await _client() as client:
        responses = [
            await client.post(f"/admin/work-items/{resource_id}/claim"),
            await client.post(f"/admin/conversations/{resource_id}/reply"),
            await client.post(f"/admin/decisions/{resource_id}/approve"),
            await client.post(f"/admin/delivery/{resource_id}/retry"),
            await client.post("/admin/knowledge/add"),
        ]

    for response in responses:
        assert response.status_code == 403
        assert response.json() == {"detail": "admin_required"}


async def test_superadmin_legacy_draft_adapters_reach_csrf_boundary(monkeypatch):
    from social_reply.application.account_management import admin

    principal = _superadmin_principal()

    async def fake_current_principal(_request):
        return principal

    monkeypatch.setattr(admin, "current_principal", fake_current_principal)
    decision_id = uuid.uuid4()

    async with await _client() as client:
        approve_response = await client.post(
            f"/admin/decisions/{decision_id}/approve"
        )
        discard_response = await client.post(
            f"/admin/decisions/{decision_id}/discard"
        )

    for response in (approve_response, discard_response):
        assert response.status_code == 403
        assert response.json() == {"detail": "invalid_csrf_token"}
