from social_reply.application.account_management import x_app


async def test_ensure_x_platform_app_persists_shared_app(monkeypatch, tmp_path):
    captured = {}

    async def fake_provision(**kwargs):
        captured.update(kwargs)
        return "app-id", "x_oauth"

    monkeypatch.setattr(x_app, "provision_platform_app", fake_provision)
    monkeypatch.setattr(
        x_app,
        "get_settings",
        lambda: type("Settings", (), {"account_secrets_root": tmp_path})(),
    )

    result = await x_app.ensure_x_platform_app(
        tenant_id="tenant-a",
        consumer_key="key",
        consumer_secret="secret",
    )

    assert result == ("app-id", "x_oauth")
    assert captured["platform_family"] == "x"
    assert captured["external_app_id"] == "key"
    assert captured["tenant_id"] == "tenant-a"
    assert captured["public_id"] == "x_oauth_tenant-a"
    assert captured["credential_bundle"] == {
        "consumer_key": "key",
        "consumer_secret": "secret",
    }
    assert captured["allow_external_app_id_rotation"] is True
