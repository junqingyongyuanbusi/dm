from types import SimpleNamespace

from fastapi import HTTPException

from social_reply.connectors.x import router


async def test_x_app_webhook_routes_activity_event_to_authorized_account(monkeypatch):
    app = SimpleNamespace(id="app-id", credential_bundle={"consumer_secret": "secret"})
    account = SimpleNamespace(id="account-id")
    calls = []

    async def fake_app(**kwargs):
        return app

    async def fake_account(**kwargs):
        calls.append(kwargs)
        return account

    monkeypatch.setattr(router, "find_platform_app_by_public_id", fake_app)
    monkeypatch.setattr(router, "find_platform_account_by_external_id", fake_account)

    result = await router._event_account(
        "x_oauth",
        {"data": {"filter": {"user_id": "user-1"}}},
    )

    assert result is account
    assert calls == [
        {
            "platform": "x",
            "external_account_id": "user-1",
            "platform_app_id": "app-id",
        }
    ]


async def test_x_app_webhook_rejects_unroutable_event(monkeypatch):
    app = SimpleNamespace(id="app-id", credential_bundle={"consumer_secret": "secret"})

    async def fake_app(**kwargs):
        return app

    monkeypatch.setattr(router, "find_platform_app_by_public_id", fake_app)

    assert await router._event_account("x_oauth", {"data": {}}) is None


async def test_unknown_x_webhook_has_no_secret(monkeypatch):
    async def missing(**kwargs):
        return None

    monkeypatch.setattr(router, "find_platform_app_by_public_id", missing)
    monkeypatch.setattr(router, "find_platform_account_by_public_id", missing)

    try:
        await router._webhook_secret("missing")
    except HTTPException as exc:
        assert exc.status_code == 404
    else:  # pragma: no cover
        raise AssertionError("missing webhook should fail")
