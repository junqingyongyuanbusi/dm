import uuid
from datetime import UTC, datetime, timedelta

from social_reply.application.account_management.auth import Principal
from social_reply.application.account_management.oauth import common
from social_reply.application.account_management.ui_i18n import reset_locale, set_locale, translate


def _user_principal() -> Principal:
    return Principal(
        session_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        username="channel-user",
        actor="user:channel-user",
        tenant_id="tenant-a",
        allowed_tenants=frozenset({"tenant-a"}),
        role="USER",
    )


def _superadmin_principal() -> Principal:
    return Principal(
        session_id=uuid.uuid4(),
        username="admin",
        actor="user:admin",
        tenant_id="default",
        allowed_tenants=frozenset({"default"}),
        role="SUPERADMIN",
    )


def test_channels_oauth_context_binds_user_session_and_safe_return_path() -> None:
    principal = _user_principal()

    context = common.build_oauth_context(
        principal=principal,
        provider="instagram",
        tenant_id="tenant-a",
        surface="channels",
        return_to="https://attacker.example/callback",
        extra={"brand_id": "default"},
    )

    assert context["context_version"] == common.OAUTH_CONTEXT_VERSION
    assert context["provider"] == "instagram"
    assert context["tenant_id"] == "tenant-a"
    assert context["initiator_user_id"] == str(principal.user_id)
    assert context["initiator_session_id"] == str(principal.session_id)
    assert context["surface"] == "channels"
    assert context["return_to"] == "/app/t/tenant-a/channels"
    assert context["nonce"]
    assert context["brand_id"] == "default"


def test_channels_oauth_context_supports_superadmin_without_admin_return_path() -> None:
    principal = _superadmin_principal()

    context = common.build_oauth_context(
        principal=principal,
        provider="x",
        tenant_id="default",
        surface="channels",
        return_to="/admin/integrations/accounts",
    )

    assert context["initiator_user_id"] is None
    assert context["initiator_session_id"] == str(principal.session_id)
    assert context["surface"] == "channels"
    assert context["return_to"] == "/app/t/default/channels"


async def test_oauth_context_rejects_expired_or_different_initiator(
    monkeypatch,
) -> None:
    principal = _user_principal()

    async def persisted_principal(_session_id):
        return principal

    monkeypatch.setattr(common, "principal_from_session_id", persisted_principal)
    context = common.build_oauth_context(
        principal=principal,
        provider="x",
        tenant_id="tenant-a",
        surface="channels",
    )

    expired_context = {
        **context,
        "issued_at": (
            datetime.now(UTC) - timedelta(seconds=common.STATE_TTL_SECONDS + 120)
        ).isoformat(),
    }
    different_user_context = {
        **context,
        "initiator_user_id": str(uuid.uuid4()),
    }

    assert await common.principal_from_oauth_context(expired_context) is None
    assert await common.principal_from_oauth_context(different_user_context) is None


async def test_oauth_context_rejects_revoked_session(monkeypatch) -> None:
    principal = _user_principal()

    async def revoked_session(_session_id):
        return None

    monkeypatch.setattr(common, "principal_from_session_id", revoked_session)
    context = common.build_oauth_context(
        principal=principal,
        provider="facebook",
        tenant_id="tenant-a",
        surface="channels",
    )

    assert await common.principal_from_oauth_context(context) is None


async def test_oauth_context_restores_bootstrap_superadmin_session(monkeypatch) -> None:
    principal = _superadmin_principal()

    async def persisted_principal(_session_id):
        return principal

    monkeypatch.setattr(common, "principal_from_session_id", persisted_principal)
    context = common.build_oauth_context(
        principal=principal,
        provider="x",
        tenant_id="default",
        surface="channels",
    )

    assert await common.principal_from_oauth_context(context) == principal


async def test_admin_oauth_context_rejects_database_user_after_role_consolidation(
    monkeypatch,
) -> None:
    principal = _user_principal()

    async def persisted_principal(_session_id):
        return principal

    monkeypatch.setattr(common, "principal_from_session_id", persisted_principal)
    context = common.build_oauth_context(
        principal=principal,
        provider="x",
        tenant_id="tenant-a",
        surface="admin",
    )

    assert await common.principal_from_oauth_context(context) is None


def test_channels_oauth_result_never_honors_external_return_target() -> None:
    response = common.oauth_result_response(
        {
            "surface": "channels",
            "tenant_id": "tenant-a",
            "return_to": "https://attacker.example/callback",
        },
        provider="facebook",
        status_value="error",
        code="access_denied",
    )

    assert response.status_code == 303
    assert response.headers["location"] == (
        "/app/t/tenant-a/channels?provider=facebook&status=error&code=access_denied"
    )
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_channels_return_path_escapes_tenant_path_segments() -> None:
    assert (
        common.safe_oauth_return_to(
            surface="channels",
            tenant_id="tenant/with spaces",
        )
        == "/app/t/tenant%2Fwith%20spaces/channels"
    )


def test_oauth_notice_uses_shared_bilingual_shell() -> None:
    chinese = common.notice(
        translate("oauth.parameters_missing.title"),
        translate("oauth.parameters_missing.retry"),
        status_code=400,
    )

    locale_token = set_locale("en")
    try:
        english = common.notice(
            translate("oauth.parameters_missing.title"),
            translate("oauth.parameters_missing.retry"),
            status_code=400,
        )
    finally:
        reset_locale(locale_token)

    chinese_body = chinese.body.decode()
    english_body = english.body.decode()
    assert chinese.status_code == english.status_code == 400
    assert '<div class="app-shell app-shell-auth"><main id="main-content">' in chinese_body
    assert '<div class="app-shell app-shell-auth"><main id="main-content">' in english_body
    assert 'class="sidebar"' not in chinese_body
    assert 'class="sidebar"' not in english_body
    assert "授权参数不完整" in chinese_body
    assert "Missing authorization parameters" in english_body
    assert "返回平台账号" in chinese_body
    assert "Back to platform accounts" in english_body
    assert "授权参数不完整" not in english_body


def test_oauth_result_redirect_does_not_leak_sensitive_context_values() -> None:
    response = common.oauth_result_response(
        {
            "surface": "admin",
            "tenant_id": "tenant-a",
            "return_to": "/admin/integrations/accounts",
            "request_token_secret": "do-not-leak-request-secret",
            "access_token": "do-not-leak-access-token",
        },
        provider="x",
        status_value="error",
        code="oauth_state_missing",
    )

    serialized_headers = repr(dict(response.headers))
    assert response.status_code == 303
    assert response.headers["location"] == (
        "/admin/integrations/accounts?provider=x&status=error&code=oauth_state_missing"
    )
    assert "do-not-leak-request-secret" not in serialized_headers
    assert "do-not-leak-access-token" not in serialized_headers
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["referrer-policy"] == "no-referrer"
