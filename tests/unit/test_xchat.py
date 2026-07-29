import base64

import httpx
import pytest

from social_reply.connectors.xchat.adapter import canonical_from_decrypted
from social_reply.connectors.xchat.client import XChatClient
from social_reply.connectors.xchat.crypto import (
    export_private_key_b64,
    import_private_key_b64,
    signing_key_entries,
)
from social_reply.connectors.xchat.key_cache import canonical_conversation_id
from social_reply.connectors.xchat.sender import XChatSender
from social_reply.connectors.xchat.state import XChatKeyState, classify_xchat_state


def test_xchat_adapter_normalizes_verified_text_message():
    event = canonical_from_decrypted(
        account_id="account-1",
        external_account_id="bot-1",
        envelope={
            "id": "event-1",
            "conversation_id": "bot-1:user-1",
            "conversation_token": "token",
            "sender_id": "user-1",
            "created_at": "2026-07-20T01:51:31Z",
        },
        event={
            "type": "Message",
            "message_id": "message-1",
            "sender_id": "user-1",
            "content": {"content_type": "Text", "text": "hello from xchat"},
            "verified": True,
        },
    )
    assert event is not None
    assert event.external_event_id == "message-1"
    assert event.conversation_key == "x_chat:account-1:bot-1:user-1"
    assert event.reply_target == {
        "kind": "x_chat",
        "conversation_id": "bot-1:user-1",
        "conversation_token": "token",
    }


def test_xchat_adapter_skips_self_non_text_and_unverified():
    envelope = {
        "id": "event-1",
        "conversation_id": "bot-1:user-1",
        "sender_id": "bot-1",
    }
    assert (
        canonical_from_decrypted(
            account_id="account-1",
            external_account_id="bot-1",
            envelope=envelope,
            event={
                "type": "Message",
                "sender_id": "bot-1",
                "content": {"content_type": "Text", "text": "echo"},
            },
        )
        is None
    )
    assert (
        canonical_from_decrypted(
            account_id="account-1",
            external_account_id="bot-1",
            envelope={**envelope, "sender_id": "user-1"},
            event={
                "type": "Message",
                "sender_id": "user-1",
                "content": {"content_type": "Media"},
                "verified": True,
            },
        )
        is None
    )
    assert (
        canonical_from_decrypted(
            account_id="account-1",
            external_account_id="bot-1",
            envelope={**envelope, "sender_id": "user-1"},
            event={
                "type": "Message",
                "sender_id": "user-1",
                "content": {"content_type": "Text", "text": "unverified"},
                "verified": False,
            },
        )
        is None
    )


def test_xchat_private_key_roundtrip():
    from chat_xdk import Chat

    original = Chat()
    original.generate_keypairs()
    encoded = export_private_key_b64(original)
    assert len(base64.b64decode(encoded)) == 64
    restored = import_private_key_b64(encoded)
    assert restored.has_identity_key()
    assert bytes(restored.export_keys()) == bytes(original.export_keys())


def test_xchat_state_requires_server_public_key_match():
    from chat_xdk import Chat

    chat = Chat()
    generated = chat.generate_keypairs()
    registration = generated.public_key
    private_keys = export_private_key_b64(chat)
    record = {
        "public_key_version": "17",
        "public_key": registration.public_key,
        "signing_public_key": registration.signing_public_key,
        "juicebox_config": {"tokens": {}},
    }

    ready = classify_xchat_state([record], private_keys_b64=private_keys)
    assert ready.key_state is XChatKeyState.READY
    assert ready.public_key_version == "17"

    recovery = classify_xchat_state([record], private_keys_b64=None)
    assert recovery.key_state is XChatKeyState.RECOVERY_REQUIRED

    invalid = classify_xchat_state([record], private_keys_b64="not-base64")
    assert invalid.key_state is XChatKeyState.INVALID

    missing = classify_xchat_state([], private_keys_b64=private_keys)
    assert missing.key_state is XChatKeyState.NOT_REGISTERED


def test_xchat_conversation_ids_use_canonical_colon_form():
    assert canonical_conversation_id("1740258119773458432-2041798240056598528") == (
        "1740258119773458432:2041798240056598528"
    )
    assert canonical_conversation_id("1740258119773458432:2041798240056598528") == (
        "1740258119773458432:2041798240056598528"
    )


def test_signing_key_entries_maps_api_fields():
    assert signing_key_entries(
        "user-1",
        [
            {
                "public_key_version": "7",
                "public_key": "identity",
                "signing_public_key": "signing",
                "identity_public_key_signature": "binding",
            }
        ],
    ) == [
        {
            "user_id": "user-1",
            "public_key_version": "7",
            "public_key": "signing",
            "identity_public_key": "identity",
            "identity_public_key_signature": "binding",
        }
    ]


@pytest.mark.asyncio
async def test_xchat_client_sends_json_body_for_subscription():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        import json

        assert request.headers["authorization"].startswith("OAuth ")
        assert json.loads(request.content) == {
            "event_type": "chat.received",
            "filter": {"user_id": "bot-1"},
            "tag": "test",
        }
        return httpx.Response(
            200,
            json={
                "data": {
                    "subscription": {
                        "subscription_id": "subscription-1",
                        "event_type": "chat.received",
                    }
                }
            },
        )

    client = XChatClient(
        consumer_key="ck",
        consumer_secret="cs",
        access_token="at",
        access_token_secret="ats",
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await client.create_received_subscription(user_id="bot-1", tag="test")
    finally:
        await client.aclose()
    assert result["data"]["subscription"]["subscription_id"] == "subscription-1"
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_xchat_client_creates_legacy_dm_activity_subscription():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert json.loads(request.content) == {
            "event_type": "dm.received",
            "filter": {"user_id": "bot-1"},
            "tag": "legacy",
            "webhook_id": "webhook-1",
        }
        return httpx.Response(200, json={"data": {"subscription_id": "dm-sub"}})

    client = XChatClient(
        consumer_key="ck",
        consumer_secret="cs",
        access_token="at",
        access_token_secret="ats",
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await client.create_activity_subscription(
            event_type="dm.received",
            user_id="bot-1",
            webhook_id="webhook-1",
            tag="legacy",
        )
    finally:
        await client.aclose()
    assert result["data"]["subscription_id"] == "dm-sub"


@pytest.mark.asyncio
async def test_xchat_history_converts_canonical_conversation_separator():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "data": [{"id": "event-1"}],
                "meta": {"conversation_key_events": ["key-event"]},
            },
        )

    client = XChatClient(
        consumer_key="ck",
        consumer_secret="cs",
        access_token="at",
        access_token_secret="ats",
        api_base_url="https://api.x.test",
        transport=httpx.MockTransport(handler),
    )
    try:
        events, key_events, next_token = await client.read_conversation_events("bot-1:user-1")
    finally:
        await client.aclose()

    assert len(seen) == 1
    assert seen[0].url.path == "/2/chat/conversations/bot-1-user-1/events"
    assert events == [{"id": "event-1"}]
    assert key_events == ["key-event"]
    assert next_token is None


@pytest.mark.asyncio
async def test_xchat_sender_restores_signing_key_version(monkeypatch):
    calls: list[tuple[str, str]] = []

    class FakeChat:
        def set_key_version(self, version):
            calls.append(("set_key_version", version))

        def extract_conversation_keys(self, events):
            calls.append(("extract_conversation_keys", events[0]))
            return {"keys": {"9": b"key"}, "latest_version": "9"}

        def encrypt_message(self, *args):
            assert calls[0] == ("set_key_version", "17")
            return type(
                "Payload",
                (),
                {"encrypted_content": "encrypted", "encoded_event_signature": "signature"},
            )()

    monkeypatch.setattr(
        "social_reply.connectors.xchat.sender.import_private_key_b64",
        lambda value: FakeChat(),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"data": [], "meta": {"conversation_key_events": ["event"]}},
            )
        return httpx.Response(201, json={"data": {"id": "sent-1"}})

    sender = XChatSender(
        consumer_key="ck",
        consumer_secret="cs",
        access_token="at",
        access_token_secret="ats",
        external_account_id="bot-1",
        private_keys_b64="private",
        signing_key_version="17",
        conversation_key_events={"bot-1:user-1": ["event"]},
        transport=httpx.MockTransport(handler),
    )
    try:
        sent = await sender.send_text(
            target={"kind": "x_chat", "conversation_id": "bot-1:user-1"},
            text="hello",
        )
    finally:
        await sender.aclose()
    assert sent == "sent-1"
    assert calls[0] == ("set_key_version", "17")
    assert calls[1] == ("extract_conversation_keys", "event")
