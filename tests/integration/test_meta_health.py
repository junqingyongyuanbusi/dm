import uuid

import httpx
import pytest
from sqlalchemy import insert

from social_reply.application.account_management import meta_health
from social_reply.infrastructure.database import models
from social_reply.infrastructure.secret_crypto import encrypt_secret_bundle

pytestmark = pytest.mark.integration


async def _seed_facebook_account(session) -> tuple[uuid.UUID, uuid.UUID]:
    app_id, account_id = uuid.uuid4(), uuid.uuid4()
    await session.execute(
        insert(models.PlatformApp).values(
            id=app_id,
            tenant_id="default",
            platform_family="meta",
            name="Messenger App",
            external_app_id="app-1",
            public_id=f"meta_{uuid.uuid4().hex}",
            credential_bundle=encrypt_secret_bundle(
                {"app_secret": "app-secret", "verify_token": "verify"}
            ),
            config={"api_version": "v23.0"},
            status="active",
        )
    )
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            tenant_id="default",
            brand_id="default",
            platform="facebook",
            platform_app_id=app_id,
            name="Page",
            external_account_id="page-1",
            public_id=f"fb_{uuid.uuid4().hex}",
            credential_bundle=encrypt_secret_bundle({"access_token": "page-token"}),
            config={
                "delivery_mode": "direct",
                "graph_base_url": "https://graph.facebook.com",
                "api_version": "v23.0",
                "instagram_login_mode": "facebook_login",
                "meta_desired_subscribed_fields": ["messages"],
            },
            capability={"dm": True, "comments": False, "max_text_length": 2000},
            automation_default="BOT_DRAFT_ONLY",
            status="active",
        )
    )
    await session.commit()
    return app_id, account_id


async def test_meta_health_repairs_missing_messenger_subscription(session, monkeypatch):
    _app_id, account_id = await _seed_facebook_account(session)
    observed = [(), ("messages",)]
    subscriptions = []

    class FakeClient:
        def __init__(self, **kwargs):
            assert kwargs["app_secret"] == "app-secret"

        async def get_account(self):
            return {"id": "page-1", "name": "Page"}

        async def aclose(self):
            return None

    async def get_fields(**kwargs):
        assert kwargs["app_id"] == "app-1"
        return observed.pop(0)

    async def subscribe(**kwargs):
        subscriptions.append(kwargs)
        return ("messages",)

    monkeypatch.setattr(meta_health, "MetaGraphClient", FakeClient)
    monkeypatch.setattr(meta_health, "get_meta_subscription_fields", get_fields)
    monkeypatch.setattr(meta_health, "subscribe_meta_account", subscribe)
    meta_health._last_check_at = None

    assert await meta_health.reconcile_meta_account_health(force=True) == []
    assert subscriptions[0]["enable_dm"] is True
    assert subscriptions[0]["enable_comments"] is False
    session.expire_all()
    account = await session.get(models.PlatformAccount, account_id)
    assert account.config["meta_health_status"] == "READY"
    assert account.config["meta_subscribed_fields"] == ["messages"]
    assert account.config["meta_health_error_code"] is None


async def test_meta_health_checks_standalone_instagram_account_path(session, monkeypatch):
    app_id, account_id = uuid.uuid4(), uuid.uuid4()
    await session.execute(
        insert(models.PlatformApp).values(
            id=app_id,
            tenant_id="default",
            platform_family="instagram",
            name="Instagram App",
            external_app_id="ig-app-1",
            public_id=f"instagram_{uuid.uuid4().hex}",
            credential_bundle=encrypt_secret_bundle(
                {"app_secret": "ig-app-secret", "verify_token": "verify"}
            ),
            config={"api_version": "v23.0"},
            status="active",
        )
    )
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            tenant_id="default",
            brand_id="default",
            platform="instagram",
            platform_app_id=app_id,
            name="@shop",
            external_account_id="ig-1",
            public_id=f"ig_{uuid.uuid4().hex}",
            credential_bundle=encrypt_secret_bundle({"access_token": "ig-token"}),
            config={
                "delivery_mode": "direct",
                "graph_base_url": "https://graph.instagram.com",
                "api_version": "v23.0",
                "instagram_login_mode": "instagram_login",
                "meta_desired_subscribed_fields": ["messages"],
            },
            capability={"dm": True, "comments": False, "max_text_length": 1000},
            automation_default="BOT_DRAFT_ONLY",
            status="active",
        )
    )
    await session.commit()

    class FakeClient:
        def __init__(self, **kwargs):
            assert kwargs["graph_base_url"] == "https://graph.instagram.com"
            assert kwargs["instagram_login_mode"] == "instagram_login"
            assert kwargs["page_id"] is None

        async def get_account(self):
            return {"id": "ig-1", "username": "shop"}

        async def aclose(self):
            return None

    async def get_fields(**kwargs):
        assert kwargs["platform"] == "instagram"
        assert kwargs["external_account_id"] == "ig-1"
        assert kwargs["instagram_login_mode"] == "instagram_login"
        assert kwargs["app_id"] == "ig-app-1"
        return ("messages",)

    monkeypatch.setattr(meta_health, "MetaGraphClient", FakeClient)
    monkeypatch.setattr(meta_health, "get_meta_subscription_fields", get_fields)
    meta_health._last_check_at = None

    assert await meta_health.reconcile_meta_account_health(force=True) == []
    session.expire_all()
    account = await session.get(models.PlatformAccount, account_id)
    assert account.config["meta_health_status"] == "READY"
    assert account.config["meta_subscribed_fields"] == ["messages"]


async def test_meta_health_marks_expired_token_for_reauthorization(session, monkeypatch):
    _app_id, account_id = await _seed_facebook_account(session)

    class ExpiredClient:
        def __init__(self, **_kwargs):
            pass

        async def get_account(self):
            request = httpx.Request("GET", "https://graph.facebook.com/v23.0/page-1")
            response = httpx.Response(
                400,
                request=request,
                json={"error": {"code": 190, "message": "Invalid OAuth access token"}},
            )
            raise httpx.HTTPStatusError("expired", request=request, response=response)

        async def aclose(self):
            return None

    monkeypatch.setattr(meta_health, "MetaGraphClient", ExpiredClient)
    meta_health._last_check_at = None

    assert await meta_health.reconcile_meta_account_health(force=True) == [str(account_id)]
    session.expire_all()
    account = await session.get(models.PlatformAccount, account_id)
    assert account.config["meta_health_status"] == "REAUTH_REQUIRED"
    assert account.config["meta_health_error_code"] == "META_HTTP_400_190"
