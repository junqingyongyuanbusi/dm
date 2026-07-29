import json
import uuid

import httpx
import pytest
from chat_xdk import Chat

from social_reply.application.account_management import service
from social_reply.application.account_management import whatsapp as whatsapp_service
from social_reply.application.platform_accounts import PlatformAccountRuntime
from social_reply.connectors.xchat.crypto import export_private_key_b64


def _xchat_material(version: str = "7") -> tuple[str, dict]:
    chat = Chat()
    generated = chat.generate_keypairs()
    registration = generated.public_key
    return export_private_key_b64(chat), {
        "public_key_version": version,
        "public_key": registration.public_key,
        "signing_public_key": registration.signing_public_key,
        "identity_public_key_signature": registration.identity_public_key_signature,
        "juicebox_config": {"tokens": {}},
    }


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


@pytest.mark.parametrize(
    ("platform", "settings_update", "error"),
    [
        ("facebook", {"facebook_messenger_enabled": False}, "facebook_integration_disabled"),
        ("instagram", {"instagram_messaging_enabled": False}, "instagram_integration_disabled"),
    ],
)
async def test_connect_meta_rechecks_feature_flags_before_platform_calls(
    monkeypatch, tmp_path, platform, settings_update, error
):
    settings = service.get_settings().model_copy(update=settings_update)
    monkeypatch.setattr(service, "get_settings", lambda: settings)

    class UnexpectedClient:
        def __init__(self, **_kwargs):
            raise AssertionError("disabled platform must not construct a Graph client")

    monkeypatch.setattr(service, "MetaGraphClient", UnexpectedClient)
    with pytest.raises(ValueError, match=error):
        await service.connect_meta_account(
            platform=platform,
            external_account_id="account-1",
            access_token="token",
            app_secret="secret",
            public_base_url="https://reply.example.com",
            verify_token="verify",
            app_id="app-1",
            secrets_root=tmp_path,
        )


@pytest.mark.parametrize(
    ("platform", "mode", "page_id", "error"),
    [
        ("facebook", "instagram_login", None, "facebook_requires_facebook_login"),
        (
            "instagram",
            "facebook_login",
            None,
            "instagram_facebook_login_requires_page_id",
        ),
        ("instagram", "instagram_login", "page-1", "instagram_login_forbids_page_id"),
    ],
)
async def test_connect_meta_rejects_invalid_login_path_before_graph_calls(
    monkeypatch,
    tmp_path,
    platform,
    mode,
    page_id,
    error,
):
    class UnexpectedClient:
        def __init__(self, **_kwargs):
            raise AssertionError("invalid path must not construct a Graph client")

    monkeypatch.setattr(service, "MetaGraphClient", UnexpectedClient)
    with pytest.raises(ValueError, match=error):
        await service.connect_meta_account(
            platform=platform,
            external_account_id="account-1",
            access_token="token",
            app_secret="secret",
            public_base_url="https://reply.example.com",
            verify_token="verify",
            app_id="app-1",
            instagram_login_mode=mode,
            page_id=page_id,
            secrets_root=tmp_path,
        )


@pytest.mark.parametrize(
    ("enable_dm", "enable_comments", "automation_default", "error"),
    [
        (False, False, "BOT_DRAFT_ONLY", "meta_dm_required"),
        (True, True, "BOT_DRAFT_ONLY", "meta_comment_reply_disabled"),
        (True, False, "BOT_ACTIVE", "meta_requires_bot_draft_only"),
    ],
)
async def test_connect_meta_rejects_out_of_scope_launch_policy_before_graph_calls(
    monkeypatch,
    tmp_path,
    enable_dm,
    enable_comments,
    automation_default,
    error,
):
    class UnexpectedClient:
        def __init__(self, **_kwargs):
            raise AssertionError("invalid launch policy must not construct a Graph client")

    monkeypatch.setattr(service, "MetaGraphClient", UnexpectedClient)
    with pytest.raises(ValueError, match=error):
        await service.connect_meta_account(
            platform="facebook",
            external_account_id="page-1",
            access_token="token",
            app_secret="secret",
            public_base_url="https://reply.example.com",
            verify_token="verify",
            app_id="app-1",
            enable_dm=enable_dm,
            enable_comments=enable_comments,
            automation_default=automation_default,
            secrets_root=tmp_path,
        )


async def test_connect_meta_accepts_bot_active_once_deployment_opts_in(monkeypatch, tmp_path):
    # 开关只解锁发布范围校验：能走到构造 Graph client，就证明闸门已放行。
    settings = service.get_settings().model_copy(update={"meta_auto_reply_enabled": True})
    monkeypatch.setattr(service, "get_settings", lambda: settings)

    class ReachedGraphCall(Exception):
        pass

    class SentinelClient:
        def __init__(self, **_kwargs):
            raise ReachedGraphCall

    monkeypatch.setattr(service, "MetaGraphClient", SentinelClient)
    with pytest.raises(ReachedGraphCall):
        await service.connect_meta_account(
            platform="facebook",
            external_account_id="page-1",
            access_token="token",
            app_secret="secret",
            public_base_url="https://reply.example.com",
            verify_token="verify",
            app_id="app-1",
            automation_default="BOT_ACTIVE",
            secrets_root=tmp_path,
        )


async def test_connect_meta_allows_instagram_comments_when_switch_is_on(monkeypatch, tmp_path):
    settings = service.get_settings().model_copy(
        update={"meta_comment_reply_enabled": True, "meta_auto_reply_enabled": True}
    )
    monkeypatch.setattr(service, "get_settings", lambda: settings)

    class ReachedGraphCall(Exception):
        pass

    class SentinelClient:
        def __init__(self, **_kwargs):
            raise ReachedGraphCall

    monkeypatch.setattr(service, "MetaGraphClient", SentinelClient)
    with pytest.raises(ReachedGraphCall):
        await service.connect_meta_account(
            platform="instagram",
            external_account_id="ig-1",
            access_token="token",
            app_secret="secret",
            public_base_url="https://reply.example.com",
            verify_token="verify",
            app_id="app-1",
            page_id="page-1",
            enable_comments=True,
            automation_default="BOT_ACTIVE",
            secrets_root=tmp_path,
        )


async def test_connect_whatsapp_rechecks_feature_flag_before_platform_calls(monkeypatch, tmp_path):
    settings = whatsapp_service.get_settings().model_copy(update={"whatsapp_enabled": False})
    monkeypatch.setattr(whatsapp_service, "get_settings", lambda: settings)

    class UnexpectedClient:
        def __init__(self, **_kwargs):
            raise AssertionError("disabled WhatsApp must not construct a client")

    monkeypatch.setattr(whatsapp_service, "WhatsAppClient", UnexpectedClient)
    with pytest.raises(ValueError, match="whatsapp_integration_disabled"):
        await whatsapp_service.connect_whatsapp_account(
            external_account_id="phone-1",
            access_token="token",
            app_secret="secret",
            public_base_url="https://reply.example.com",
            verify_token="verify",
            app_id="app-1",
            secrets_root=tmp_path,
        )


async def test_connect_meta_reuses_existing_app_public_id(monkeypatch, tmp_path):
    async def fake_provision_meta_app(**kwargs):
        assert kwargs["app_public_id"] == "meta_public"
        assert kwargs["verify_token"] == "existing-verify-token"
        return (
            uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
            "meta_public",
            "existing-verify-token",
            "app-1",
        )

    async def fake_provision_account(**kwargs):
        assert kwargs["platform_app_id"] == uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
        assert kwargs["status"] == "active"
        assert kwargs["config"]["meta_health_status"] == "PROVISIONING"
        assert kwargs["capability"]["comments"] is False
        return uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"), "ig_public"

    async def fake_subscribe(**kwargs):
        assert kwargs["external_account_id"] == "page-1"
        assert kwargs["app_secret"] == "app-secret"
        return ("messages",)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/subscriptions"):
            if request.method == "GET":
                return httpx.Response(200, json={"data": []})
            return httpx.Response(200, json={"success": True})
        assert request.headers["Authorization"] == "Bearer access-token"
        assert request.url.params["appsecret_proof"]
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
    settings = service.get_settings().model_copy(
        update={"x_legacy_dm_enabled": False, "xchat_enabled": True}
    )
    monkeypatch.setattr(service, "get_settings", lambda: settings)
    private_keys, public_key_record = _xchat_material()
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
                "xchat_private_keys_b64": private_keys,
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

    class FakeXChatClient:
        def __init__(self, **kwargs):
            pass

        async def get_user_public_keys(self, user_id):
            return [public_key_record]

        async def aclose(self):
            pass

    async def fake_existing(**kwargs):
        return existing

    async def fake_provision(**kwargs):
        assert kwargs["credential_bundle"]["xchat_private_keys_b64"] == private_keys
        assert kwargs["credential_bundle"]["xchat_signing_key_version"] == "7"
        assert kwargs["config"]["xchat_cursors"] == {"x-1-peer": "123"}
        assert kwargs["config"]["xchat_key_state"] == "READY"
        assert kwargs["capability"]["dm"] is True
        assert kwargs["capability"]["x_chat"] is True
        return uuid.uuid4(), "primary"

    app_id = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")

    async def fake_x_app(**kwargs):
        return app_id, "x_oauth"

    monkeypatch.setattr(service, "XClient", FakeXClient)
    monkeypatch.setattr(service, "XChatClient", FakeXChatClient)
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


async def test_connect_x_detects_registered_xchat_that_needs_pin(monkeypatch, tmp_path):
    settings = service.get_settings().model_copy(
        update={"x_legacy_dm_enabled": True, "xchat_enabled": True}
    )
    monkeypatch.setattr(service, "get_settings", lambda: settings)

    class FakeXClient:
        def __init__(self, **kwargs):
            pass

        async def get_me(self):
            return {"id": "x-1", "username": "bot"}

        async def read_dm_events(self, *, max_results):
            return [], None

        async def aclose(self):
            pass

    class FakeXChatClient:
        def __init__(self, **kwargs):
            pass

        async def get_user_public_keys(self, user_id):
            return [
                {
                    "public_key_version": "7",
                    "public_key": "identity",
                    "signing_public_key": "signing",
                    "juicebox_config": {"tokens": {}},
                }
            ]

        async def aclose(self):
            pass

    async def missing_existing(**kwargs):
        raise LookupError("missing")

    async def fake_x_app(**kwargs):
        return uuid.uuid4(), "x_oauth"

    async def fake_provision(**kwargs):
        assert kwargs["config"]["xchat_registered"] is True
        assert kwargs["config"]["xchat_key_state"] == "RECOVERY_REQUIRED"
        assert kwargs["capability"]["dm"] is True
        assert kwargs["capability"]["x_chat"] is False
        return uuid.uuid4(), "x_public"

    monkeypatch.setattr(service, "XClient", FakeXClient)
    monkeypatch.setattr(service, "XChatClient", FakeXChatClient)
    monkeypatch.setattr(
        service,
        "get_platform_account_runtime_by_external_id",
        missing_existing,
    )
    monkeypatch.setattr(service, "ensure_x_platform_app", fake_x_app)
    monkeypatch.setattr(service, "provision_direct_account", fake_provision)

    result = await service.connect_x_account(
        consumer_key="ck",
        consumer_secret="cs",
        access_token="at",
        access_token_secret="ats",
        environment="oauth",
        public_base_url="https://reply.example.com",
        secrets_root=tmp_path,
    )

    assert "需要提交 4 位 PIN" in result.manual_steps[1]


async def test_connect_x_rejects_app_without_direct_message_permission(monkeypatch, tmp_path):
    settings = service.get_settings().model_copy(
        update={"x_legacy_dm_enabled": False, "xchat_enabled": True}
    )
    monkeypatch.setattr(service, "get_settings", lambda: settings)
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


@pytest.mark.parametrize(
    ("settings_values", "xchat_pin", "error"),
    [
        (
            {
                "x_integration_enabled": False,
                "x_legacy_dm_enabled": False,
                "x_activity_enabled": False,
                "xchat_enabled": False,
            },
            None,
            "x_integration_disabled",
        ),
        (
            {
                "x_integration_enabled": True,
                "x_legacy_dm_enabled": True,
                "x_activity_enabled": True,
                "xchat_enabled": False,
            },
            "1234",
            "xchat_disabled",
        ),
    ],
)
async def test_connect_x_rechecks_feature_flags_at_execution(
    monkeypatch, tmp_path, settings_values, xchat_pin, error
):
    monkeypatch.setattr(
        service,
        "get_settings",
        lambda: type("Settings", (), settings_values)(),
    )
    with pytest.raises(ValueError, match=error):
        await service.connect_x_account(
            consumer_key="ck",
            consumer_secret="cs",
            access_token="at",
            access_token_secret="ats",
            environment="oauth",
            xchat_pin=xchat_pin,
            public_base_url="https://reply.example.com",
            secrets_root=tmp_path,
        )


async def test_connect_x_skips_legacy_probe_when_disabled(monkeypatch, tmp_path):
    class FakeXClient:
        def __init__(self, **kwargs):
            pass

        async def get_me(self):
            return {"id": "x-1", "username": "bot"}

        async def read_dm_events(self, *, max_results):
            raise AssertionError("legacy DM probe must be disabled")

        async def aclose(self):
            pass

    async def missing_existing(**kwargs):
        raise LookupError("missing")

    async def fake_x_app(**kwargs):
        return uuid.uuid4(), "x_oauth"

    async def fake_provision(**kwargs):
        assert kwargs["capability"]["dm"] is False
        return uuid.uuid4(), "x_public"

    monkeypatch.setattr(
        service,
        "get_settings",
        lambda: type(
            "Settings",
            (),
            {
                "x_legacy_dm_enabled": False,
                "x_activity_enabled": True,
                "xchat_enabled": False,
                "x_integration_enabled": True,
            },
        )(),
    )
    monkeypatch.setattr(service, "XClient", FakeXClient)
    monkeypatch.setattr(
        service,
        "get_platform_account_runtime_by_external_id",
        missing_existing,
    )
    monkeypatch.setattr(service, "ensure_x_platform_app", fake_x_app)
    monkeypatch.setattr(service, "provision_direct_account", fake_provision)

    result = await service.connect_x_account(
        consumer_key="ck",
        consumer_secret="cs",
        access_token="at",
        access_token_secret="ats",
        environment="oauth",
        public_base_url="https://reply.example.com",
        secrets_root=tmp_path,
    )
    assert result.public_id == "x_public"


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

        async def get_user_public_keys(self, user_id):
            return [{"public_key_version": "7"}]

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
    dispatched = []

    async def fake_dispatch(actor, *args, **kwargs):
        dispatched.append((actor.actor_name, args))

    monkeypatch.setattr(service, "dispatch_actor", fake_dispatch)
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
    assert str(fake_session.statement).count("||") == 2
    assert dispatched == [("recover_xchat_account", (str(existing.id),))]
