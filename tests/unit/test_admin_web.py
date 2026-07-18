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


async def test_admin_login_sets_http_only_session_cookie():
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
    assert "SameSite=strict" in cookie


async def test_admin_meta_submission_parses_form_once(monkeypatch):
    from social_reply.application.account_management import admin

    captured = {}

    async def fake_submit(**kwargs):
        captured.update(kwargs)
        return __import__("uuid").UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

    async def fake_process(_job_id):
        return "COMPLETED"

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
