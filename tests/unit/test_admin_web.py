import httpx

from apps.api.main import create_app


async def _client():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://test",
        follow_redirects=False,
    )


async def test_admin_dashboard_redirects_to_login():
    async with await _client() as client:
        response = await client.get("/admin")
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


async def test_admin_login_sets_http_only_session_cookie(monkeypatch):
    from social_reply.application.account_management import admin
    from social_reply.application.account_management.auth import Principal

    async def fake_authenticate(username, password):
        assert username == "admin"
        assert password == "test-admin-password"
        return (
            Principal(
                session_id=__import__("uuid").uuid4(),
                username="admin",
                actor="user:admin",
                allowed_tenants=frozenset({"default"}),
            ),
            "opaque-session-token",
        )

    monkeypatch.setattr(admin, "authenticate", fake_authenticate)
    async with await _client() as client:
        page = await client.get("/admin/login")
        csrf = client.cookies["reply_admin_csrf"]
        assert page.status_code == 200
        response = await client.post(
            "/admin/login",
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


async def test_admin_login_accepts_only_whitelisted_next(monkeypatch):
    from social_reply.application.account_management import admin
    from social_reply.application.account_management.auth import Principal

    async def fake_authenticate(_username, _password):
        return (
            Principal(
                session_id=__import__("uuid").uuid4(),
                username="admin",
                actor="user:admin",
                allowed_tenants=frozenset({"default"}),
            ),
            "opaque-session-token",
        )

    monkeypatch.setattr(admin, "authenticate", fake_authenticate)
    async with await _client() as client:
        page = await client.get(
            "/admin/login?next=%2Fadmin%2Faccounts%3Fprovider%3Dx%26status%3Dconnected"
        )
        csrf = client.cookies["reply_admin_csrf"]
        assert "SameSite=lax" in page.headers["set-cookie"]
        safe = await client.post(
            "/admin/login",
            data={
                "csrf_token": csrf,
                "username": "admin",
                "password": "password",
                "next": "/admin/accounts?provider=x&status=connected",
            },
        )
        assert safe.headers["location"] == "/admin/accounts?provider=x&status=connected"
        new_page = await client.get(
            "/admin/login?next=%2Fadmin%2Fintegrations%2Faccounts%3Fprovider%3Dx%26status%3Dconnected"
        )
        new_csrf = client.cookies["reply_admin_csrf"]
        assert new_page.status_code == 200
        new_safe = await client.post(
            "/admin/login",
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
        await client.get("/admin/login")
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
        assert unsafe.headers["location"] == "/admin"


async def test_admin_login_failure_preserves_whitelisted_next(monkeypatch):
    from social_reply.application.account_management import admin

    async def reject_authentication(_username, _password):
        return None

    monkeypatch.setattr(admin, "authenticate", reject_authentication)
    async with await _client() as client:
        await client.get("/admin/login")
        csrf = client.cookies["reply_admin_csrf"]
        response = await client.post(
            "/admin/login",
            data={
                "csrf_token": csrf,
                "username": "admin",
                "password": "wrong",
                "next": "/admin/accounts?provider=x&status=processing",
            },
        )
    assert response.status_code == 401
    assert (
        'href="/admin/login?next=%2Fadmin%2Faccounts%3Fprovider%3Dx%26status%3Dprocessing"'
        in response.text
    )


async def test_admin_meta_submission_parses_form_once(monkeypatch):
    from social_reply.application.account_management import admin
    from social_reply.application.account_management.auth import Principal

    captured = {}

    async def fake_submit(**kwargs):
        captured.update(kwargs)
        return __import__("uuid").UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

    async def fake_process(_job_id):
        return "COMPLETED"

    principal = Principal(
        session_id=__import__("uuid").uuid4(),
        username="admin",
        actor="user:admin",
        allowed_tenants=frozenset({"default"}),
    )

    async def fake_authenticate(_username, _password):
        return principal, "opaque-session-token"

    async def fake_current_principal(_request):
        return principal

    monkeypatch.setattr(admin, "authenticate", fake_authenticate)
    monkeypatch.setattr(admin, "current_principal", fake_current_principal)
    monkeypatch.setattr(admin, "submit_provisioning_job", fake_submit)
    monkeypatch.setattr(
        "social_reply.application.account_management.jobs.process_provisioning_job",
        fake_process,
    )
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
                "verify_token": "verify",
            },
        )
    assert response.status_code == 303
    assert captured["platform"] == "instagram"
    assert captured["request"]["external_account_id"] == "ig-1"
    assert captured["secrets"] == {
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
    from social_reply.application.account_management import admin
    from social_reply.application.account_management.auth import Principal

    captured = {}
    principal = Principal(
        session_id=__import__("uuid").uuid4(),
        username="tenant-admin",
        actor="user:tenant-admin",
        allowed_tenants=frozenset({"tenant-a"}),
    )
    settings = admin.get_settings().model_copy(update={"feishu_enabled": True})

    async def fake_current_principal(_request):
        return principal

    monkeypatch.setattr(admin, "get_settings", lambda: settings)
    monkeypatch.setattr(admin, "current_principal", fake_current_principal)

    async def fake_submit(**kwargs):
        captured.update(kwargs)
        return __import__("uuid").UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")

    async def fake_dispatch(*_args, **_kwargs):
        return None

    monkeypatch.setattr(admin, "submit_provisioning_job", fake_submit)
    monkeypatch.setattr(admin, "dispatch_actor", fake_dispatch)
    async with await _client() as client:
        await client.get("/admin/login")
        csrf = client.cookies["reply_admin_csrf"]
        response = await client.post(
            "/admin/connect/feishu",
            data={
                "csrf_token": csrf,
                "tenant_id": "tenant-a",
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
    assert captured["tenant_id"] == "tenant-a"
    assert captured["request"]["app_id"] == "cli_12345678"
    assert captured["request"]["group_mode"] == "mentions_only"
    assert captured["request"]["automation_default"] == "BOT_DRAFT_ONLY"
    assert captured["secrets"] == {
        "app_secret": "app-secret",
        "verification_token": "verification-secret",
        "encrypt_key": "encrypt-secret",
    }


async def test_admin_feishu_rejects_bad_csrf_and_other_tenant(monkeypatch):
    from social_reply.application.account_management import admin
    from social_reply.application.account_management.auth import Principal

    principal = Principal(
        session_id=__import__("uuid").uuid4(),
        username="tenant-admin",
        actor="user:tenant-admin",
        allowed_tenants=frozenset({"tenant-a"}),
    )
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
    assert wrong_tenant.status_code == 403


async def test_admin_feishu_enforces_gate_and_draft_only(monkeypatch):
    from social_reply.application.account_management import admin
    from social_reply.application.account_management.auth import Principal

    principal = Principal(
        session_id=__import__("uuid").uuid4(),
        username="tenant-admin",
        actor="user:tenant-admin",
        allowed_tenants=frozenset({"tenant-a"}),
    )

    async def fake_current_principal(_request):
        return principal

    monkeypatch.setattr(admin, "current_principal", fake_current_principal)
    base = {
        "tenant_id": "tenant-a",
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
