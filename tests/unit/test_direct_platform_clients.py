import json

import httpx

from social_reply.connectors.meta.client import MetaGraphClient
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
        external_account_id="page-1",
        transport=httpx.MockTransport(handler),
    )
    assert (
        await client.send_text(target={"kind": "dm", "recipient_id": "user-1"}, text="hi")
        == "m-1"
    )
    assert (
        await client.send_text(target={"kind": "comment", "comment_id": "c-1"}, text="ok")
        == "m-2"
    )
    assert requests[0].url.path.endswith("/page-1/messages")
    assert json.loads(requests[1].content) == {"message": "ok"}
    await client.aclose()


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
        await client.send_text(target={"kind": "dm", "participant_id": "u1"}, text="hi")
        == "dm-1"
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
