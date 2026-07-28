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


def _variant(**payload_overrides):
    return {
        "data": {
            **_MENTION_PAYLOAD["data"],
            "payload": {**_MENTION_PAYLOAD["data"]["payload"], **payload_overrides},
        }
    }


def test_same_person_commenting_twice_shares_one_conversation():
    # 同一人在同一条帖下的多次评论要有上下文，否则每次回复都像失忆
    (first_event,), _ = _adapter().normalize_activity_mention(_MENTION_PAYLOAD)
    (second_event,), _ = _adapter().normalize_activity_mention(_variant(id="2080765813578199999"))
    assert first_event.conversation_key == second_event.conversation_key
    # 但回复各自指向对方那条评论，而不是都回到第一条
    assert (
        first_event.reply_target["in_reply_to_post_id"]
        != second_event.reply_target["in_reply_to_post_id"]
    )


def test_different_people_under_one_post_never_share_a_conversation():
    # 一条帖子下 50 个评论者共享同一个 conversation_id；只按 thread 建键会把
    # 陌生人并进同一会话，联系人归属和历史上下文都会错。
    (a,), _ = _adapter().normalize_activity_mention(_variant(id="post-a", author_id="user-a"))
    (b,), _ = _adapter().normalize_activity_mention(_variant(id="post-b", author_id="user-b"))
    assert a.external_conversation_id == b.external_conversation_id
    assert a.conversation_key != b.conversation_key


def test_reply_prefix_is_stripped_from_the_user_text():
    # X 会给回复自动拼上被回复者 handle，真实语料形如 "@handle 联系方式"
    (event,), _ = _adapter().normalize_activity_mention(
        _variant(text="@ExampleUser @someone_else 联系方式")
    )
    assert event.text == "联系方式"
    # 原文仍留在 raw_payload 里可追溯
    assert event.raw_payload["text"].startswith("@ExampleUser")


def test_bare_mention_without_body_keeps_the_original_text():
    (event,), _ = _adapter().normalize_activity_mention(_variant(text="@ExampleUser"))
    assert event.text == "@ExampleUser"


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
