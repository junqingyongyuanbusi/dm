from urllib.parse import parse_qs

import httpx

from social_reply.application.account_management.meta_subscription import (
    get_meta_app_subscription,
    get_meta_subscription_fields,
    meta_app_subscription_object,
    meta_subscription_fields,
    reconcile_meta_app_subscription,
    subscribe_meta_account,
)
from social_reply.connectors.meta.client import appsecret_proof


async def test_facebook_subscription_installs_messages_and_feed():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"success": True})

    fields = await subscribe_meta_account(
        platform="facebook",
        access_token="page-token",
        app_secret="app-secret",
        external_account_id="page-1",
        instagram_login_mode="facebook_login",
        graph_base_url="https://graph.facebook.com",
        api_version="v23.0",
        enable_dm=True,
        enable_comments=True,
        transport=httpx.MockTransport(handler),
    )

    assert fields == ("messages", "feed")
    assert requests[0].url.host == "graph.facebook.com"
    assert requests[0].url.path == "/v23.0/page-1/subscribed_apps"
    assert requests[0].url.params["subscribed_fields"] == "messages,feed"
    assert requests[0].headers["authorization"] == "Bearer page-token"
    assert requests[0].url.params["appsecret_proof"] == appsecret_proof("page-token", "app-secret")


async def test_standalone_instagram_subscription_uses_instagram_graph_me():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"success": True})

    fields = await subscribe_meta_account(
        platform="instagram",
        access_token="ig-token",
        app_secret="app-secret",
        external_account_id="ig-1",
        instagram_login_mode="instagram_login",
        graph_base_url="https://graph.facebook.com",
        api_version="v23.0",
        enable_dm=True,
        enable_comments=False,
        transport=httpx.MockTransport(handler),
    )

    assert fields == ("messages",)
    assert requests[0].url.host == "graph.instagram.com"
    assert requests[0].url.path == "/v23.0/ig-1/subscribed_apps"
    assert requests[0].url.params["subscribed_fields"] == "messages"
    assert requests[0].url.params["appsecret_proof"] == appsecret_proof("ig-token", "app-secret")


async def test_reads_fields_for_the_current_meta_app():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "other-app", "subscribed_fields": ["feed"]},
                    {"id": "app-1", "subscribed_fields": ["messages"]},
                ]
            },
        )

    fields = await get_meta_subscription_fields(
        platform="facebook",
        access_token="page-token",
        app_secret="app-secret",
        app_id="app-1",
        external_account_id="page-1",
        instagram_login_mode="facebook_login",
        graph_base_url="https://graph.facebook.com",
        api_version="v23.0",
        transport=httpx.MockTransport(handler),
    )

    assert fields == ("messages",)
    assert requests[0].method == "GET"
    assert requests[0].url.path == "/v23.0/page-1/subscribed_apps"


def _app_subscription_handler(payload: dict, requests: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=payload)
        return httpx.Response(200, json={"success": True})

    return handler


async def test_app_subscription_object_maps_platform_to_webhook_object():
    assert meta_app_subscription_object("facebook") == "page"
    assert meta_app_subscription_object("instagram") == "instagram"


async def test_app_level_subscription_installs_fields_when_callback_has_none():
    # 生产事故形态：回调 URL 已注册且 active=true，但没有任何字段，Meta 静默丢弃全部事件。
    requests: list[httpx.Request] = []
    payload = {
        "data": [
            {
                "object": "page",
                "callback_url": "https://relay.example/webhooks/meta/app-pub",
                "active": True,
            }
        ]
    }

    fields = await reconcile_meta_app_subscription(
        app_id="app-1",
        app_secret="app-secret",
        object_type="page",
        desired_fields=("messages",),
        callback_url="https://relay.example/webhooks/meta/app-pub",
        verify_token="verify-token",
        api_version="v23.0",
        transport=httpx.MockTransport(_app_subscription_handler(payload, requests)),
    )

    assert fields == ("messages",)
    post = requests[1]
    assert post.method == "POST"
    assert post.url.path == "/v23.0/app-1/subscriptions"
    body = {k: v[0] for k, v in parse_qs(post.content.decode()).items()}
    assert body["object"] == "page"
    assert body["fields"] == "messages"
    assert body["verify_token"] == "verify-token"
    assert requests[0].url.params["access_token"] == "app-1|app-secret"


async def test_app_level_subscription_merges_fields_installed_by_other_accounts():
    # POST 会整体替换该 object 的订阅，直接写 desired 会抹掉同 App 上别的账号依赖的字段。
    requests: list[httpx.Request] = []
    payload = {
        "data": [
            {
                "object": "page",
                "callback_url": "https://relay.example/webhooks/meta/app-pub",
                "active": True,
                "fields": [{"name": "feed", "version": "v23.0"}],
            }
        ]
    }

    fields = await reconcile_meta_app_subscription(
        app_id="app-1",
        app_secret="app-secret",
        object_type="page",
        desired_fields=("messages",),
        callback_url="https://relay.example/webhooks/meta/app-pub",
        verify_token="verify-token",
        api_version="v23.0",
        transport=httpx.MockTransport(_app_subscription_handler(payload, requests)),
    )

    assert fields == ("feed", "messages")
    body = {k: v[0] for k, v in parse_qs(requests[1].content.decode()).items()}
    assert body["fields"] == "feed,messages"


async def test_app_level_subscription_is_a_noop_when_already_installed():
    requests: list[httpx.Request] = []
    payload = {
        "data": [
            {
                "object": "page",
                "callback_url": "https://relay.example/webhooks/meta/app-pub",
                "active": True,
                "fields": [{"name": "messages", "version": "v23.0"}],
            }
        ]
    }

    fields = await reconcile_meta_app_subscription(
        app_id="app-1",
        app_secret="app-secret",
        object_type="page",
        desired_fields=("messages",),
        callback_url="https://relay.example/webhooks/meta/app-pub",
        verify_token="verify-token",
        api_version="v23.0",
        transport=httpx.MockTransport(_app_subscription_handler(payload, requests)),
    )

    assert fields == ("messages",)
    assert [r.method for r in requests] == ["GET"]


async def test_app_level_subscription_reinstalls_when_callback_url_moved():
    requests: list[httpx.Request] = []
    payload = {
        "data": [
            {
                "object": "page",
                "callback_url": "https://old.example/webhooks/meta/app-pub",
                "active": True,
                "fields": [{"name": "messages", "version": "v23.0"}],
            }
        ]
    }

    await reconcile_meta_app_subscription(
        app_id="app-1",
        app_secret="app-secret",
        object_type="page",
        desired_fields=("messages",),
        callback_url="https://relay.example/webhooks/meta/app-pub",
        verify_token="verify-token",
        api_version="v23.0",
        transport=httpx.MockTransport(_app_subscription_handler(payload, requests)),
    )

    assert [r.method for r in requests] == ["GET", "POST"]


async def test_reading_app_subscription_returns_none_for_unsubscribed_object():
    requests: list[httpx.Request] = []
    payload = {"data": [{"object": "page", "callback_url": "https://x", "active": True}]}

    result = await get_meta_app_subscription(
        app_id="app-1",
        app_secret="app-secret",
        object_type="instagram",
        api_version="v23.0",
        transport=httpx.MockTransport(_app_subscription_handler(payload, requests)),
    )

    assert result is None


def test_instagram_facebook_login_never_asks_page_for_comments():
    # facebook_login 下订阅写在关联 Page 上，而 Page 的 subscribed_fields 枚举里
    # 没有 comments，提交会被 Graph 以 code 100 拒掉。IG 评论走 App 级 instagram 对象。
    assert meta_subscription_fields(
        platform="instagram",
        enable_dm=True,
        enable_comments=True,
        instagram_login_mode="facebook_login",
    ) == ("messages",)


def test_instagram_login_mode_still_subscribes_comments_directly():
    # instagram_login 走 graph.instagram.com/<IG_ID>/subscribed_apps，那里接受 comments
    assert meta_subscription_fields(
        platform="instagram",
        enable_dm=True,
        enable_comments=True,
        instagram_login_mode="instagram_login",
    ) == ("messages", "comments")


def test_facebook_comments_ride_the_feed_field():
    assert meta_subscription_fields(platform="facebook", enable_dm=True, enable_comments=True) == (
        "messages",
        "feed",
    )
