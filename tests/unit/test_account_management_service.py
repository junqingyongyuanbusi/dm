import json
import uuid

import httpx
import pytest

from social_reply.application.account_management import service
from social_reply.application.platform_accounts import PlatformAccountRuntime


async def test_connect_telegram_validates_and_configures_webhook(monkeypatch, tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/getMe"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {"id": 42, "username": "reply_bot", "first_name": "Reply"},
                },
            )
        if request.url.path.endswith("/setWebhook"):
            return httpx.Response(200, json={"ok": True, "result": True})
        return httpx.Response(
            200,
            json={"ok": True, "result": {"pending_update_count": 3}},
        )

    async def fake_provision(**kwargs):
        assert kwargs["credential_bundle"] == {"bot_token": "123:token"}
        assert kwargs["automation_default"] == "BOT_DRAFT_ONLY"
        assert kwargs["preserve_existing_webhook_secret"] is False
        return uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"), "tg_public"

    monkeypatch.setattr(service, "provision_direct_account", fake_provision)
    result = await service.connect_telegram_account(
        token="123:token",
        public_base_url="https://reply.example.com/",
        secrets_root=tmp_path,
        rotate_webhook_secret=True,
        transport=httpx.MockTransport(handler),
    )

    assert result.external_account_id == "42"
    assert result.webhook_url == "https://reply.example.com/webhooks/telegram/tg_public"
    assert result.pending_update_count == 3
    set_webhook = json.loads(calls[1].content)
    assert set_webhook["url"] == result.webhook_url
    assert set_webhook["secret_token"]


async def test_connect_meta_reuses_existing_app_public_id(monkeypatch, tmp_path):
    async def fake_provision_meta_app(**kwargs):
        assert kwargs["app_public_id"] == "meta_public"
        assert kwargs["verify_token"] == "existing-verify-token"
        return (
            uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
            "meta_public",
            "existing-verify-token",
        )

    async def fake_provision_account(**kwargs):
        assert kwargs["platform_app_id"] == uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
        return uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"), "ig_public"

    async def fake_subscribe(**kwargs):
        assert kwargs["external_account_id"] == "page-1"
        return ("messages", "comments")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer access-token"
        return httpx.Response(200, json={"id": "ig-1", "name": "IG Account"})

    monkeypatch.setattr(service, "provision_meta_app", fake_provision_meta_app)
    monkeypatch.setattr(service, "provision_direct_account", fake_provision_account)
    monkeypatch.setattr(service, "subscribe_meta_account", fake_subscribe)

    result = await service.connect_meta_account(
        platform="instagram",
        external_account_id="ig-1",
        access_token="access-token",
        app_secret="app-secret",
        app_public_id="meta_public",
        verify_token="existing-verify-token",
        page_id="page-1",
        public_base_url="https://reply.example.com",
        secrets_root=tmp_path,
        transport=httpx.MockTransport(handler),
    )

    assert result.verify_token == "existing-verify-token"
    assert result.webhook_url == "https://reply.example.com/webhooks/meta/meta_public"
    assert result.platform_app_id == uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


async def test_reconnect_x_preserves_xchat_keys_and_cursors_without_pin(monkeypatch, tmp_path):
    existing = PlatformAccountRuntime(
        id=uuid.uuid4(),
        tenant_id="default",
        brand_id="default",
        platform="x",
        platform_app_id=None,
        name="x-bot",
        external_account_id="x-1",
        public_id="primary",
        credential_bundle_data={},
        webhook_secret_bundle_data=None,
        config={
            "delivery_mode": "direct",
            "xchat_enabled": True,
            "xchat_cursors": {"x-1-peer": "123"},
        },
        capability={"dm": True, "x_chat": True},
        config_version=1,
        automation_default="BOT_ACTIVE",
        status="active",
    )
    monkeypatch.setattr(
        type(existing),
        "credential_bundle",
        property(
            lambda self: {
                "consumer_key": "old-ck",
                "consumer_secret": "old-cs",
                "access_token": "old-at",
                "access_token_secret": "old-ats",
                "xchat_private_keys_b64": "private",
                "xchat_signing_key_version": "7",
            }
        ),
    )

    class FakeXClient:
        def __init__(self, **kwargs):
            pass

        async def get_me(self):
            return {"id": "x-1", "username": "bot"}

        async def read_dm_events(self, *, max_results):
            assert max_results == 10
            return [], None

        async def aclose(self):
            pass

    async def fake_existing(**kwargs):
        return existing

    async def fake_provision(**kwargs):
        assert kwargs["credential_bundle"]["xchat_private_keys_b64"] == "private"
        assert kwargs["credential_bundle"]["xchat_signing_key_version"] == "7"
        assert kwargs["config"]["xchat_cursors"] == {"x-1-peer": "123"}
        assert kwargs["capability"]["x_chat"] is True
        return uuid.uuid4(), "primary"

    app_id = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")

    async def fake_x_app(**kwargs):
        return app_id, "x_oauth"

    monkeypatch.setattr(service, "XClient", FakeXClient)
    monkeypatch.setattr(service, "get_platform_account_runtime_by_external_id", fake_existing)
    monkeypatch.setattr(service, "ensure_x_platform_app", fake_x_app)
    monkeypatch.setattr(service, "provision_direct_account", fake_provision)

    result = await service.connect_x_account(
        consumer_key="new-ck",
        consumer_secret="new-cs",
        access_token="new-at",
        access_token_secret="new-ats",
        environment="prod",
        public_base_url="https://reply.example.com",
        secrets_root=tmp_path,
    )
    assert "XChat 已解锁" in result.manual_steps[1]
    assert result.platform_app_id == app_id
    assert result.app_public_id == "x_oauth"
    assert result.webhook_url == "https://reply.example.com/webhooks/x/x_oauth"


async def test_connect_x_rejects_app_without_direct_message_permission(
    monkeypatch, tmp_path
):
    closed = False

    class FakeXClient:
        def __init__(self, **kwargs):
            pass

        async def get_me(self):
            return {"id": "x-1", "username": "bot"}

        async def read_dm_events(self, *, max_results):
            request = httpx.Request("GET", "https://api.x.com/2/dm_events")
            response = httpx.Response(
                403,
                request=request,
                json={
                    "type": "https://api.x.com/2/problems/oauth1-permissions",
                    "title": "Forbidden",
                },
            )
            raise httpx.HTTPStatusError("forbidden", request=request, response=response)

        async def aclose(self):
            nonlocal closed
            closed = True

    monkeypatch.setattr(service, "XClient", FakeXClient)

    with pytest.raises(ValueError, match="x_direct_message_permission_missing"):
        await service.connect_x_account(
            consumer_key="ck",
            consumer_secret="cs",
            access_token="at",
            access_token_secret="ats",
            environment="oauth",
            public_base_url="https://reply.example.com",
            secrets_root=tmp_path,
        )
    assert closed is True


async def test_enable_xchat_updates_existing_account_without_persisting_pin(monkeypatch):
    existing = PlatformAccountRuntime(
        id=uuid.uuid4(),
        tenant_id="default",
        brand_id="default",
        platform="x",
        platform_app_id=None,
        name="x-bot",
        external_account_id="x-1",
        public_id="primary",
        credential_bundle_data={},
        webhook_secret_bundle_data=None,
        config={"delivery_mode": "direct"},
        capability={"dm": True},
        config_version=1,
        automation_default="BOT_ACTIVE",
        status="active",
    )
    monkeypatch.setattr(
        type(existing),
        "credential_bundle",
        property(
            lambda self: {
                "consumer_key": "ck",
                "consumer_secret": "cs",
                "access_token": "at",
                "access_token_secret": "ats",
            }
        ),
    )

    class FakeXChatClient:
        def __init__(self, **kwargs):
            pass

        async def aclose(self):
            pass

    async def fake_runtime(account_id):
        return existing

    async def fake_unlock(**kwargs):
        assert kwargs["pin"] == "1234"
        return "private", "7"

    class FakeResult:
        pass

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def execute(self, statement):
            self.statement = statement
            return FakeResult()

        async def commit(self):
            pass

    fake_session = FakeSession()
    monkeypatch.setattr(service, "get_platform_account_runtime", fake_runtime)
    monkeypatch.setattr(service, "XChatClient", FakeXChatClient)
    monkeypatch.setattr(service, "unlock_account_xchat_keys", fake_unlock)
    monkeypatch.setattr(service, "get_session_factory", lambda: lambda: fake_session)
    encrypted = {}
    monkeypatch.setattr(
        service,
        "encrypt_secret_bundle",
        lambda value: encrypted.update(value) or {"__encrypted__": "cipher"},
    )

    await service.enable_xchat_for_account(account_id=existing.id, pin="1234")
    assert encrypted["xchat_private_keys_b64"] == "private"
    assert encrypted["xchat_signing_key_version"] == "7"
    assert "1234" not in encrypted.values()
