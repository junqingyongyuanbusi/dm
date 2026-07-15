import httpx

from social_reply.connectors.chatwoot.client import (
    FakeChatwootClient,
    HttpxChatwootClient,
)


async def test_fake_records_and_returns_incrementing_id():
    fake = FakeChatwootClient()
    mid1 = await fake.create_message(
        account_id=1, conversation_id=77, content="您好", private=False)
    mid2 = await fake.create_message(
        account_id=1, conversation_id=77, content="草稿", private=True)
    assert mid2 > mid1
    assert fake.sent[0] == {
        "account_id": 1, "conversation_id": 77, "content": "您好",
        "private": False, "id": mid1}
    assert fake.sent[1]["private"] is True


async def test_httpx_client_builds_correct_request():
    captured = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["token"] = request.headers.get("api_access_token")
        import json
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": 9001})

    transport = httpx.MockTransport(_handler)
    client = HttpxChatwootClient("http://cw.test", "tok-123", transport=transport)
    mid = await client.create_message(
        account_id=2, conversation_id=88, content="hi", private=False)
    assert mid == 9001
    assert captured["url"] == "http://cw.test/api/v1/accounts/2/conversations/88/messages"
    assert captured["token"] == "tok-123"
    assert captured["body"] == {"content": "hi", "message_type": "outgoing", "private": False}
