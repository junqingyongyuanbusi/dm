from types import SimpleNamespace

from fastapi import HTTPException

from social_reply.connectors.x import router


def test_ingress_plan_filters_disabled_legacy_dm_but_keeps_mentions():
    payload = {
        "direct_message_events": [
            {
                "id": "dm-1",
                "message_create": {
                    "sender_id": "user-1",
                    "message_data": {"text": "dm"},
                },
            }
        ],
        "tweet_create_events": [{"id_str": "post-1", "user_id_str": "user-1", "text": "mention"}],
    }
    events = router.XWebhookAdapter(
        account_id="account-1",
        external_account_id="bot-1",
    ).normalize(payload)

    filtered, status, dispatch_xchat = router._ingress_plan(
        payload,
        "",
        events,
        legacy_enabled=False,
        xchat_enabled=True,
    )

    assert [event.reply_target["kind"] for event in filtered] == ["reply"]
    assert status == "PENDING"
    assert dispatch_xchat is False


def test_ingress_plan_routes_activity_dm_without_xchat_decryption():
    payload = {
        "data": {
            "event_type": "dm.received",
            "payload": {"id": "dm-1", "sender_id": "user-1", "text": "hello"},
        }
    }
    events, activity_status = router.XWebhookAdapter(
        account_id="account-1",
        external_account_id="bot-1",
    ).normalize_activity_dm(payload)

    planned, status, dispatch_xchat = router._ingress_plan(
        payload,
        "dm.received",
        events,
        legacy_enabled=True,
        xchat_enabled=True,
        activity_status=activity_status,
    )

    assert [event.external_event_id for event in planned] == ["dm-1"]
    assert status == "PENDING"
    assert dispatch_xchat is False


def test_ingress_plan_drops_disabled_xchat_before_dispatch():
    events, status, dispatch_xchat = router._ingress_plan(
        {"data": {"event_type": "chat.received"}},
        "chat.received",
        [],
        legacy_enabled=True,
        xchat_enabled=False,
    )

    assert events == []
    assert status == "IGNORED_XCHAT_DISABLED"
    assert dispatch_xchat is False


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
