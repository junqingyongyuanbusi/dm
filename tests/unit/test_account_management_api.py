import uuid

import httpx
import pytest

from apps.api.main import create_app
from social_reply.application.account_management import router as account_router


async def _client():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://test",
    )


async def test_account_api_requires_control_api_key():
    async with await _client() as client:
        response = await client.post(
            "/api/v1/platform-accounts/telegram",
            json={"token": "123:token"},
        )
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid_control_api_key"


async def test_telegram_account_api_submits_durable_job(monkeypatch):
    captured = {}

    async def fake_submit(**kwargs):
        captured.update(kwargs)
        return uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

    async def fake_enqueue(_job_id):
        return None

    monkeypatch.setattr(account_router, "submit_provisioning_job", fake_submit)
    monkeypatch.setattr(account_router, "_enqueue", fake_enqueue)
    async with await _client() as client:
        response = await client.post(
            "/api/v1/platform-accounts/telegram",
            headers={"Authorization": "Bearer test-control-key"},
            json={"token": "123:token", "brand_id": "brand-a"},
        )
    assert response.status_code == 202
    assert response.json() == {
        "job_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "status": "PENDING",
        "status_url": "/api/v1/platform-accounts/jobs/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    }
    assert captured["platform"] == "telegram"
    assert captured["brand_id"] == "brand-a"
    assert captured["secrets"] == {"token": "123:token"}
    assert "token" not in captured["request"]


async def test_messenger_account_api_defaults_to_dm_only_draft_mode(monkeypatch):
    captured = {}

    async def fake_submit(**kwargs):
        captured.update(kwargs)
        return uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

    async def fake_enqueue(_job_id):
        return None

    monkeypatch.setattr(account_router, "submit_provisioning_job", fake_submit)
    monkeypatch.setattr(account_router, "_enqueue", fake_enqueue)
    async with await _client() as client:
        response = await client.post(
            "/api/v1/platform-accounts/meta",
            headers={"Authorization": "Bearer test-control-key"},
            json={
                "platform": "facebook",
                "external_account_id": "page-1",
                "access_token": "page-token",
                "app_secret": "app-secret",
                "app_id": "app-1",
                "verify_token": "verify",
            },
        )
    assert response.status_code == 202
    assert captured["platform"] == "facebook"
    assert captured["request"]["enable_dm"] is True
    assert captured["request"]["enable_comments"] is False
    assert captured["request"]["automation_default"] == "BOT_DRAFT_ONLY"


async def test_messenger_account_api_keeps_draft_mode_when_comments_enabled(monkeypatch):
    captured = {}
    settings = account_router.get_settings().model_copy(
        update={"meta_comment_reply_enabled": True, "meta_auto_reply_enabled": True}
    )
    monkeypatch.setattr(account_router, "get_settings", lambda: settings)

    async def fake_submit(**kwargs):
        captured.update(kwargs)
        return uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

    async def fake_enqueue(_job_id):
        return None

    monkeypatch.setattr(account_router, "submit_provisioning_job", fake_submit)
    monkeypatch.setattr(account_router, "_enqueue", fake_enqueue)
    async with await _client() as client:
        response = await client.post(
            "/api/v1/platform-accounts/meta",
            headers={"Authorization": "Bearer test-control-key"},
            json={
                "platform": "facebook",
                "external_account_id": "page-1",
                "access_token": "page-token",
                "app_secret": "app-secret",
                "app_id": "app-1",
                "verify_token": "verify",
            },
        )
    assert response.status_code == 202
    assert captured["request"]["enable_comments"] is True
    assert captured["request"]["automation_default"] == "BOT_DRAFT_ONLY"


async def test_meta_account_api_rejects_missing_app_identity():
    async with await _client() as client:
        response = await client.post(
            "/api/v1/platform-accounts/meta",
            headers={"X-Control-Api-Key": "test-control-key"},
            json={
                "platform": "instagram",
                "external_account_id": "ig-1",
                "access_token": "token",
                "app_secret": "secret",
            },
        )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "payload",
    [
        {
            "platform": "facebook",
            "external_account_id": "page-1",
            "access_token": "token",
            "app_secret": "secret",
            "app_id": "app-1",
            "verify_token": "verify",
            "instagram_login_mode": "instagram_login",
        },
        {
            "platform": "instagram",
            "external_account_id": "ig-1",
            "access_token": "token",
            "app_secret": "secret",
            "app_id": "app-1",
            "verify_token": "verify",
            "instagram_login_mode": "facebook_login",
        },
        {
            "platform": "instagram",
            "external_account_id": "ig-1",
            "access_token": "token",
            "app_secret": "secret",
            "app_id": "app-1",
            "verify_token": "verify",
            "instagram_login_mode": "instagram_login",
            "page_id": "page-1",
        },
    ],
)
async def test_meta_account_api_rejects_mixed_login_paths(payload):
    async with await _client() as client:
        response = await client.post(
            "/api/v1/platform-accounts/meta",
            headers={"Authorization": "Bearer test-control-key"},
            json=payload,
        )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "overrides",
    [
        {"enable_dm": False},
        {"enable_comments": True},
        {"automation_default": "BOT_ACTIVE"},
    ],
)
async def test_meta_account_api_rejects_out_of_scope_launch_policy(overrides):
    payload = {
        "platform": "facebook",
        "external_account_id": "page-1",
        "access_token": "token",
        "app_secret": "secret",
        "app_id": "app-1",
        "verify_token": "verify",
        **overrides,
    }
    async with await _client() as client:
        response = await client.post(
            "/api/v1/platform-accounts/meta",
            headers={"Authorization": "Bearer test-control-key"},
            json=payload,
        )
    assert response.status_code == 422


async def test_instagram_account_api_keeps_draft_mode_when_comments_enabled(monkeypatch):
    captured = {}
    settings = account_router.get_settings().model_copy(
        update={"meta_comment_reply_enabled": True, "meta_auto_reply_enabled": True}
    )
    monkeypatch.setattr(account_router, "get_settings", lambda: settings)

    async def fake_submit(**kwargs):
        captured.update(kwargs)
        return uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

    async def fake_enqueue(_job_id):
        return None

    monkeypatch.setattr(account_router, "submit_provisioning_job", fake_submit)
    monkeypatch.setattr(account_router, "_enqueue", fake_enqueue)
    async with await _client() as client:
        response = await client.post(
            "/api/v1/platform-accounts/meta",
            headers={"Authorization": "Bearer test-control-key"},
            json={
                "platform": "instagram",
                "external_account_id": "ig-1",
                "access_token": "token",
                "app_secret": "secret",
                "app_id": "app-1",
                "verify_token": "verify",
                "page_id": "page-1",
            },
        )
    assert response.status_code == 202
    assert captured["request"]["enable_comments"] is True
    assert captured["request"]["automation_default"] == "BOT_DRAFT_ONLY"


async def test_x_account_api_rejects_disabled_features(monkeypatch):
    settings = account_router.get_settings().model_copy(
        update={
            "x_legacy_dm_enabled": False,
            "x_activity_enabled": False,
            "xchat_enabled": False,
        }
    )
    monkeypatch.setattr(account_router, "get_settings", lambda: settings)
    async with await _client() as client:
        response = await client.post(
            "/api/v1/platform-accounts/x",
            headers={"Authorization": "Bearer test-control-key"},
            json={
                "consumer_key": "ck",
                "consumer_secret": "cs",
                "access_token": "at",
                "access_token_secret": "ats",
                "environment": "oauth",
            },
        )
    assert response.status_code == 503
    assert response.json()["detail"] == "x_integration_disabled"


async def test_x_account_api_rejects_pin_when_xchat_disabled(monkeypatch):
    settings = account_router.get_settings().model_copy(
        update={
            "x_legacy_dm_enabled": True,
            "x_activity_enabled": True,
            "xchat_enabled": False,
        }
    )
    monkeypatch.setattr(account_router, "get_settings", lambda: settings)
    async with await _client() as client:
        response = await client.post(
            "/api/v1/platform-accounts/x",
            headers={"Authorization": "Bearer test-control-key"},
            json={
                "consumer_key": "ck",
                "consumer_secret": "cs",
                "access_token": "at",
                "access_token_secret": "ats",
                "environment": "oauth",
                "xchat_pin": "1234",
            },
        )
    assert response.status_code == 422
    assert response.json()["detail"] == "xchat_disabled"


@pytest.mark.parametrize(
    ("endpoint", "settings_update", "payload", "detail"),
    [
        (
            "/api/v1/platform-accounts/meta",
            {"facebook_messenger_enabled": False},
            {
                "platform": "facebook",
                "external_account_id": "page-1",
                "access_token": "token",
                "app_secret": "secret",
                "app_id": "app-1",
                "verify_token": "verify",
            },
            "facebook_integration_disabled",
        ),
        (
            "/api/v1/platform-accounts/meta",
            {"instagram_messaging_enabled": False},
            {
                "platform": "instagram",
                "external_account_id": "ig-1",
                "access_token": "token",
                "app_secret": "secret",
                "app_id": "app-1",
                "verify_token": "verify",
                "page_id": "page-1",
            },
            "instagram_integration_disabled",
        ),
        (
            "/api/v1/platform-accounts/whatsapp",
            {"whatsapp_enabled": False},
            {
                "external_account_id": "phone-1",
                "access_token": "token",
                "app_secret": "secret",
                "app_id": "app-1",
                "verify_token": "verify",
            },
            "whatsapp_integration_disabled",
        ),
    ],
)
async def test_future_platform_account_api_rejects_disabled_integrations(
    monkeypatch, endpoint, settings_update, payload, detail
):
    settings = account_router.get_settings().model_copy(update=settings_update)
    monkeypatch.setattr(account_router, "get_settings", lambda: settings)

    async def unexpected_submit(**_kwargs):
        raise AssertionError("disabled platform must not submit a provisioning job")

    monkeypatch.setattr(account_router, "submit_provisioning_job", unexpected_submit)
    async with await _client() as client:
        response = await client.post(
            endpoint,
            headers={"Authorization": "Bearer test-control-key"},
            json=payload,
        )
    assert response.status_code == 503
    assert response.json()["detail"] == detail


async def test_whatsapp_account_api_submits_phone_number_job(monkeypatch):
    captured = {}

    async def fake_submit(**kwargs):
        captured.update(kwargs)
        return uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

    async def fake_enqueue(_job_id):
        return None

    monkeypatch.setattr(account_router, "submit_provisioning_job", fake_submit)
    monkeypatch.setattr(account_router, "_enqueue", fake_enqueue)
    async with await _client() as client:
        response = await client.post(
            "/api/v1/platform-accounts/whatsapp",
            headers={"Authorization": "Bearer test-control-key"},
            json={
                "external_account_id": "phone-1",
                "access_token": "token",
                "app_secret": "secret",
                "app_id": "app-1",
                "verify_token": "verify",
            },
        )
    assert response.status_code == 202
    assert captured["platform"] == "whatsapp"
    assert captured["request"]["external_account_id"] == "phone-1"
    assert captured["secrets"] == {
        "access_token": "token",
        "app_secret": "secret",
        "verify_token": "verify",
    }


async def test_meta_account_api_rejects_bot_active_while_switch_is_off():
    async with await _client() as client:
        response = await client.post(
            "/api/v1/platform-accounts/meta",
            headers={"Authorization": "Bearer test-control-key"},
            json={
                "platform": "facebook",
                "external_account_id": "page-1",
                "access_token": "page-token",
                "app_secret": "app-secret",
                "app_id": "app-1",
                "verify_token": "verify",
                "automation_default": "BOT_ACTIVE",
            },
        )
    assert response.status_code == 422
    assert "BOT_DRAFT_ONLY" in response.text


async def test_meta_account_api_still_rejects_bot_active_once_deployment_opts_in(monkeypatch):
    settings = account_router.get_settings().model_copy(update={"meta_auto_reply_enabled": True})
    monkeypatch.setattr(account_router, "get_settings", lambda: settings)
    async with await _client() as client:
        response = await client.post(
            "/api/v1/platform-accounts/meta",
            headers={"Authorization": "Bearer test-control-key"},
            json={
                "platform": "facebook",
                "external_account_id": "page-1",
                "access_token": "page-token",
                "app_secret": "app-secret",
                "app_id": "app-1",
                "verify_token": "verify",
                "automation_default": "BOT_ACTIVE",
            },
        )
    assert response.status_code == 422
    assert "BOT_DRAFT_ONLY" in response.text


async def test_feishu_account_api_splits_public_and_secret_fields(monkeypatch):
    captured = {}
    settings = account_router.get_settings().model_copy(update={"feishu_enabled": True})
    monkeypatch.setattr(account_router, "get_settings", lambda: settings)

    async def fake_submit(**kwargs):
        captured.update(kwargs)
        return uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")

    async def fake_enqueue(_job_id):
        return None

    monkeypatch.setattr(account_router, "submit_provisioning_job", fake_submit)
    monkeypatch.setattr(account_router, "_enqueue", fake_enqueue)
    async with await _client() as client:
        response = await client.post(
            "/api/v1/platform-accounts/feishu",
            headers={"Authorization": "Bearer test-control-key"},
            json={
                "app_id": "cli_12345678",
                "app_secret": "app-secret",
                "verification_token": "verification-secret",
                "encrypt_key": "encrypt-secret",
            },
        )

    assert response.status_code == 202
    assert captured["platform"] == "feishu"
    assert captured["request"] == {
        "automation_default": "BOT_DRAFT_ONLY",
        "app_id": "cli_12345678",
        "api_base_url": "https://open.feishu.cn",
        "group_mode": "mentions_only",
    }
    assert captured["secrets"] == {
        "app_secret": "app-secret",
        "verification_token": "verification-secret",
        "encrypt_key": "encrypt-secret",
    }
    assert not set(captured["secrets"]) & set(captured["request"])


async def test_feishu_account_api_enforces_gate_before_submission(monkeypatch):
    settings = account_router.get_settings().model_copy(update={"feishu_enabled": False})
    monkeypatch.setattr(account_router, "get_settings", lambda: settings)

    async def unexpected_submit(**_kwargs):
        raise AssertionError("disabled Feishu must not submit")

    monkeypatch.setattr(account_router, "submit_provisioning_job", unexpected_submit)
    async with await _client() as client:
        response = await client.post(
            "/api/v1/platform-accounts/feishu",
            headers={"Authorization": "Bearer test-control-key"},
            json={
                "app_id": "cli_12345678",
                "app_secret": "app-secret",
                "verification_token": "verification-secret",
                "encrypt_key": "encrypt-secret",
            },
        )
    assert response.status_code == 503
    assert response.json()["detail"] == "feishu_integration_disabled"


@pytest.mark.parametrize(
    "overrides",
    [
        {"app_id": "bad-app-id"},
        {"app_secret": "   "},
        {"verification_token": ""},
        {"encrypt_key": "   "},
        {"automation_default": "BOT_ACTIVE"},
        {"unexpected": "field"},
    ],
)
async def test_feishu_account_api_rejects_invalid_or_extra_fields(monkeypatch, overrides):
    settings = account_router.get_settings().model_copy(update={"feishu_enabled": True})
    monkeypatch.setattr(account_router, "get_settings", lambda: settings)
    payload = {
        "app_id": "cli_12345678",
        "app_secret": "app-secret",
        "verification_token": "verification-secret",
        "encrypt_key": "encrypt-secret",
        **overrides,
    }
    async with await _client() as client:
        response = await client.post(
            "/api/v1/platform-accounts/feishu",
            headers={"Authorization": "Bearer test-control-key"},
            json=payload,
        )
    assert response.status_code == 422


async def test_email_account_api_splits_canonical_public_fields_and_preserves_secrets(monkeypatch):
    captured = {}
    settings = account_router.get_settings().model_copy(update={"email_enabled": True})
    monkeypatch.setattr(account_router, "get_settings", lambda: settings)

    async def fake_submit(**kwargs):
        captured.update(kwargs)
        return uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")

    async def fake_enqueue(_job_id):
        return None

    monkeypatch.setattr(account_router, "submit_provisioning_job", fake_submit)
    monkeypatch.setattr(account_router, "_enqueue", fake_enqueue)
    async with await _client() as client:
        response = await client.post(
            "/api/v1/platform-accounts/email",
            headers={"Authorization": "Bearer test-control-key"},
            json={
                "tenant_id": "tenant-a",
                "brand_id": "brand-a",
                "email_address": " Support@Example.COM. ",
                "username": "  mail-user  ",
                "password": "  mail-password  ",
                "imap_host": " IMAP.LARKSUITE.COM. ",
                "smtp_host": " SMTP.LARKSUITE.COM. ",
                "smtp_security": "starttls",
                "smtp_port": 587,
                "from_name": "Support",
            },
        )

    assert response.status_code == 202
    assert captured["platform"] == "email"
    assert captured["request"] == {
        "automation_default": "BOT_DRAFT_ONLY",
        "email_address": "Support@example.com",
        "imap_host": "imap.larksuite.com",
        "imap_port": 993,
        "mailbox": "INBOX",
        "smtp_host": "smtp.larksuite.com",
        "smtp_port": 587,
        "smtp_security": "starttls",
        "from_name": "Support",
        "internal_domain_policy": "ignore",
    }
    assert captured["secrets"] == {
        "username": "  mail-user  ",
        "password": "  mail-password  ",
    }
    assert not set(captured["request"]) & {"username", "password"}


@pytest.mark.parametrize(
    ("smtp_security", "smtp_port", "expected_port"),
    [
        ("ssl", None, 465),
        ("starttls", None, 587),
        ("starttls", 2525, 2525),
    ],
)
async def test_email_account_api_defaults_smtp_port_from_security_and_preserves_explicit_port(
    monkeypatch, smtp_security, smtp_port, expected_port
):
    captured = {}
    settings = account_router.get_settings().model_copy(update={"email_enabled": True})
    monkeypatch.setattr(account_router, "get_settings", lambda: settings)

    async def fake_submit(**kwargs):
        captured.update(kwargs)
        return uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")

    async def fake_enqueue(_job_id):
        return None

    monkeypatch.setattr(account_router, "submit_provisioning_job", fake_submit)
    monkeypatch.setattr(account_router, "_enqueue", fake_enqueue)
    payload = {
        "email_address": "support@example.com",
        "username": "mail-user",
        "password": "mail-password",
        "imap_host": "imap.larksuite.com",
        "smtp_host": "smtp.larksuite.com",
        "smtp_security": smtp_security,
    }
    if smtp_port is not None:
        payload["smtp_port"] = smtp_port

    async with await _client() as client:
        response = await client.post(
            "/api/v1/platform-accounts/email",
            headers={"Authorization": "Bearer test-control-key"},
            json=payload,
        )

    assert response.status_code == 202
    assert captured["request"]["smtp_port"] == expected_port
    assert type(captured["request"]["smtp_port"]) is int


@pytest.mark.parametrize(
    "overrides",
    [
        {"username": ""},
        {"password": ""},
        {"automation_default": "BOT_ACTIVE"},
        {"brand_id": "invalid brand"},
        {"password": "p" * 513},
    ],
)
async def test_email_account_api_422_never_echoes_credentials(monkeypatch, overrides):
    username_marker = "MARKED-EMAIL-USERNAME"
    password_marker = "MARKED-EMAIL-PASSWORD"
    settings = account_router.get_settings().model_copy(update={"email_enabled": True})
    monkeypatch.setattr(account_router, "get_settings", lambda: settings)
    payload = {
        "email_address": "support@example.com",
        "username": username_marker,
        "password": password_marker,
        "imap_host": "imap.larksuite.com",
        "smtp_host": "smtp.larksuite.com",
        **overrides,
    }
    async with await _client() as client:
        response = await client.post(
            "/api/v1/platform-accounts/email",
            headers={"Authorization": "Bearer test-control-key"},
            json=payload,
        )

    assert response.status_code == 422
    assert username_marker not in response.text
    assert password_marker not in response.text


async def test_email_account_api_allowlist_runs_after_auth_tenant_and_feature_gate(
    monkeypatch,
):
    base = account_router.get_settings().model_copy(
        update={
            "email_enabled": False,
            "email_allowed_hosts": frozenset({"smtp.larksuite.com"}),
        }
    )
    monkeypatch.setattr(account_router, "get_settings", lambda: base)

    async def unexpected_submit(**_kwargs):
        raise AssertionError("rejected Email request must not create a job")

    monkeypatch.setattr(account_router, "submit_provisioning_job", unexpected_submit)
    payload = {
        "email_address": "support@example.com",
        "username": "mail-user",
        "password": "mail-password",
        "imap_host": "imap.larksuite.com",
        "smtp_host": "smtp.larksuite.com",
    }
    async with await _client() as client:
        unauthenticated = await client.post("/api/v1/platform-accounts/email", json=payload)
        forbidden = await client.post(
            "/api/v1/platform-accounts/email",
            headers={"Authorization": "Bearer test-control-key"},
            json={**payload, "tenant_id": "forbidden"},
        )
        disabled = await client.post(
            "/api/v1/platform-accounts/email",
            headers={"Authorization": "Bearer test-control-key"},
            json=payload,
        )
        monkeypatch.setattr(
            account_router,
            "get_settings",
            lambda: base.model_copy(update={"email_enabled": True}),
        )
        disallowed = await client.post(
            "/api/v1/platform-accounts/email",
            headers={"Authorization": "Bearer test-control-key"},
            json=payload,
        )

    assert unauthenticated.status_code == 401
    assert forbidden.status_code == 403
    assert forbidden.json()["detail"] == "tenant_access_denied"
    assert disabled.status_code == 503
    assert disabled.json()["detail"] == "email_integration_disabled"
    assert disallowed.status_code == 422
    assert disallowed.json()["detail"] == "email_hostname_not_allowed"


async def test_email_account_api_returns_disabled_after_tenant_authorization(monkeypatch):
    settings = account_router.get_settings().model_copy(update={"email_enabled": False})
    monkeypatch.setattr(account_router, "get_settings", lambda: settings)
    payload = {
        "email_address": "support@example.com",
        "username": "mail-user",
        "password": "mail-password",
        "imap_host": "imap.larksuite.com",
        "smtp_host": "smtp.larksuite.com",
    }
    async with await _client() as client:
        unauthorized = await client.post(
            "/api/v1/platform-accounts/email",
            json=payload,
        )
        forbidden = await client.post(
            "/api/v1/platform-accounts/email",
            headers={"Authorization": "Bearer test-control-key"},
            json={**payload, "tenant_id": "forbidden"},
        )
        disabled = await client.post(
            "/api/v1/platform-accounts/email",
            headers={"Authorization": "Bearer test-control-key"},
            json=payload,
        )

    assert unauthorized.status_code == 401
    assert forbidden.status_code == 403
    assert forbidden.json()["detail"] == "tenant_access_denied"
    assert disabled.status_code == 503
    assert disabled.json()["detail"] == "email_integration_disabled"


@pytest.mark.parametrize(
    "overrides",
    [
        {"email_address": "not-an-email"},
        {"username": "   "},
        {"password": "   "},
        {"imap_host": "127.0.0.1"},
        {"imap_port": 0},
        {"mailbox": "INBOX\nInjected"},
        {"smtp_host": "bad_host"},
        {"smtp_port": 65536},
        {"smtp_security": "plain"},
        {"from_name": "Support\r\nBcc: victim@example.com"},
        {"from_name": "Support\x85Injected"},
        {"internal_domain_policy": "deny"},
        {"automation_default": "BOT_ACTIVE"},
        {"unexpected": "field"},
    ],
)
async def test_email_account_api_rejects_invalid_or_extra_fields(monkeypatch, overrides):
    settings = account_router.get_settings().model_copy(update={"email_enabled": True})
    monkeypatch.setattr(account_router, "get_settings", lambda: settings)
    payload = {
        "email_address": "support@example.com",
        "username": "mail-user",
        "password": "mail-password",
        "imap_host": "imap.larksuite.com",
        "smtp_host": "smtp.larksuite.com",
        **overrides,
    }
    async with await _client() as client:
        response = await client.post(
            "/api/v1/platform-accounts/email",
            headers={"Authorization": "Bearer test-control-key"},
            json=payload,
        )
    assert response.status_code == 422


async def test_email_account_api_typo_and_extra_fields_return_generic_redacted_error(monkeypatch):
    settings = account_router.get_settings().model_copy(update={"email_enabled": True})
    monkeypatch.setattr(account_router, "get_settings", lambda: settings)
    secret = "DO-NOT-ECHO-THIS-PASSWORD"
    payload = {
        "email_address": "support@example.com",
        "username": "mail-user",
        "passwrod": secret,
        "imap_host": "imap.larksuite.com",
        "smtp_host": "smtp.larksuite.com",
        "unexpected": "field",
    }
    async with await _client() as client:
        response = await client.post(
            "/api/v1/platform-accounts/email",
            headers={"Authorization": "Bearer test-control-key"},
            json=payload,
        )

    assert response.status_code == 422
    assert response.json() == {"detail": "invalid_email_account_request"}
    assert secret not in response.text


async def test_email_account_api_authentication_precedes_invalid_json_parsing():
    async with await _client() as client:
        response = await client.post(
            "/api/v1/platform-accounts/email",
            content=b'{"password":"SECRET",',
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 401
    assert response.json()["detail"] == "invalid_control_api_key"
    assert "SECRET" not in response.text


async def test_feishu_account_api_denies_other_tenant_even_when_disabled(monkeypatch):
    settings = account_router.get_settings().model_copy(update={"feishu_enabled": False})
    monkeypatch.setattr(account_router, "get_settings", lambda: settings)
    async with await _client() as client:
        response = await client.post(
            "/api/v1/platform-accounts/feishu",
            headers={"Authorization": "Bearer test-control-key"},
            json={
                "tenant_id": "forbidden",
                "app_id": "cli_12345678",
                "app_secret": "app-secret",
                "verification_token": "verification-secret",
                "encrypt_key": "encrypt-secret",
            },
        )
    assert response.status_code == 403
    assert response.json()["detail"] == "tenant_access_denied"
