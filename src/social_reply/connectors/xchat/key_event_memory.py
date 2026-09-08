import sys
from collections import OrderedDict
from dataclasses import dataclass

_MAX_CACHED_CONVERSATIONS = 128
_MAX_CACHED_EVENT_BYTES = 1024 * 1024


@dataclass(frozen=True)
class _CachedEvents:
    events: tuple[str, ...]
    retained_bytes: int


class ConversationKeyEventCache:
    """Bound disposable key-event references, never truncate durable key history."""

    def __init__(
        self,
        *,
        max_conversations: int = _MAX_CACHED_CONVERSATIONS,
        max_bytes: int = _MAX_CACHED_EVENT_BYTES,
    ) -> None:
        if max_conversations < 1 or max_bytes < 1:
            raise ValueError("xchat_key_cache_limits_must_be_positive")
        self._max_conversations = max_conversations
        self._max_bytes = max_bytes
        self._entries: OrderedDict[str, _CachedEvents] = OrderedDict()
        self._retained_bytes = 0

    def get(self, conversation_id: str) -> list[str]:
        cached = self._entries.get(conversation_id)
        if cached is None:
            return []
        self._entries.move_to_end(conversation_id)
        return list(cached.events)

    def store(self, conversation_id: str, events: list[str]) -> None:
        previous = self._entries.pop(conversation_id, None)
        if previous is not None:
            self._retained_bytes -= previous.retained_bytes
        if not events:
            return
        retained_events = tuple(events)
        retained_bytes = (
            sys.getsizeof(conversation_id)
            + sys.getsizeof(retained_events)
            + sum(sys.getsizeof(event) for event in retained_events)
        )
        # Oversized history remains usable by this send, but is not retained afterward.
        if retained_bytes > self._max_bytes:
            return
        while self._entries and (
            len(self._entries) >= self._max_conversations
            or self._retained_bytes + retained_bytes > self._max_bytes
        ):
            _, evicted = self._entries.popitem(last=False)
            self._retained_bytes -= evicted.retained_bytes
        self._entries[conversation_id] = _CachedEvents(retained_events, retained_bytes)
        self._retained_bytes += retained_bytes
