from types import SimpleNamespace

from fastapi import HTTPException

from social_reply.connectors.x import router


def test_ingress_plan_drops_legacy_dm_when_disabled():
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

    assert filtered == []
    assert status == "IGNORED_X_LEGACY_DISABLED"
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


_MENTION_PAYLOAD = {
    "data": {
        "event_uuid": "2080765813578191303",
        "filter": {"user_id": "bot-1"},
        "event_type": "post.mention.create",
        "tag": "mentions",
        "payload": {
            "id": "2080765813578191303",
            "conversation_id": "2080000000000000000",
            "author_id": "user-9",
            "text": "hey @ExampleUser how do I avoid scams?",
            "created_at": "2026-07-24T21:23:23.000Z",
            "reply_settings": "everyone",
            "lang": "en",
        },
    }
}


def _adapter():
    return router.XWebhookAdapter(account_id="account-1", external_account_id="bot-1")


def test_mention_normalizes_to_a_public_reply_target():
    events, status = _adapter().normalize_activity_mention(_MENTION_PAYLOAD)
    assert status == "PENDING"
    (event,) = events
    assert event.channel_type == "mention"
    assert event.external_event_id == "2080765813578191303"
    assert event.external_user_id == "user-9"
    assert event.reply_target == {
        "kind": "reply",
        "in_reply_to_post_id": "2080765813578191303",
    }
    assert event.event_namespace == "x.activity.post_mention_create"


def test_mention_conversation_key_follows_the_thread_not_the_post():
    # 同一 thread 的两条 mention 必须落进同一会话，否则多轮对话拿不到上下文
    second = {
        "data": {
            **_MENTION_PAYLOAD["data"],
            "payload": {
                **_MENTION_PAYLOAD["data"]["payload"],
                "id": "2080765813578199999",
            },
        }
    }
    (first_event,), _ = _adapter().normalize_activity_mention(_MENTION_PAYLOAD)
    (second_event,), _ = _adapter().normalize_activity_mention(second)
    assert first_event.conversation_key == second_event.conversation_key
    assert first_event.conversation_key.endswith(":2080000000000000000")
    # 但回复目标各自指向自己那条帖子
    assert (
        first_event.reply_target["in_reply_to_post_id"]
        != (second_event.reply_target["in_reply_to_post_id"])
    )


def test_mention_by_the_bot_itself_is_ignored():
    payload = {
        "data": {
            **_MENTION_PAYLOAD["data"],
            "payload": {**_MENTION_PAYLOAD["data"]["payload"], "author_id": "bot-1"},
        }
    }
    events, status = _adapter().normalize_activity_mention(payload)
    assert events == []
    assert status == "IGNORED_SELF_MENTION"


def test_mention_without_text_is_rejected_as_unsupported_schema():
    payload = {
        "data": {
            **_MENTION_PAYLOAD["data"],
            "payload": {**_MENTION_PAYLOAD["data"]["payload"], "text": "   "},
        }
    }
    events, status = _adapter().normalize_activity_mention(payload)
    assert events == []
    assert status == "X_ACTIVITY_MENTION_SCHEMA_UNSUPPORTED"


def test_mention_is_dropped_when_public_reply_is_disabled():
    events, activity_status = _adapter().normalize_activity_mention(_MENTION_PAYLOAD)
    filtered, status, dispatch_xchat = router._ingress_plan(
        _MENTION_PAYLOAD,
        "post.mention.create",
        events,
        legacy_enabled=True,
        xchat_enabled=True,
        mention_enabled=False,
        activity_status=activity_status,
    )
    assert filtered == []
    assert status == "IGNORED_X_PUBLIC_REPLY_DISABLED"
    assert dispatch_xchat is False


def test_mention_is_ingested_when_public_reply_is_enabled():
    events, activity_status = _adapter().normalize_activity_mention(_MENTION_PAYLOAD)
    filtered, status, dispatch_xchat = router._ingress_plan(
        _MENTION_PAYLOAD,
        "post.mention.create",
        events,
        legacy_enabled=True,
        xchat_enabled=True,
        mention_enabled=True,
        activity_status=activity_status,
    )
    assert [e.reply_target["kind"] for e in filtered] == ["reply"]
    assert status == "PENDING"
    assert dispatch_xchat is False


def test_other_post_events_are_ignored_even_when_mentions_are_enabled():
    # 订阅只申请 post.mention.create，但 webhook 是 App 级共享的，别的 post 事件也可能到达
    filtered, status, _ = router._ingress_plan(
        {"data": {"event_type": "post.create"}},
        "post.create",
        [],
        legacy_enabled=True,
        xchat_enabled=True,
        mention_enabled=True,
    )
    assert filtered == []
    assert status == "IGNORED_X_ACTIVITY_EVENT"
