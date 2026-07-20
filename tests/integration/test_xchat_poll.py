import base64
import uuid

import pytest
from chat_xdk import Chat
from sqlalchemy import insert, select

from social_reply.application.event_ingestion import xchat_poll
from social_reply.infrastructure.database import models
from social_reply.infrastructure.secret_crypto import encrypt_secret_bundle

pytestmark = pytest.mark.integration

_SELF = "1740258119773458432"
_PEER = "1831625034739412993"
_CONVERSATION = f"{_SELF}-{_PEER}"


async def _seed_account(session, *, bootstrapped: bool) -> uuid.UUID:
    chat = Chat()
    chat.generate_keypairs()
    exported = chat.export_keys()
    private_keys = base64.b64encode(bytes(exported)).decode()
    exported[:] = b"\x00" * len(exported)
    account_id = uuid.uuid4()
    config = {
        "delivery_mode": "direct",
        "xchat_enabled": True,
        "xchat_bootstrapped": {_CONVERSATION: bootstrapped},
        "xchat_cursors": {_CONVERSATION: "100"} if bootstrapped else {},
    }
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            tenant_id="default",
            brand_id="b1",
            platform="x",
            name="x-bot",
            external_account_id=_SELF,
            public_id="primary",
            credential_bundle=encrypt_secret_bundle(
                {
                    "consumer_key": "ck",
                    "consumer_secret": "cs",
                    "access_token": "at",
                    "access_token_secret": "ats",
                    "xchat_private_keys_b64": private_keys,
                    "xchat_signing_key_version": "7",
                }
            ),
            webhook_secret_bundle=encrypt_secret_bundle({"consumer_secret": "cs"}),
            config=config,
            capability={"dm": True, "x_chat": True},
            automation_default="BOT_ACTIVE",
            status="active",
        )
    )
    await session.commit()
    return account_id


async def test_xchat_first_empty_poll_bootstraps(session, monkeypatch):
    account_id = await _seed_account(session, bootstrapped=False)

    async def fake_conversations(self, *, max_results=100, pagination_token=None):
        return ([{"id": _CONVERSATION, "participant_ids": [_PEER], "type": "direct"}], None)

    async def fake_events(self, conversation_id, *, pagination_token=None):
        return ([], [], None)

    monkeypatch.setattr(xchat_poll.XChatClient, "read_conversations", fake_conversations)
    monkeypatch.setattr(xchat_poll.XChatClient, "read_conversation_events", fake_events)
    xchat_poll._last_poll_at = 0.0

    assert await xchat_poll.poll_xchat_messages() == []
    session.expire_all()
    account = await session.get(models.PlatformAccount, account_id)
    assert account.config["xchat_cursors"].get(_CONVERSATION) is None
    assert account.config["xchat_bootstrapped"][_CONVERSATION] is True


async def test_xchat_poll_ingests_decrypted_text(session, monkeypatch):
    await _seed_account(session, bootstrapped=True)

    async def fake_conversations(self, *, max_results=100, pagination_token=None):
        return ([{"id": _CONVERSATION, "participant_ids": [_PEER], "type": "direct"}], None)

    async def fake_events(self, conversation_id, *, pagination_token=None):
        return (
            [
                {
                    "id": "200",
                    "sender_id": _PEER,
                    "conversation_id": _CONVERSATION.replace("-", ":"),
                    "conversation_token": "token",
                    "created_at": "2026-07-20T01:51:31Z",
                    "encoded_event": "cipher",
                }
            ],
            ["key-change"],
            None,
        )

    async def fake_public_keys(self, user_id):
        return []

    def fake_decrypt(**kwargs):
        envelope = kwargs["message_events"][0]
        return (
            [
                {
                    "envelope": envelope,
                    "event": {
                        "type": "Message",
                        "message_id": "message-200",
                        "sender_id": _PEER,
                        "content": {"content_type": "Text", "text": "follow-up"},
                        "verified": True,
                    },
                }
            ],
            {"keys": {"1": b"0" * 32}, "latest_version": "1"},
            {},
        )

    monkeypatch.setattr(xchat_poll.XChatClient, "read_conversations", fake_conversations)
    monkeypatch.setattr(xchat_poll.XChatClient, "read_conversation_events", fake_events)
    monkeypatch.setattr(xchat_poll.XChatClient, "get_user_public_keys", fake_public_keys)
    monkeypatch.setattr(xchat_poll, "decrypt_history", fake_decrypt)
    xchat_poll._last_poll_at = 0.0

    assert await xchat_poll.poll_xchat_messages() == ["message-200"]
    normalized = (
        await session.execute(
            select(models.NormalizedEvent).where(
                models.NormalizedEvent.external_event_id == "message-200"
            )
        )
    ).scalar_one()
    message = await session.get(models.Message, normalized.message_id)
    conversation = await session.get(models.Conversation, normalized.conversation_id)
    assert message.text == "follow-up"
    assert conversation.conversation_key.startswith("x_chat:")


async def test_first_xchat_backfill_only_replies_to_newest_recent_inbound(
    session, monkeypatch
):
    await _seed_account(session, bootstrapped=False)

    async def fake_conversations(self, *, max_results=100, pagination_token=None):
        return ([{"id": _CONVERSATION, "participant_ids": [_PEER], "type": "direct"}], None)

    async def fake_events(self, conversation_id, *, pagination_token=None):
        return (
            [
                {
                    "id": "300",
                    "sender_id": _PEER,
                    "conversation_id": _CONVERSATION.replace("-", ":"),
                    "created_at": "2099-07-20T01:53:00Z",
                    "encoded_event": "third",
                },
                {
                    "id": "200",
                    "sender_id": _PEER,
                    "conversation_id": _CONVERSATION.replace("-", ":"),
                    "created_at": "2099-07-20T01:52:00Z",
                    "encoded_event": "second",
                },
                {
                    "id": "100",
                    "sender_id": _PEER,
                    "conversation_id": _CONVERSATION.replace("-", ":"),
                    "created_at": "2099-07-20T01:51:00Z",
                    "encoded_event": "first",
                },
            ],
            ["key-change"],
            None,
        )

    async def fake_public_keys(self, user_id):
        return []

    def fake_decrypt(**kwargs):
        return (
            [
                {
                    "envelope": envelope,
                    "event": {
                        "type": "Message",
                        "message_id": f"message-{envelope['id']}",
                        "sender_id": _PEER,
                        "content": {"content_type": "Text", "text": envelope["encoded_event"]},
                        "verified": True,
                    },
                }
                for envelope in kwargs["message_events"]
            ],
            {},
            {},
        )

    monkeypatch.setattr(xchat_poll.XChatClient, "read_conversations", fake_conversations)
    monkeypatch.setattr(xchat_poll.XChatClient, "read_conversation_events", fake_events)
    monkeypatch.setattr(xchat_poll.XChatClient, "get_user_public_keys", fake_public_keys)
    monkeypatch.setattr(xchat_poll, "decrypt_history", fake_decrypt)
    xchat_poll._last_poll_at = 0.0

    assert await xchat_poll.poll_xchat_messages() == ["message-300"]


async def test_first_xchat_backfill_skips_when_latest_event_is_ours(session, monkeypatch):
    await _seed_account(session, bootstrapped=False)

    async def fake_conversations(self, *, max_results=100, pagination_token=None):
        return ([{"id": _CONVERSATION, "participant_ids": [_PEER], "type": "direct"}], None)

    async def fake_events(self, conversation_id, *, pagination_token=None):
        return (
            [
                {
                    "id": "200",
                    "sender_id": _SELF,
                    "conversation_id": _CONVERSATION.replace("-", ":"),
                    "created_at": "2099-07-20T01:52:00Z",
                    "encoded_event": "our reply",
                },
                {
                    "id": "100",
                    "sender_id": _PEER,
                    "conversation_id": _CONVERSATION.replace("-", ":"),
                    "created_at": "2099-07-20T01:51:00Z",
                    "encoded_event": "question",
                },
            ],
            ["key-change"],
            None,
        )

    async def fake_public_keys(self, user_id):
        return []

    def fake_decrypt(**kwargs):
        return (
            [
                {
                    "envelope": envelope,
                    "event": {
                        "type": "Message",
                        "message_id": f"message-{envelope['id']}",
                        "sender_id": envelope["sender_id"],
                        "content": {"content_type": "Text", "text": envelope["encoded_event"]},
                        "verified": True,
                    },
                }
                for envelope in kwargs["message_events"]
            ],
            {},
            {},
        )

    monkeypatch.setattr(xchat_poll.XChatClient, "read_conversations", fake_conversations)
    monkeypatch.setattr(xchat_poll.XChatClient, "read_conversation_events", fake_events)
    monkeypatch.setattr(xchat_poll.XChatClient, "get_user_public_keys", fake_public_keys)
    monkeypatch.setattr(xchat_poll, "decrypt_history", fake_decrypt)
    xchat_poll._last_poll_at = 0.0

    assert await xchat_poll.poll_xchat_messages() == []
