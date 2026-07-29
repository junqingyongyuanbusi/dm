import json
import uuid

import httpx
import pytest

from social_reply.application.platform_accounts import PlatformAccountRuntime
from social_reply.connectors import registry
from social_reply.connectors.errors import PermanentSendError, RetryableSendError
from social_reply.connectors.meta.client import MetaGraphClient, appsecret_proof
from social_reply.connectors.x.client import XClient
from social_reply.infrastructure.secrets import SecretStore


async def test_meta_client_sends_dm_and_comment():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"message_id": f"m-{len(requests)}"})

    client = MetaGraphClient(
        platform="facebook",
        access_token="token",
        app_secret="secret",
        external_account_id="page-1",
        transport=httpx.MockTransport(handler),
    )
    assert (
        await client.send_text(target={"kind": "dm", "recipient_id": "user-1"}, text="hi") == "m-1"
    )
    assert (
        await client.send_text(target={"kind": "comment", "comment_id": "c-1"}, text="ok") == "m-2"
    )
    assert requests[0].url.path.endswith("/page-1/messages")
    assert requests[0].url.params["appsecret_proof"] == appsecret_proof("token", "secret")
    assert json.loads(requests[1].content) == {"message": "ok"}
    # Facebook 回复评论是给评论加子评论
    assert requests[1].url.path.endswith("/c-1/comments")
    await client.aclose()


async def test_instagram_replies_to_comments_on_the_replies_edge():
    # IG 的回复端点是 POST /<IG_COMMENT_ID>/replies；沿用 Facebook 的 /comments
    # 会被 Graph 拒绝，而这条路径此前从未启用，所以一直没暴露。
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "ig-reply-1"})

    client = MetaGraphClient(
        platform="instagram",
        access_token="page-token",
        app_secret="secret",
        external_account_id="ig-1",
        page_id="page-1",
        transport=httpx.MockTransport(handler),
    )
    assert (
        await client.send_text(target={"kind": "comment", "comment_id": "igc-1"}, text="hi")
        == "ig-reply-1"
    )
    assert requests[0].url.path == "/v23.0/igc-1/replies"
    assert json.loads(requests[0].content) == {"message": "hi"}
    await client.aclose()


async def test_facebook_login_instagram_sends_dm_through_page_id():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"message_id": "mid-page"})

    client = MetaGraphClient(
        platform="instagram",
        access_token="page-token",
        app_secret="secret",
        external_account_id="ig-1",
        page_id="page-1",
        transport=httpx.MockTransport(handler),
    )
    assert (
        await client.send_text(target={"kind": "dm", "recipient_id": "igsid-1"}, text="hi")
        == "mid-page"
    )
    assert requests[0].url.path == "/v23.0/page-1/messages"
    assert requests[0].url.params["appsecret_proof"] == appsecret_proof("page-token", "secret")
    await client.aclose()


async def test_standalone_instagram_client_uses_instagram_graph_endpoints():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"user_id": "ig-1", "username": "shop"})
        return httpx.Response(200, json={"message_id": "mid-1"})

    client = MetaGraphClient(
        platform="instagram",
        access_token="token",
        app_secret="secret",
        external_account_id="ig-1",
        graph_base_url="https://graph.instagram.com",
        instagram_login_mode="instagram_login",
        transport=httpx.MockTransport(handler),
    )
    assert await client.get_account() == {
        "user_id": "ig-1",
        "username": "shop",
        "id": "ig-1",
        "name": "shop",
    }
    assert (
        await client.send_text(target={"kind": "dm", "recipient_id": "igsid-1"}, text="hi")
        == "mid-1"
    )
    assert requests[0].url.host == "graph.instagram.com"
    assert requests[0].url.path == "/v23.0/me"
    assert requests[0].url.params["appsecret_proof"] == appsecret_proof("token", "secret")
    assert requests[1].url.path == "/v23.0/ig-1/messages"
    await client.aclose()


async def test_xchat_sender_is_not_built_when_globally_disabled(monkeypatch):
    account = PlatformAccountRuntime(
        id=uuid.uuid4(),
        tenant_id="default",
        brand_id="default",
        platform="x",
        platform_app_id=None,
        name="x",
        external_account_id="x-1",
        public_id="x-public",
        credential_bundle_data={},
        webhook_secret_bundle_data=None,
        config={"delivery_mode": "direct"},
        capability={"dm": True, "x_chat": True},
        config_version=1,
        automation_default="BOT_DRAFT_ONLY",
        status="active",
    )
    monkeypatch.setattr(
        type(account),
        "credential_bundle",
        property(
            lambda self: {
                "consumer_key": "ck",
                "consumer_secret": "cs",
                "access_token": "at",
                "access_token_secret": "ats",
                "xchat_private_keys_b64": "private",
                "xchat_signing_key_version": "7",
            }
        ),
    )
    monkeypatch.setattr(
        registry,
        "get_settings",
        lambda: type("Settings", (), {"xchat_enabled": False})(),
    )

    sender = registry._build_sender(account)
    assert isinstance(sender, XClient)
    await sender.aclose()


async def test_x_client_sends_dm_and_reply():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"data": {"dm_event_id": "dm-1"}})
        return httpx.Response(200, json={"data": {"id": "tweet-1"}})

    client = XClient(
        consumer_key="ck",
        consumer_secret="cs",
        access_token="at",
        access_token_secret="ats",
        transport=httpx.MockTransport(handler),
    )
    assert (
        await client.send_text(target={"kind": "dm", "participant_id": "u1"}, text="hi") == "dm-1"
    )
    assert (
        await client.send_text(target={"kind": "reply", "in_reply_to_post_id": "t0"}, text="ok")
        == "tweet-1"
    )
    assert requests[0].headers.get("authorization", "").startswith("OAuth ")
    # 回归防护：OAuth1 签名不得清空 JSON body（曾因 OAuth1Auth 导致 body 为空 → X 400）
    assert json.loads(requests[0].content) == {"text": "hi"}
    assert json.loads(requests[1].content) == {
        "text": "ok",
        "reply": {"in_reply_to_tweet_id": "t0"},
    }
    await client.aclose()


def test_secret_store_reads_json_bundle_and_plain_text(tmp_path):
    store = SecretStore()
    bundle = tmp_path / "bundle.json"
    bundle.write_text('{"access_token":"abc"}')
    plain = tmp_path / "plain"
    plain.write_text("token")
    assert store.read_mapping(bundle.as_uri(), fallback_key="access_token") == {
        "access_token": "abc"
    }
    assert store.read_mapping(plain.as_uri(), fallback_key="bot_token") == {"bot_token": "token"}


def _x_client(handler) -> XClient:
    return XClient(
        consumer_key="ck",
        consumer_secret="cs",
        access_token="at",
        access_token_secret="ats",
        transport=httpx.MockTransport(handler),
    )


def _meta_client(handler) -> MetaGraphClient:
    return MetaGraphClient(
        platform="facebook",
        access_token="token",
        app_secret="secret",
        external_account_id="page-1",
        transport=httpx.MockTransport(handler),
    )


async def test_x_349_cannot_dm_raises_permanent():
    """对方不收 DM(未关注/拉黑/关陌生人私信)是 X 侧确定性拒绝,重试无意义。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"errors": [{"code": 349, "message": "You cannot send messages to this user."}]},
        )

    client = _x_client(handler)
    with pytest.raises(PermanentSendError) as exc_info:
        await client.send_text(target={"kind": "dm", "participant_id": "u1"}, text="hi")
    assert exc_info.value.code == "X_CANNOT_DM_349"
    await client.aclose()


async def test_x_429_raises_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"title": "Too Many Requests"})

    client = _x_client(handler)
    with pytest.raises(RetryableSendError):
        await client.send_text(target={"kind": "dm", "participant_id": "u1"}, text="hi")
    await client.aclose()


async def test_x_5xx_stays_ambiguous_http_error():
    """5xx 时消息可能已创建,必须保持 HTTPStatusError 让投递层按歧义处理,不可吞成永久错。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"errors": [{"code": 349}]})

    client = _x_client(handler)
    with pytest.raises(httpx.HTTPStatusError):
        await client.send_text(target={"kind": "dm", "participant_id": "u1"}, text="hi")
    await client.aclose()


async def test_meta_window_expired_raises_permanent():
    """Graph code 10 = 超出 24h 消息窗口:Meta 特有的确定性拒绝,须转 NEEDS_REVIEW 而非退避重试。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "code": 10,
                    "error_subcode": 2018278,
                    "message": "This message is sent outside of allowed window.",
                }
            },
        )

    client = _meta_client(handler)
    with pytest.raises(PermanentSendError) as exc_info:
        await client.send_text(target={"kind": "dm", "recipient_id": "user-1"}, text="hi")
    assert exc_info.value.code == "META_WINDOW_EXPIRED:2018278"
    await client.aclose()


async def test_unknown_meta_4xx_fails_permanently():
    def rejected(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"code": 2500, "error_subcode": 99, "message": "Bad request"}},
        )

    client = _meta_client(rejected)
    with pytest.raises(PermanentSendError) as exc_info:
        await client.send_text(target={"kind": "dm", "recipient_id": "user-1"}, text="hi")
    assert exc_info.value.code == "META_SEND_REJECTED_2500:99"
    await client.aclose()


async def test_meta_unreachable_and_rate_limit_classified():
    def unreachable(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"error": {"code": 551, "message": "Person is not available"}}
        )

    def limited(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"code": 613, "message": "Calls rate limited"}})

    client = _meta_client(unreachable)
    with pytest.raises(PermanentSendError) as exc_info:
        await client.send_text(target={"kind": "dm", "recipient_id": "user-1"}, text="hi")
    assert exc_info.value.code == "META_SEND_REJECTED_551"
    await client.aclose()

    client = _meta_client(limited)
    with pytest.raises(RetryableSendError):
        await client.send_text(target={"kind": "dm", "recipient_id": "user-1"}, text="hi")
    await client.aclose()
