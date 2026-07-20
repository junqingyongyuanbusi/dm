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
