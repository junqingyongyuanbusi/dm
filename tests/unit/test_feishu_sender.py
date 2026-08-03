import json

import httpx
import pytest

from social_reply.connectors.errors import PermanentSendError, RetryableSendError
from social_reply.connectors.feishu.client import FeishuClient, FeishuClientError


def _target(**updates):
    return {
        "kind": "mention",
        "message_id": "om_1",
        "chat_id": "oc_1",
        "chat_type": "group",
        "sender_open_id": "ou_1",
        "thread_id": "omt_1",
        "uuid": "11111111-1111-1111-1111-111111111111",
        **updates,
    }


async def test_send_text_caches_token_and_emits_exact_unicode_thread_reply():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "tenant-1", "expire": 7200},
            )
        message_number = sum(request.url.path.endswith("/reply") for request in requests)
        return httpx.Response(200, json={"code": 0, "data": {"message_id": f"om_{message_number}"}})

    client = FeishuClient(
        app_id="cli_12345678",
        app_secret="app-secret",
        transport=httpx.MockTransport(handler),
    )
    first = await client.send_text(target=_target(), text="您好，世界")
    second = await client.send_text(target=_target(message_id="om_2"), text="再见")
    await client.aclose()

    assert (first, second) == ("om_1", "om_2")
    assert len([request for request in requests if "tenant_access_token" in request.url.path]) == 1
    reply = requests[1]
    assert reply.method == "POST"
    assert reply.url.path == "/open-apis/im/v1/messages/om_1/reply"
    assert reply.headers["authorization"] == "Bearer tenant-1"
    assert b"\\u60a8" not in reply.content
    assert json.loads(reply.content) == {
        "msg_type": "text",
        "content": json.dumps({"text": "您好，世界"}, ensure_ascii=False, separators=(",", ":")),
        "uuid": "11111111-1111-1111-1111-111111111111",
        "reply_in_thread": True,
    }


async def test_send_text_refreshes_cached_token_before_monotonic_expiry():
    now = [100.0]
    tokens = iter(("tenant-1", "tenant-2"))
    authorization: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": next(tokens), "expire": 600},
            )
        authorization.append(request.headers["authorization"])
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_sent"}})

    client = FeishuClient(
        app_id="cli_12345678",
        app_secret="app-secret",
        transport=httpx.MockTransport(handler),
        clock=lambda: now[0],
    )
    await client.send_text(target=_target(thread_id=None, root_id=None), text="one")
    now[0] += 541
    await client.send_text(target=_target(thread_id=None, root_id=None), text="two")
    await client.aclose()

    assert authorization == ["Bearer tenant-1", "Bearer tenant-2"]


async def test_send_text_retries_token_invalid_once_with_same_uuid():
    token_calls = 0
    reply_bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls
        if request.url.path.endswith("/tenant_access_token/internal"):
            token_calls += 1
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "tenant_access_token": f"tenant-{token_calls}",
                    "expire": 7200,
                },
            )
        reply_bodies.append(json.loads(request.content))
        if len(reply_bodies) == 1:
            return httpx.Response(200, json={"code": 99991663})
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_sent"}})

    client = FeishuClient(
        app_id="cli_12345678",
        app_secret="app-secret",
        transport=httpx.MockTransport(handler),
    )
    assert await client.send_text(target=_target(), text="hello") == "om_sent"
    await client.aclose()

    assert token_calls == 2
    assert len(reply_bodies) == 2
    assert reply_bodies[0]["uuid"] == reply_bodies[1]["uuid"]


@pytest.mark.parametrize(
    ("response", "error_type", "code"),
    [
        (httpx.Response(429), RetryableSendError, "FEISHU_API_429"),
        (httpx.Response(200, json={"code": 99991400}), RetryableSendError, "FEISHU_API_99991400"),
        (httpx.Response(400, json={"code": 230001}), PermanentSendError, "FEISHU_API_230001"),
        (httpx.Response(403), PermanentSendError, "FEISHU_API_HTTP_403"),
        (httpx.Response(503), httpx.HTTPStatusError, None),
        (httpx.Response(200, content=b"not-json"), FeishuClientError, "FEISHU_INVALID_RESPONSE"),
        (
            httpx.Response(200, json={"code": 0, "data": {}}),
            FeishuClientError,
            "FEISHU_MESSAGE_ID_MISSING",
        ),
    ],
)
async def test_send_text_classifies_reply_failures(response, error_type, code):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "tenant", "expire": 7200},
            )
        return response

    client = FeishuClient(
        app_id="cli_12345678",
        app_secret="credential-must-not-leak",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(error_type) as exc_info:
        await client.send_text(target=_target(), text="hello")
    await client.aclose()

    if code is not None:
        assert getattr(exc_info.value, "code", str(exc_info.value)) == code
    assert "credential-must-not-leak" not in str(exc_info.value)


async def test_token_acquisition_rejection_is_retryable_before_reply_dispatch():
    client = FeishuClient(
        app_id="cli_12345678",
        app_secret="app-secret",
        transport=httpx.MockTransport(lambda _request: httpx.Response(400, json={"code": 10003})),
    )
    with pytest.raises(RetryableSendError, match="FEISHU_API_10003"):
        await client.send_text(target=_target(), text="hello")
    await client.aclose()
