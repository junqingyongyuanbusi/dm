import json

import httpx

from social_reply.connectors.telegram.adapter import TelegramWebhookAdapter
from social_reply.connectors.telegram.client import TelegramClient


def test_telegram_adapter_verifies_and_normalizes_message():
    adapter = TelegramWebhookAdapter(account_id="account-1", secret="secret-1")
    assert adapter.verify(headers={"x-telegram-bot-api-secret-token": "secret-1"}, body=b"{}")
    events = adapter.normalize(
        {
            "update_id": 100,
            "message": {
                "message_id": 55,
                "date": 1784180000,
                "from": {"id": 9},
                "chat": {"id": 77, "type": "private"},
                "text": "Hello",
            },
        }
    )
    assert len(events) == 1
    event = events[0]
    assert event.external_event_id == "100"
    assert event.conversation_key == "telegram:account-1:77"
    assert event.reply_target == {"chat_id": 77}
    assert event.text == "Hello"


async def test_telegram_client_send_message_and_set_webhook():
    requests: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.url.path, json.loads(request.content)))
        if request.url.path.endswith("/sendMessage"):
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 9001}})
        return httpx.Response(200, json={"ok": True, "result": True})

    client = TelegramClient(
        token="token",
        api_base_url="https://api.telegram.test",
        transport=httpx.MockTransport(handler),
    )
    message_id = await client.send_text(target={"chat_id": 77}, text="Hi")
    await client.set_webhook(
        url="https://example.test/webhooks/telegram/primary",
        secret_token="secret-1",
    )
    assert message_id == "9001"
    assert requests[0] == (
        "/bottoken/sendMessage",
        {"chat_id": 77, "text": "Hi"},
    )
    assert requests[1][0] == "/bottoken/setWebhook"
    assert requests[1][1]["secret_token"] == "secret-1"
    await client.aclose()
