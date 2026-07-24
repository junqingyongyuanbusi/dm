import httpx

from social_reply.application.account_management.meta_subscription import (
    get_meta_subscription_fields,
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
