import json
import uuid

import httpx

from social_reply.application.account_management import service


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

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer access-token"
        return httpx.Response(200, json={"id": "ig-1", "name": "IG Account"})

    monkeypatch.setattr(service, "provision_meta_app", fake_provision_meta_app)
    monkeypatch.setattr(service, "provision_direct_account", fake_provision_account)

    result = await service.connect_meta_account(
        platform="instagram",
        external_account_id="ig-1",
        access_token="access-token",
        app_secret="app-secret",
        app_public_id="meta_public",
        verify_token="existing-verify-token",
        public_base_url="https://reply.example.com",
        secrets_root=tmp_path,
        transport=httpx.MockTransport(handler),
    )

    assert result.verify_token == "existing-verify-token"
    assert result.webhook_url == "https://reply.example.com/webhooks/meta/meta_public"
    assert result.platform_app_id == uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
