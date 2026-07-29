import hashlib
import hmac
import json
import uuid

import httpx
import pytest
from sqlalchemy import select

from apps.api.main import create_app
from social_reply.application.account_management import service
from social_reply.application.account_management.service import connect_meta_account
from social_reply.connectors.meta.client import appsecret_proof
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory

pytestmark = pytest.mark.integration


async def test_messenger_provisioning_activates_only_after_dm_subscription(
    migrated_db,
    session,
    tmp_path,
):
    requests: list[httpx.Request] = []
    app_id = f"app-{uuid.uuid4().hex}"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path.endswith("/page-1"):
            return httpx.Response(200, json={"id": "page-1", "name": "Support"})
        if request.url.path.endswith("/subscriptions"):
            if request.method == "GET":
                return httpx.Response(200, json={"data": []})
            return httpx.Response(200, json={"success": True})
        if request.method == "POST" and request.url.path.endswith("/subscribed_apps"):
            return httpx.Response(200, json={"success": True})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    result = await connect_meta_account(
        platform="facebook",
        external_account_id="page-1",
        access_token="page-token",
        app_secret="app-secret",
        app_id=app_id,
        app_public_id=f"messenger_{uuid.uuid4().hex}",
        public_base_url="https://reply.example.com",
        verify_token="verify-token",
        tenant_id="tenant-a",
        brand_id="brand-a",
        secrets_root=tmp_path,
        transport=httpx.MockTransport(handler),
    )

    session.expire_all()
    account = await session.get(models.PlatformAccount, result.account_id)
    assert account.status == "active"
    assert account.automation_default == "BOT_DRAFT_ONLY"
    assert account.capability == {"dm": True, "comments": False, "max_text_length": 2000}
    assert account.config["meta_desired_subscribed_fields"] == ["messages"]
    assert account.config["meta_subscribed_fields"] == ["messages"]
    # App 级订阅缺失时 Meta 不投递任何事件，因此它与账号级订阅同样属于激活前置条件。
    assert account.config["meta_app_subscribed_fields"] == ["messages"]
    assert account.config["meta_health_status"] == "READY"
    assert account.config["meta_health_error_code"] is None
    assert [request.url.path for request in requests] == [
        "/v23.0/page-1",
        f"/v23.0/{app_id}/subscriptions",
        f"/v23.0/{app_id}/subscriptions",
        "/v23.0/page-1/subscribed_apps",
    ]
    for request in requests:
        if request.url.path.endswith("/subscriptions"):
            # App 级订阅用 app access token，不携带页面令牌的 appsecret_proof。
            assert request.url.params["access_token"] == f"{app_id}|app-secret"
            continue
        assert request.url.params["appsecret_proof"] == appsecret_proof("page-token", "app-secret")
    app_subscribe = requests[2]
    assert app_subscribe.method == "POST"
    assert b"object=page" in app_subscribe.content
    assert requests[3].url.params["subscribed_fields"] == "messages"


async def test_messenger_webhook_during_subscription_is_durably_routed(
    migrated_db,
    session,
    tmp_path,
    monkeypatch,
):
    app_public_id = f"messenger_{uuid.uuid4().hex}"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/subscriptions"):
            if request.method == "GET":
                return httpx.Response(200, json={"data": []})
            return httpx.Response(200, json={"success": True})
        return httpx.Response(200, json={"id": "page-1", "name": "Support"})

    async def subscribe_during_provisioning(**_kwargs):
        async with get_session_factory()() as check_session:
            account = (
                await check_session.execute(
                    select(models.PlatformAccount).where(
                        models.PlatformAccount.platform == "facebook",
                        models.PlatformAccount.external_account_id == "page-1",
                    )
                )
            ).scalar_one()
            assert account.status == "active"
            assert account.config["meta_health_status"] == "PROVISIONING"
        payload = {
            "object": "page",
            "entry": [
                {
                    "id": "page-1",
                    "messaging": [
                        {
                            "sender": {"id": "psid-race"},
                            "recipient": {"id": "page-1"},
                            "message": {"mid": "mid-race", "text": "during setup"},
                        }
                    ],
                }
            ],
        }
        body = json.dumps(payload, separators=(",", ":")).encode()
        signature = "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app()),
            base_url="http://test",
        ) as client:
            response = await client.post(
                f"/webhooks/meta/{app_public_id}",
                content=body,
                headers={"X-Hub-Signature-256": signature},
            )
        assert response.status_code == 200
        return ("messages",)

    monkeypatch.setattr(service, "subscribe_meta_account", subscribe_during_provisioning)
    result = await connect_meta_account(
        platform="facebook",
        external_account_id="page-1",
        access_token="page-token",
        app_secret="app-secret",
        app_id=f"app-{uuid.uuid4().hex}",
        app_public_id=app_public_id,
        public_base_url="https://reply.example.com",
        verify_token="verify-token",
        tenant_id="tenant-a",
        brand_id="brand-a",
        secrets_root=tmp_path,
        transport=httpx.MockTransport(handler),
    )

    session.expire_all()
    occurrence = (
        await session.execute(
            select(models.RawEvent).where(models.RawEvent.platform_account_id == result.account_id)
        )
    ).scalar_one()
    assert occurrence.processing_status == "PROCESSED"
    decision = (await session.execute(select(models.ReplyDecision))).scalar_one()
    assert decision.action == "draft"
    assert await session.scalar(select(models.OutboxMessage.id)) is None


async def test_failed_messenger_subscription_leaves_account_disabled(
    migrated_db,
    session,
    tmp_path,
):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"id": "page-1", "name": "Support"})
        return httpx.Response(
            400,
            json={"error": {"code": 200, "message": "Missing pages_manage_metadata"}},
        )

    with pytest.raises(httpx.HTTPStatusError):
        await connect_meta_account(
            platform="facebook",
            external_account_id="page-1",
            access_token="page-token",
            app_secret="app-secret",
            app_id=f"app-{uuid.uuid4().hex}",
            app_public_id=f"messenger_{uuid.uuid4().hex}",
            public_base_url="https://reply.example.com",
            verify_token="verify-token",
            tenant_id="tenant-a",
            brand_id="brand-a",
            secrets_root=tmp_path,
            transport=httpx.MockTransport(handler),
        )

    account = (
        await session.execute(
            select(models.PlatformAccount).where(
                models.PlatformAccount.tenant_id == "tenant-a",
                models.PlatformAccount.platform == "facebook",
                models.PlatformAccount.external_account_id == "page-1",
            )
        )
    ).scalar_one()
    assert account.status == "DISABLED"
    assert account.config["meta_health_status"] == "ERROR"
    assert account.config["meta_health_error_code"] == "META_HTTP_400_200"
