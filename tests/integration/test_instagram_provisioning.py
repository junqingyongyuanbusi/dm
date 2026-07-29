import uuid

import httpx
import pytest

from social_reply.application.account_management import service
from social_reply.application.account_management.service import connect_meta_account
from social_reply.connectors.meta.client import appsecret_proof
from social_reply.infrastructure.database import models

pytestmark = pytest.mark.integration


async def test_facebook_login_instagram_uses_page_subscription_path(
    migrated_db,
    session,
    tmp_path,
):
    requests: list[httpx.Request] = []
    app_id = f"fb-app-{uuid.uuid4().hex}"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path.endswith("/ig-1"):
            return httpx.Response(200, json={"id": "ig-1", "name": "Shop"})
        if request.url.path.endswith("/subscriptions"):
            if request.method == "GET":
                return httpx.Response(200, json={"data": []})
            return httpx.Response(200, json={"success": True})
        if request.method == "POST" and request.url.path.endswith("/subscribed_apps"):
            return httpx.Response(200, json={"success": True})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    result = await connect_meta_account(
        platform="instagram",
        external_account_id="ig-1",
        access_token="page-token",
        app_secret="facebook-app-secret",
        app_id=app_id,
        app_public_id=f"meta_ig_{uuid.uuid4().hex}",
        public_base_url="https://reply.example.com",
        verify_token="verify-token",
        page_id="page-1",
        instagram_login_mode="facebook_login",
        tenant_id="tenant-a",
        brand_id="brand-a",
        secrets_root=tmp_path,
        transport=httpx.MockTransport(handler),
    )

    session.expire_all()
    account = await session.get(models.PlatformAccount, result.account_id)
    app = await session.get(models.PlatformApp, result.platform_app_id)
    assert app.platform_family == "meta"
    assert account.status == "active"
    assert account.config["instagram_login_mode"] == "facebook_login"
    assert account.config["page_id"] == "page-1"
    assert account.config["meta_health_status"] == "READY"
    assert account.config["meta_app_subscribed_fields"] == ["messages"]
    assert account.capability == {"dm": True, "comments": False, "max_text_length": 1000}
    assert [request.url.path for request in requests] == [
        "/v23.0/ig-1",
        f"/v23.0/{app_id}/subscriptions",
        f"/v23.0/{app_id}/subscriptions",
        "/v23.0/page-1/subscribed_apps",
    ]
    assert b"object=instagram" in requests[2].content
    for request in requests:
        assert request.url.host == "graph.facebook.com"
        if request.url.path.endswith("/subscriptions"):
            continue
        assert request.url.params["appsecret_proof"] == appsecret_proof(
            "page-token", "facebook-app-secret"
        )


async def test_instagram_login_uses_instagram_account_subscription_path(
    migrated_db,
    session,
    tmp_path,
):
    requests: list[httpx.Request] = []
    app_id = f"ig-app-{uuid.uuid4().hex}"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path.endswith("/me"):
            return httpx.Response(200, json={"user_id": "ig-1", "username": "shop"})
        if request.url.path.endswith("/subscriptions"):
            if request.method == "GET":
                return httpx.Response(200, json={"data": []})
            return httpx.Response(200, json={"success": True})
        if request.method == "POST" and request.url.path.endswith("/subscribed_apps"):
            return httpx.Response(200, json={"success": True})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    result = await connect_meta_account(
        platform="instagram",
        external_account_id="ig-1",
        access_token="instagram-token",
        app_secret="instagram-app-secret",
        app_id=app_id,
        app_public_id=f"instagram_ig_{uuid.uuid4().hex}",
        public_base_url="https://reply.example.com",
        verify_token="verify-token",
        instagram_login_mode="instagram_login",
        tenant_id="tenant-a",
        brand_id="brand-a",
        secrets_root=tmp_path,
        transport=httpx.MockTransport(handler),
    )

    session.expire_all()
    account = await session.get(models.PlatformAccount, result.account_id)
    app = await session.get(models.PlatformApp, result.platform_app_id)
    assert app.platform_family == "instagram"
    assert account.status == "active"
    assert account.config["instagram_login_mode"] == "instagram_login"
    assert "page_id" not in account.config
    assert account.config["meta_health_status"] == "READY"
    assert [request.url.path for request in requests] == [
        "/v23.0/me",
        f"/v23.0/{app_id}/subscriptions",
        f"/v23.0/{app_id}/subscriptions",
        "/v23.0/ig-1/subscribed_apps",
    ]
    for request in requests:
        if request.url.path.endswith("/subscriptions"):
            # App 级 Webhooks 挂在 Meta App 上，即使账号流量走 graph.instagram.com。
            assert request.url.host == "graph.facebook.com"
            continue
        assert request.url.host == "graph.instagram.com"
        assert request.url.params["appsecret_proof"] == appsecret_proof(
            "instagram-token", "instagram-app-secret"
        )


@pytest.mark.parametrize("login_mode", ["facebook_login", "instagram_login"])
async def test_instagram_comment_provisioning_validates_permissions_and_subscriptions(
    migrated_db,
    session,
    tmp_path,
    monkeypatch,
    login_mode,
):
    requests: list[httpx.Request] = []
    app_id = f"ig-comments-{login_mode}-{uuid.uuid4().hex}"
    settings = service.get_settings().model_copy(
        update={"meta_comment_reply_enabled": True, "meta_auto_reply_enabled": True}
    )
    monkeypatch.setattr(service, "get_settings", lambda: settings)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path.endswith("/ig-1"):
            return httpx.Response(200, json={"id": "ig-1", "name": "Shop"})
        if request.method == "GET" and request.url.path.endswith("/me"):
            return httpx.Response(200, json={"user_id": "ig-1", "username": "shop"})
        if request.url.path.endswith(("/debug_token", "/debug_access_token")):
            permission = (
                "instagram_manage_comments"
                if login_mode == "facebook_login"
                else "instagram_business_manage_comments"
            )
            scopes = [permission]
            granular_scopes = []
            if login_mode == "facebook_login":
                scopes.append("pages_read_engagement")
                granular_scopes = [
                    {"scope": scope, "target_ids": ["page-1"]} for scope in scopes
                ]
            return httpx.Response(
                200,
                json={
                    "data": {
                        "is_valid": True,
                        "scopes": scopes,
                        "granular_scopes": granular_scopes,
                    }
                },
            )
        if request.url.path.endswith("/subscriptions"):
            if request.method == "GET":
                return httpx.Response(200, json={"data": []})
            return httpx.Response(200, json={"success": True})
        if request.method == "POST" and request.url.path.endswith("/subscribed_apps"):
            return httpx.Response(200, json={"success": True})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    result = await connect_meta_account(
        platform="instagram",
        external_account_id="ig-1",
        access_token="page-token" if login_mode == "facebook_login" else "instagram-token",
        app_secret="app-secret",
        app_id=app_id,
        app_public_id=f"ig_comments_{uuid.uuid4().hex}",
        public_base_url="https://reply.example.com",
        verify_token="verify-token",
        page_id="page-1" if login_mode == "facebook_login" else None,
        instagram_login_mode=login_mode,
        tenant_id="tenant-a",
        brand_id="brand-a",
        enable_comments=True,
        automation_default="BOT_ACTIVE",
        secrets_root=tmp_path,
        transport=httpx.MockTransport(handler),
    )

    session.expire_all()
    account = await session.get(models.PlatformAccount, result.account_id)
    expected_account_fields = (
        ["messages"]
        if login_mode == "facebook_login"
        else ["messages", "comments"]
    )
    assert account.capability == {"dm": True, "comments": True, "max_text_length": 1000}
    assert account.automation_default == "BOT_ACTIVE"
    assert account.config["meta_desired_subscribed_fields"] == expected_account_fields
    assert account.config["meta_desired_app_subscribed_fields"] == ["messages", "comments"]
    assert account.config["meta_subscribed_fields"] == expected_account_fields
    assert account.config["meta_app_subscribed_fields"] == ["comments", "messages"]
    debug = next(
        request
        for request in requests
        if request.url.path.endswith(("/debug_token", "/debug_access_token"))
    )
    assert debug.url.params["input_token"] in {"page-token", "instagram-token"}
    account_subscription = next(
        request for request in requests if request.url.path.endswith("/subscribed_apps")
    )
    assert account_subscription.url.params["subscribed_fields"] == ",".join(
        expected_account_fields
    )
