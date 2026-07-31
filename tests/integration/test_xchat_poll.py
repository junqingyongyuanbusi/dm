import base64
import uuid

import pytest
from chat_xdk import Chat
from sqlalchemy import insert, select

from social_reply.application.event_ingestion import xchat_poll
from social_reply.infrastructure.database import models
from social_reply.infrastructure.secret_crypto import encrypt_secret_bundle
from social_reply.shared.config import get_settings

pytestmark = pytest.mark.integration

_SELF = "1740258119773458432"
_PEER = "1831625034739412993"
_CONVERSATION = f"{_SELF}:{_PEER}"


def _set_poll_settings(monkeypatch, *, interval: int = 0, max_conversations: int = 10) -> None:
    settings = get_settings().model_copy(
        update={
            "xchat_poll_interval_seconds": interval,
            "xchat_max_conversations_per_poll": max_conversations,
        }
    )
    monkeypatch.setattr(xchat_poll, "get_settings", lambda: settings)


async def _conversation_checkpoint(session, account_id: uuid.UUID):
    session.expire_all()
    return (
        await session.execute(
            select(models.PlatformCheckpoint).where(
                models.PlatformCheckpoint.platform_account_id == account_id,
                models.PlatformCheckpoint.stream == "XCHAT_CONVERSATION",
                models.PlatformCheckpoint.scope_key == _CONVERSATION,
            )
        )
    ).scalar_one()


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
    xchat_poll._last_poll_at = None

    assert await xchat_poll.poll_xchat_messages() == []
    checkpoint = await _conversation_checkpoint(session, account_id)
    assert checkpoint.cursor is None
    assert checkpoint.bootstrapped is True


async def test_xchat_poll_ingests_decrypted_text(session, monkeypatch):
    account_id = await _seed_account(session, bootstrapped=True)

    async def fake_conversations(self, *, max_results=100, pagination_token=None):
        return ([{"id": _CONVERSATION, "participant_ids": [_PEER], "type": "direct"}], None)

    async def fake_events(self, conversation_id, *, pagination_token=None):
        return (
            [
                {
                    "id": "200",
                    "sender_id": _PEER,
                    "conversation_id": _CONVERSATION,
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
    xchat_poll._last_poll_at = None

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
    assert normalized.external_conversation_id == _CONVERSATION
    assert normalized.event_metadata["event_namespace"] == "x.xchat"
    assert normalized.event_metadata["envelope_id"] == "200"
    message_raw = await session.get(models.RawEvent, normalized.raw_event_id)
    assert message_raw.source == "xchat_poll"
    assert message_raw.ingress_kind == "poll"
    assert message_raw.event_namespace == "x.xchat.message"
    assert message_raw.external_event_id == "200"
    assert message_raw.external_conversation_id == _CONVERSATION
    assert message_raw.payload["encoded_event"] == "cipher"
    assert message_raw.context["conversation"]["peer_id"] == _PEER
    assert message_raw.processing_status == "PROCESSED"
    key_raw = (
        await session.execute(
            select(models.RawEvent).where(models.RawEvent.event_namespace == "x.xchat.key_change")
        )
    ).scalar_one()
    assert key_raw.payload == {"key_change": "key-change"}
    assert key_raw.processing_status == "PROCESSED_KEY_MATERIAL"
    checkpoint = await _conversation_checkpoint(session, account_id)
    assert checkpoint.cursor == "200"
    assert checkpoint.bootstrapped is True


async def test_first_xchat_backfill_only_replies_to_newest_recent_inbound(session, monkeypatch):
    await _seed_account(session, bootstrapped=False)

    async def fake_conversations(self, *, max_results=100, pagination_token=None):
        return ([{"id": _CONVERSATION, "participant_ids": [_PEER], "type": "direct"}], None)

    async def fake_events(self, conversation_id, *, pagination_token=None):
        return (
            [
                {
                    "id": "300",
                    "sender_id": _PEER,
                    "conversation_id": _CONVERSATION,
                    "created_at": "2099-07-20T01:53:00Z",
                    "encoded_event": "third",
                },
                {
                    "id": "200",
                    "sender_id": _PEER,
                    "conversation_id": _CONVERSATION,
                    "created_at": "2099-07-20T01:52:00Z",
                    "encoded_event": "second",
                },
                {
                    "id": "100",
                    "sender_id": _PEER,
                    "conversation_id": _CONVERSATION,
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
    xchat_poll._last_poll_at = None

    assert await xchat_poll.poll_xchat_messages() == ["message-300"]
    raw_events = (
        (
            await session.execute(
                select(models.RawEvent).where(models.RawEvent.event_namespace == "x.xchat.message")
            )
        )
        .scalars()
        .all()
    )
    assert len(raw_events) == 3
    assert sorted(row.processing_status for row in raw_events) == [
        "IGNORED_BOOTSTRAP",
        "IGNORED_BOOTSTRAP",
        "PROCESSED",
    ]


async def test_xchat_decrypt_failure_keeps_raw_occurrence_and_cursor(session, monkeypatch):
    account_id = await _seed_account(session, bootstrapped=True)

    async def fake_conversations(self, *, max_results=100, pagination_token=None):
        return ([{"id": _CONVERSATION, "participant_ids": [_PEER], "type": "direct"}], None)

    async def fake_events(self, conversation_id, *, pagination_token=None):
        return (
            [
                {
                    "id": "200",
                    "sender_id": _PEER,
                    "conversation_id": _CONVERSATION,
                    "created_at": "2026-07-20T01:51:31Z",
                    "encoded_event": "cipher",
                }
            ],
            [],
            None,
        )

    async def fake_public_keys(self, user_id):
        return []

    decrypt_fails = True

    def fake_decrypt(**kwargs):
        if decrypt_fails:
            return [], {}, {0: "key"}
        envelope = kwargs["message_events"][0]
        return (
            [
                {
                    "envelope": envelope,
                    "event": {
                        "type": "Message",
                        "message_id": "message-200-recovered",
                        "sender_id": _PEER,
                        "content": {"content_type": "Text", "text": "recovered"},
                        "verified": True,
                    },
                }
            ],
            {},
            {},
        )

    monkeypatch.setattr(xchat_poll.XChatClient, "read_conversations", fake_conversations)
    monkeypatch.setattr(xchat_poll.XChatClient, "read_conversation_events", fake_events)
    monkeypatch.setattr(xchat_poll.XChatClient, "get_user_public_keys", fake_public_keys)
    monkeypatch.setattr(xchat_poll, "decrypt_history", fake_decrypt)
    _set_poll_settings(monkeypatch)
    xchat_poll._last_poll_at = None

    assert await xchat_poll.poll_xchat_messages() == []
    session.expire_all()
    checkpoint = await _conversation_checkpoint(session, account_id)
    raw_event = (
        await session.execute(
            select(models.RawEvent).where(models.RawEvent.external_event_id == "200")
        )
    ).scalar_one()
    gap = (
        await session.execute(
            select(models.SyncGap).where(models.SyncGap.checkpoint_id == checkpoint.id)
        )
    ).scalar_one()
    assert raw_event.processing_status == "XCHAT_DECRYPT_FAILED"
    assert raw_event.payload["encoded_event"] == "cipher"
    assert checkpoint.cursor == "100"
    assert gap.status == "OPEN"
    assert gap.gap_type == "DECRYPT_ERROR"
    gap_id = gap.id

    decrypt_fails = False
    xchat_poll._last_poll_at = None
    assert await xchat_poll.poll_xchat_messages() == ["message-200-recovered"]
    checkpoint = await _conversation_checkpoint(session, account_id)
    gap = await session.get(models.SyncGap, gap_id)
    assert checkpoint.cursor == "200"
    assert gap.status == "RESOLVED"
    assert (
        await session.scalar(
            select(models.NormalizedEvent).where(
                models.NormalizedEvent.external_event_id == "message-200-recovered"
            )
        )
    ) is not None


async def test_xchat_event_page_gap_resumes_without_advancing_early(session, monkeypatch):
    account_id = await _seed_account(session, bootstrapped=True)
    calls: list[str | None] = []

    async def fake_conversations(self, *, max_results=100, pagination_token=None):
        return ([{"id": _CONVERSATION, "participant_ids": [_PEER], "type": "direct"}], None)

    async def fake_events(self, conversation_id, *, pagination_token=None):
        calls.append(pagination_token)
        if pagination_token == "t3":
            return (
                [
                    {
                        "id": "50",
                        "sender_id": _PEER,
                        "conversation_id": _CONVERSATION,
                        "encoded_event": "old",
                    }
                ],
                [],
                None,
            )
        ids = {None: "400", "t1": "300", "t2": "200"}
        next_tokens = {None: "t1", "t1": "t2", "t2": "t3"}
        event_id = ids[pagination_token]
        return (
            [
                {
                    "id": event_id,
                    "sender_id": _PEER,
                    "conversation_id": _CONVERSATION,
                    "encoded_event": event_id,
                }
            ],
            [],
            next_tokens[pagination_token],
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
    _set_poll_settings(monkeypatch)
    xchat_poll._last_poll_at = None

    assert await xchat_poll.poll_xchat_messages() == [
        "message-200",
        "message-300",
        "message-400",
    ]
    checkpoint = await _conversation_checkpoint(session, account_id)
    gap = (
        await session.execute(
            select(models.SyncGap).where(models.SyncGap.checkpoint_id == checkpoint.id)
        )
    ).scalar_one()
    assert checkpoint.cursor == "100"
    assert gap.status == "OPEN"
    assert gap.gap_type == "PAGE_CAP"
    assert gap.resume_token == "t3"
    gap_id = gap.id

    xchat_poll._last_poll_at = None
    assert await xchat_poll.poll_xchat_messages() == []
    assert calls == [None, "t1", "t2", "t3"]
    checkpoint = await _conversation_checkpoint(session, account_id)
    gap = await session.get(models.SyncGap, gap_id)
    assert checkpoint.cursor == "400"
    assert gap.status == "RESOLVED"


async def test_xchat_discovery_page_gap_resumes_deeper_conversations(session, monkeypatch):
    account_id = await _seed_account(session, bootstrapped=True)
    calls: list[str | None] = []
    request_sizes: list[int] = []

    async def fake_conversations(self, *, max_results=100, pagination_token=None):
        calls.append(pagination_token)
        request_sizes.append(max_results)
        current = {None: "one", "t1": "two", "t2": "three", "t3": "deep"}[pagination_token]
        next_token = {None: "t1", "t1": "t2", "t2": "t3", "t3": None}[pagination_token]
        return ([{"id": current, "participant_ids": [_PEER], "type": "direct"}], next_token)

    async def fake_events(self, conversation_id, *, pagination_token=None):
        return [], [], None

    monkeypatch.setattr(xchat_poll.XChatClient, "read_conversations", fake_conversations)
    monkeypatch.setattr(xchat_poll.XChatClient, "read_conversation_events", fake_events)
    _set_poll_settings(monkeypatch, max_conversations=3)
    xchat_poll._last_poll_at = None

    assert await xchat_poll.poll_xchat_messages() == []
    discovery = (
        await session.execute(
            select(models.PlatformCheckpoint).where(
                models.PlatformCheckpoint.platform_account_id == account_id,
                models.PlatformCheckpoint.stream == "XCHAT_DISCOVERY",
            )
        )
    ).scalar_one()
    gap = (
        await session.execute(
            select(models.SyncGap).where(models.SyncGap.checkpoint_id == discovery.id)
        )
    ).scalar_one()
    assert calls == [None, "t1", "t2"]
    assert request_sizes == [3, 2, 1]
    assert gap.status == "OPEN"
    assert gap.resume_token == "t3"
    gap_id = gap.id

    xchat_poll._last_poll_at = None
    assert await xchat_poll.poll_xchat_messages() == []
    assert calls[-1] == "t3"
    deep = (
        await session.execute(
            select(models.PlatformCheckpoint).where(
                models.PlatformCheckpoint.platform_account_id == account_id,
                models.PlatformCheckpoint.stream == "XCHAT_CONVERSATION",
                models.PlatformCheckpoint.scope_key == "deep",
            )
        )
    ).scalar_one()
    deep_id = deep.id
    session.expire_all()
    gap = await session.get(models.SyncGap, gap_id)
    deep = await session.get(models.PlatformCheckpoint, deep_id)
    assert deep.bootstrapped is True
    assert gap.status == "RESOLVED"


async def test_expired_xchat_discovery_token_restarts_from_first_page(session, monkeypatch):
    account_id = await _seed_account(session, bootstrapped=True)
    calls: list[str | None] = []

    async def fake_conversations(self, *, max_results=100, pagination_token=None):
        calls.append(pagination_token)
        if pagination_token == "expired":
            raise RuntimeError("token expired")
        conversation_id = "first" if calls.count(None) == 1 else "deep"
        next_token = "expired" if calls.count(None) == 1 else None
        return ([{"id": conversation_id, "participant_ids": [_PEER], "type": "direct"}], next_token)

    async def fake_events(self, conversation_id, *, pagination_token=None):
        return [], [], None

    monkeypatch.setattr(xchat_poll.XChatClient, "read_conversations", fake_conversations)
    monkeypatch.setattr(xchat_poll.XChatClient, "read_conversation_events", fake_events)
    monkeypatch.setattr(xchat_poll, "_MAX_CONVERSATION_PAGES", 1)
    _set_poll_settings(monkeypatch)

    xchat_poll._last_poll_at = None
    assert await xchat_poll.poll_xchat_messages() == []
    discovery = (
        await session.execute(
            select(models.PlatformCheckpoint).where(
                models.PlatformCheckpoint.platform_account_id == account_id,
                models.PlatformCheckpoint.stream == "XCHAT_DISCOVERY",
            )
        )
    ).scalar_one()
    gap = (
        await session.execute(
            select(models.SyncGap).where(models.SyncGap.checkpoint_id == discovery.id)
        )
    ).scalar_one()
    gap_id = gap.id
    assert gap.resume_token == "expired"

    xchat_poll._last_poll_at = None
    assert await xchat_poll.poll_xchat_messages() == []
    session.expire_all()
    gap = await session.get(models.SyncGap, gap_id)
    assert gap.status == "OPEN"
    assert gap.resume_token is None
    assert gap.detail["restart_from_checkpoint"] is True

    xchat_poll._last_poll_at = None
    assert await xchat_poll.poll_xchat_messages() == []
    session.expire_all()
    gap = await session.get(models.SyncGap, gap_id)
    deep = await session.scalar(
        select(models.PlatformCheckpoint.id).where(
            models.PlatformCheckpoint.platform_account_id == account_id,
            models.PlatformCheckpoint.stream == "XCHAT_CONVERSATION",
            models.PlatformCheckpoint.scope_key == "deep",
        )
    )
    assert calls == [None, "expired", None]
    assert gap.status == "RESOLVED"
    assert deep is not None


async def test_expired_xchat_event_token_restarts_from_conversation_cursor(session, monkeypatch):
    account_id = await _seed_account(session, bootstrapped=True)
    calls: list[str | None] = []

    async def fake_conversations(self, *, max_results=100, pagination_token=None):
        return ([{"id": _CONVERSATION, "participant_ids": [_PEER], "type": "direct"}], None)

    async def fake_events(self, conversation_id, *, pagination_token=None):
        calls.append(pagination_token)
        if pagination_token == "expired":
            raise RuntimeError("token expired")
        return (
            [
                {
                    "id": "200",
                    "sender_id": _PEER,
                    "conversation_id": _CONVERSATION,
                    "encoded_event": "cipher",
                }
            ],
            [],
            "expired" if calls.count(None) == 1 else None,
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
                        "message_id": "message-200-expired-token",
                        "sender_id": _PEER,
                        "content": {"content_type": "Text", "text": "recover"},
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
    monkeypatch.setattr(xchat_poll, "_MAX_EVENT_PAGES", 1)
    _set_poll_settings(monkeypatch)

    xchat_poll._last_poll_at = None
    assert await xchat_poll.poll_xchat_messages() == ["message-200-expired-token"]
    checkpoint = await _conversation_checkpoint(session, account_id)
    gap = (
        await session.execute(
            select(models.SyncGap).where(models.SyncGap.checkpoint_id == checkpoint.id)
        )
    ).scalar_one()
    checkpoint_id = checkpoint.id
    gap_id = gap.id
    assert checkpoint.cursor == "100"
    assert gap.resume_token == "expired"

    xchat_poll._last_poll_at = None
    assert await xchat_poll.poll_xchat_messages() == []
    session.expire_all()
    checkpoint = await session.get(models.PlatformCheckpoint, checkpoint_id)
    gap = await session.get(models.SyncGap, gap_id)
    assert checkpoint.cursor == "100"
    assert gap.status == "OPEN"
    assert gap.resume_token is None

    xchat_poll._last_poll_at = None
    assert await xchat_poll.poll_xchat_messages() == []
    checkpoint = await _conversation_checkpoint(session, account_id)
    gap = await session.get(models.SyncGap, gap_id)
    assert calls == [None, "expired", None]
    assert checkpoint.cursor == "200"
    assert gap.status == "RESOLVED"


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
                    "conversation_id": _CONVERSATION,
                    "created_at": "2099-07-20T01:52:00Z",
                    "encoded_event": "our reply",
                },
                {
                    "id": "100",
                    "sender_id": _PEER,
                    "conversation_id": _CONVERSATION,
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
    xchat_poll._last_poll_at = None

    assert await xchat_poll.poll_xchat_messages() == []
