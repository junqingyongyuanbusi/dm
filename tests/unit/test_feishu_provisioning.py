import json
import uuid

import httpx
import pytest

from social_reply.application.account_management import feishu
from social_reply.connectors.feishu.client import FeishuClient, FeishuClientError


async def test_feishu_client_fetches_tenant_token_and_active_bot():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "tenant-token", "expire": 7200},
            )
        return httpx.Response(
            200,
            json={
                "code": 0,
                "bot": {
                    "open_id": "ou_bot_1",
                    "app_name": "Support Bot",
                    "activate_status": 2,
                },
            },
        )

    client = FeishuClient(
        app_id="cli_12345678",
        app_secret="app-secret",
        transport=httpx.MockTransport(handler),
    )
    bot = await client.inspect_bot()
    await client.aclose()

    assert bot.open_id == "ou_bot_1"
    assert bot.name == "Support Bot"
    assert bot.activate_status == 2
    assert requests[0].url.path == "/open-apis/auth/v3/tenant_access_token/internal"
    assert json.loads(requests[0].content) == {
        "app_id": "cli_12345678",
        "app_secret": "app-secret",
    }
    assert requests[1].url.path == "/open-apis/bot/v3/info"
    assert requests[1].headers["authorization"] == "Bearer tenant-token"


@pytest.mark.parametrize(
    ("response", "code", "retryable"),
    [
        (httpx.Response(503), "FEISHU_HTTP_503", True),
        (httpx.Response(400), "FEISHU_HTTP_400", False),
        (httpx.Response(200, json={"code": 99991400}), "FEISHU_API_99991400", True),
        (httpx.Response(400, json={"code": 10003}), "FEISHU_API_10003", False),
        (
            httpx.Response(200, json={"code": 0, "tenant_access_token": "", "expire": 7200}),
            "FEISHU_TOKEN_MISSING",
            False,
        ),
        (
            httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "token", "expire": 1},
            ),
            "FEISHU_TOKEN_EXPIRE_INVALID",
            False,
        ),
    ],
)
async def test_feishu_client_classifies_token_errors(response, code, retryable):
    client = FeishuClient(
        app_id="cli_12345678",
        app_secret="credential-must-not-leak",
        transport=httpx.MockTransport(lambda _request: response),
    )
    with pytest.raises(FeishuClientError) as exc_info:
        await client.tenant_access_token()
    await client.aclose()
    assert exc_info.value.code == code
    assert exc_info.value.retryable is retryable
    assert "credential-must-not-leak" not in str(exc_info.value)


@pytest.mark.parametrize(
    ("bot", "code"),
    [
        (None, "FEISHU_BOT_MISSING"),
        ({"open_id": "", "activate_status": 2}, "FEISHU_BOT_OPEN_ID_MISSING"),
        ({"open_id": "ou_1", "activate_status": 1}, "FEISHU_BOT_NOT_ACTIVATED"),
    ],
)
async def test_feishu_client_requires_active_bot_object(bot, code):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "token", "expire": 7200},
            )
        return httpx.Response(200, json={"code": 0, "bot": bot})

    client = FeishuClient(
        app_id="cli_12345678",
        app_secret="app-secret",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(FeishuClientError, match=code):
        await client.inspect_bot()
    await client.aclose()


async def test_connect_feishu_provisions_direct_draft_account(monkeypatch, tmp_path):
    captured = {}
    settings = feishu.get_settings().model_copy(update={"feishu_enabled": True})
    monkeypatch.setattr(feishu, "get_settings", lambda: settings)

    async def fake_provision(**kwargs):
        captured.update(kwargs)
        return uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd"), "fs_public"

    monkeypatch.setattr(feishu, "provision_direct_account", fake_provision)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "token", "expire": 7200},
            )
        return httpx.Response(
            200,
            json={
                "code": 0,
                "bot": {
                    "open_id": "ou_bot_1",
                    "app_name": "Support Bot",
                    "activate_status": 2,
                },
            },
        )

    result = await feishu.connect_feishu_account(
        app_id="cli_12345678",
        app_secret="app-secret",
        verification_token="verify-secret",
        encrypt_key="encrypt-secret",
        public_base_url="https://reply.example",
        tenant_id="tenant-a",
        brand_id="brand-a",
        secrets_root=tmp_path,
        transport=httpx.MockTransport(handler),
    )

    assert captured["platform"] == "feishu"
    assert captured["external_account_id"] == "cli_12345678"
    assert captured["public_id_prefix"] == "fs"
    assert captured["credential_bundle"] == {
        "app_id": "cli_12345678",
        "app_secret": "app-secret",
    }
    assert captured["webhook_secret_bundle"] == {
        "verification_token": "verify-secret",
        "encrypt_key": "encrypt-secret",
    }
    assert captured["config"]["feishu_bot_open_id"] == "ou_bot_1"
    assert captured["config"]["feishu_health_status"] == "READY"
    assert captured["capability"] == {
        "dm": True,
        "mentions": True,
    }
    assert captured["status"] == "active"
    assert captured["automation_default"] == "BOT_DRAFT_ONLY"
    assert result.webhook_url == "https://reply.example/webhooks/feishu/fs_public"
    assert result.bot_name == "Support Bot"
    assert result.bot_status == 2
    assert "im.message.receive_v1" in " ".join(result.manual_steps)
    assert "mention" in " ".join(result.manual_steps).lower()


async def test_connect_feishu_rejects_unapproved_api_origin_before_provider_call(monkeypatch):
    settings = feishu.get_settings().model_copy(update={"feishu_enabled": True})
    monkeypatch.setattr(feishu, "get_settings", lambda: settings)
    with pytest.raises(ValueError, match="invalid_feishu_api_base_url"):
        await feishu.connect_feishu_account(
            app_id="cli_12345678",
            app_secret="app-secret",
            verification_token="verification-secret",
            encrypt_key="encrypt-secret",
            public_base_url="https://reply.example",
            api_base_url="https://attacker.example",
        )


async def test_connect_feishu_rejects_bot_active_before_provider_call(monkeypatch):
    settings = feishu.get_settings().model_copy(update={"feishu_enabled": True})
    monkeypatch.setattr(feishu, "get_settings", lambda: settings)
    with pytest.raises(ValueError, match="feishu_requires_bot_draft_only"):
        await feishu.connect_feishu_account(
            app_id="cli_12345678",
            app_secret="app-secret",
            verification_token="verification-secret",
            encrypt_key="encrypt-secret",
            public_base_url="https://reply.example",
            automation_default="BOT_ACTIVE",
        )
