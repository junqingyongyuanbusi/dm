import uuid

import pytest

from social_reply.application.account_management import meta_app


class _ExistingApp:
    credential_bundle = {"verify_token": "stored-token"}


async def test_existing_meta_app_preserves_stored_verify_token(monkeypatch, tmp_path):
    async def find_app(**_kwargs):
        return _ExistingApp()

    async def find_external(**_kwargs):
        return "app-1"

    async def provision(**kwargs):
        assert kwargs["credential_bundle"]["verify_token"] == "stored-token"
        return uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"), "meta-public"

    monkeypatch.setattr(meta_app, "find_platform_app_by_public_id", find_app)
    monkeypatch.setattr(meta_app, "_find_platform_app_external_id", find_external)
    monkeypatch.setattr(meta_app, "provision_platform_app", provision)

    _, _, token, external_app_id = await meta_app.provision_meta_app(
        tenant_id="default",
        app_id=None,
        app_public_id="meta-public",
        app_name=None,
        app_secret="secret",
        verify_token="stored-token",
        secrets_root=tmp_path,
        graph_base_url="https://graph.facebook.com",
        api_version="v23.0",
    )
    assert token == "stored-token"
    # App 级 Webhook 订阅需要 Meta 的 external app id，而调用方可能只传了 app_public_id。
    assert external_app_id == "app-1"


async def test_existing_meta_app_rejects_verify_token_rotation(monkeypatch, tmp_path):
    async def find_app(**_kwargs):
        return _ExistingApp()

    monkeypatch.setattr(meta_app, "find_platform_app_by_public_id", find_app)

    with pytest.raises(ValueError, match="meta_verify_token_rotation_not_supported"):
        await meta_app.provision_meta_app(
            tenant_id="default",
            app_id="app-1",
            app_public_id="meta-public",
            app_name=None,
            app_secret="secret",
            verify_token="different-token",
            secrets_root=tmp_path,
            graph_base_url="https://graph.facebook.com",
            api_version="v23.0",
        )
