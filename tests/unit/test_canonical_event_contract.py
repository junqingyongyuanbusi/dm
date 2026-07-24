import uuid

import pytest

from social_reply.domain.messages.canonical import (
    CanonicalEvent,
    CanonicalEventKind,
    canonical_event_from_dict,
    canonical_event_to_dict,
)


def _event() -> CanonicalEvent:
    return CanonicalEvent(
        platform="telegram",
        platform_account_key=str(uuid.uuid4()),
        external_event_id="event-1",
        external_user_id="user-1",
        conversation_key="telegram:account:chat",
        text="hello",
    )


def test_canonical_event_round_trip_includes_explicit_message_kind():
    encoded = canonical_event_to_dict(_event())
    assert encoded["event_kind"] == "message"
    assert canonical_event_from_dict(encoded).event_kind is CanonicalEventKind.MESSAGE


def test_historical_canonical_event_defaults_to_message_kind():
    encoded = canonical_event_to_dict(_event())
    encoded.pop("event_kind")
    assert canonical_event_from_dict(encoded).event_kind is CanonicalEventKind.MESSAGE


def test_unknown_canonical_event_kind_fails_closed():
    encoded = canonical_event_to_dict(_event())
    encoded["event_kind"] = "delivery_receipt"
    with pytest.raises(ValueError):
        canonical_event_from_dict(encoded)
