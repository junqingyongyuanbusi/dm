import sys
from types import SimpleNamespace

import httpx
import pytest

from social_reply.connectors.xchat.key_event_memory import ConversationKeyEventCache
from social_reply.connectors.xchat.sender import XChatSender


def test_cache_evicts_least_recently_used_conversation_without_mutating_events():
    cache = ConversationKeyEventCache(max_conversations=2, max_bytes=4096)
    original_events = ["first-key", "second-key"]
    cache.store("100:200", original_events)
    cache.store("100:300", ["other-key"])

    returned_events = cache.get("100:200")
    returned_events.append("caller-only")
    cache.store("100:400", ["new-key"])

    assert cache.get("100:200") == original_events
    assert cache.get("100:300") == []
    assert cache.get("100:400") == ["new-key"]
    assert original_events == ["first-key", "second-key"]


def test_cache_evicts_by_retained_event_size_not_only_conversation_count():
    event = "key-event" * 100
    entry_bytes = sys.getsizeof("100:200") + sys.getsizeof((event,)) + sys.getsizeof(event)
    cache = ConversationKeyEventCache(max_conversations=100, max_bytes=entry_bytes)
    cache.store("100:200", [event])
    cache.store("100:300", [event])

    assert cache.get("100:200") == []
    assert cache.get("100:300") == [event]


def test_oversized_replacement_is_not_cached_or_truncated():
    cache = ConversationKeyEventCache(max_conversations=2, max_bytes=512)
    cache.store("100:200", ["old-key"])
    oversized_events = ["new-key" * 1000, "historical-key"]
    cache.store("100:200", oversized_events)

    assert cache.get("100:200") == []
    assert oversized_events == ["new-key" * 1000, "historical-key"]


def test_replacement_reclaims_size_budget_and_empty_results_are_not_cached():
    cache = ConversationKeyEventCache(max_conversations=2, max_bytes=512)
    for revision in range(20):
        cache.store("100:200", [f"key-{revision}"])
    cache.store("100:300", ["other-key"])

    assert cache.get("100:200") == ["key-19"]
    assert cache.get("100:300") == ["other-key"]
    cache.store("100:200", [])
    assert cache.get("100:200") == []


@pytest.mark.parametrize("persisted", [True, False])
async def test_evicted_conversation_reloads_complete_keys_before_sending(monkeypatch, persisted):
    from social_reply.infrastructure.database import engine

    required_events = ["historical-key", "current-key"]
    database_reads = []
    provider_reads = []
    extracted_events = []
    durable_config = {"xchat_conversation_key_events": {"100:200": required_events}}

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *arguments):
            return None

        async def scalar(self, statement):
            database_reads.append(statement)
            return durable_config if persisted else {}

    class FakeChat:
        def set_key_version(self, version):
            assert version == "2"

        def extract_conversation_keys(self, events):
            extracted_events.append(list(events))
            return {"keys": {"2": b"test-key"}, "latest_version": "2"}

        def encrypt_message(self, *arguments):
            return SimpleNamespace(encrypted_content="encrypted", encoded_event_signature="signed")

    def handle_request(request):
        if request.method == "GET":
            provider_reads.append(request.url.path)
            return httpx.Response(200, json={"meta": {"conversation_key_events": required_events}})
        return httpx.Response(201, json={"data": {"id": "sent-message"}})

    monkeypatch.setattr(engine, "get_session_factory", lambda: FakeSession)
    monkeypatch.setattr(
        "social_reply.connectors.xchat.sender.import_private_key_b64", lambda value: FakeChat()
    )
    initial_events = {
        "100:200": required_events,
        **{f"100:{recipient}": ["unrelated-key"] for recipient in range(300, 428)},
    }
    sender = XChatSender(
        consumer_key="test-consumer",
        consumer_secret="test-consumer-secret",
        access_token="test-access",
        access_token_secret="test-access-secret",
        external_account_id="100",
        private_keys_b64="test-private",
        signing_key_version="2",
        conversation_key_events=initial_events,
        transport=httpx.MockTransport(handle_request),
    )
    try:
        assert sender._conversation_key_events.get("100:200") == []
        for _ in range(2):
            result = await sender.send_text(
                target={"kind": "x_chat", "conversation_id": "100-200"}, text="hello"
            )
            assert result == "sent-message"
    finally:
        await sender.aclose()

    assert len(database_reads) == 1
    assert len(provider_reads) == (0 if persisted else 1)
    assert extracted_events == [required_events, required_events]
    assert durable_config["xchat_conversation_key_events"]["100:200"] == required_events
    assert initial_events["100:200"] == required_events
