from importlib import import_module
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest

from apps.api.main import create_app
from social_reply.application.account_management.ui_i18n import (
    CATALOGS,
    DEFAULT_LOCALE,
    LOCALE_COOKIE_NAME,
    SUPPORTED_LOCALES,
    get_locale,
    normalize_locale,
    reset_locale,
    set_locale,
    translate,
)
from social_reply.shared.config import Settings


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        testing=True,
        chatwoot_enabled=False,
        x_activity_enabled=False,
        platform_secret_keys="Wm5wbamjBFvTmkGIU2NskIKCrJfsb4AdUBDZR-m1-CM=",
    )


def _test_app():
    app = create_app(_settings())

    @app.get("/_test/locale")
    async def current_locale() -> dict[str, str]:
        return {"locale": get_locale()}

    return app


def test_catalogs_have_symmetric_foundation_keys():
    assert set(CATALOGS) == set(SUPPORTED_LOCALES)

    default_keys = set(CATALOGS[DEFAULT_LOCALE])
    for locale in SUPPORTED_LOCALES:
        assert set(CATALOGS[locale]) == default_keys

    required_prefixes = ("shell.", "nav.", "status.", "button.", "auth.", "empty.")
    for required_prefix in required_prefixes:
        assert any(key.startswith(required_prefix) for key in default_keys)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "zh-CN"),
        ("", "zh-CN"),
        ("unknown", "zh-CN"),
        ("zh-CN", "zh-CN"),
        (" zh-cn ", "zh-CN"),
        ("en", "en"),
        ("EN", "en"),
    ],
)
def test_normalize_locale(value: str | None, expected: str):
    assert normalize_locale(value) == expected


def test_default_locale_and_request_local_translation():
    assert get_locale() == DEFAULT_LOCALE
    assert translate("button.save") == "保存"

    locale_token = set_locale("en")
    try:
        assert get_locale() == "en"
        assert translate("common.welcome", name="Ada") == "Welcome, Ada"
    finally:
        reset_locale(locale_token)

    assert get_locale() == DEFAULT_LOCALE


@pytest.mark.parametrize("cookie_value", [None, "unsupported"])
async def test_missing_or_unknown_cookie_uses_default_locale(cookie_value: str | None):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_test_app()),
        base_url="https://test",
    ) as client:
        if cookie_value is not None:
            client.cookies.set(LOCALE_COOKIE_NAME, cookie_value)
        response = await client.get("/_test/locale")

    assert response.status_code == 200
    assert response.json() == {"locale": DEFAULT_LOCALE}


@pytest.mark.parametrize("method", ["GET", "HEAD"])
async def test_valid_language_switch_preserves_path_and_other_query_parameters(method: str):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_test_app()),
        base_url="https://test",
        follow_redirects=False,
    ) as client:
        response = await client.request(
            method,
            "/_test/locale",
            params=[
                ("view", "open"),
                ("ui_lang", "zh-CN"),
                ("tag", "one"),
                ("ui_lang", "en"),
                ("tag", "two"),
            ],
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/_test/locale?view=open&tag=one&tag=two"
    set_cookie = response.headers["set-cookie"].lower()
    assert f"{LOCALE_COOKIE_NAME}=en" in set_cookie
    assert "httponly" in set_cookie
    assert "samesite=lax" in set_cookie
    assert "path=/" in set_cookie
    assert "secure" in set_cookie


async def test_language_switch_redirect_cannot_target_an_external_origin():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_test_app()),
        base_url="https://test",
        follow_redirects=False,
    ) as client:
        response = await client.get("https://test//evil.example/switch?ui_lang=en")

    redirect_url = urlsplit(response.headers["location"])
    assert response.status_code == 303
    assert redirect_url.scheme == ""
    assert redirect_url.netloc == ""
    assert response.headers["location"] == "/%2Fevil.example/switch"


async def test_invalid_language_is_removed_without_being_persisted():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_test_app()),
        base_url="https://test",
        follow_redirects=False,
    ) as client:
        response = await client.get(
            "/_test/locale",
            params=[("ui_lang", "https://evil.example"), ("view", "all")],
        )
        follow_up = await client.get(response.headers["location"])

    redirect_url = urlsplit(response.headers["location"])
    assert response.status_code == 303
    assert parse_qsl(redirect_url.query, keep_blank_values=True) == [("view", "all")]
    assert "ui_lang" not in redirect_url.query
    assert "set-cookie" not in response.headers
    assert follow_up.json() == {"locale": DEFAULT_LOCALE}


async def test_locale_cookie_applies_to_the_following_request():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_test_app()),
        base_url="https://test",
        follow_redirects=False,
    ) as client:
        switch_response = await client.get("/_test/locale?ui_lang=en")
        localized_response = await client.get(switch_response.headers["location"])

    assert switch_response.status_code == 303
    assert localized_response.status_code == 200
    assert localized_response.json() == {"locale": "en"}
    assert get_locale() == DEFAULT_LOCALE


@pytest.mark.parametrize(
    "callback_path",
    (
        "/admin/oauth/x/callback",
        "/admin/oauth/meta/callback",
        "/admin/oauth/instagram/callback",
    ),
)
async def test_language_redirect_keeps_oauth_callback_security_headers(
    callback_path: str,
):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_test_app()),
        base_url="https://test",
        follow_redirects=False,
    ) as client:
        response = await client.get(f"{callback_path}?ui_lang=en&state=secret")

    assert response.status_code == 303
    assert response.headers["location"] == f"{callback_path}?state=secret"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["referrer-policy"] == "no-referrer"


@pytest.mark.parametrize(
    ("module_name", "callback_path"),
    (
        (
            "social_reply.application.account_management.oauth.meta",
            "/admin/oauth/meta/callback",
        ),
        (
            "social_reply.application.account_management.oauth.instagram",
            "/admin/oauth/instagram/callback",
        ),
    ),
)
async def test_unhandled_oauth_callback_errors_keep_generic_no_store_response(
    monkeypatch,
    module_name: str,
    callback_path: str,
):
    callback_module = import_module(module_name)

    async def fail_to_consume_oauth_state(*_args, **_kwargs):
        raise RuntimeError("simulated oauth state failure")

    monkeypatch.setattr(
        callback_module,
        "take_oauth_state",
        fail_to_consume_oauth_state,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_test_app()),
        base_url="https://test",
        follow_redirects=False,
    ) as client:
        response = await client.get(f"{callback_path}?state=secret-state")

    assert response.status_code == 500
    assert response.text == "OAuth callback failed"
    assert "secret-state" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["referrer-policy"] == "no-referrer"
