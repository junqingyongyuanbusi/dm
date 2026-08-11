from types import SimpleNamespace

import pytest

from social_reply.application.message_delivery.outbox import _send_state_allowed


@pytest.mark.parametrize(
    ("origin_kind", "actor_kind", "payload", "state", "allowed"),
    [
        ("DECISION", "BOT", {"approval": "admin"}, "BOT_ACTIVE", True),
        ("DECISION", "BOT", {"approval": "admin"}, "BOT_DRAFT_ONLY", False),
        ("DECISION", "ADMIN_HUMAN", {"approval": "admin"}, "BOT_DRAFT_ONLY", True),
        ("DECISION", "ADMIN_HUMAN", {}, "BOT_DRAFT_ONLY", False),
        ("DRAFT_APPROVAL", "ADMIN_HUMAN", {}, "BOT_DRAFT_ONLY", True),
        ("DRAFT_APPROVAL", "BOT", {"approval": "admin"}, "BOT_DRAFT_ONLY", False),
        ("MANUAL_REPLY", "ADMIN_HUMAN", {}, "HUMAN_ACTIVE", True),
        ("MANUAL_REPLY", "BOT", {}, "HUMAN_ACTIVE", False),
        ("SYSTEM_NOTICE", "SYSTEM", {}, "HANDOFF_PENDING", True),
        ("SYSTEM_NOTICE", "BOT", {}, "HANDOFF_PENDING", False),
    ],
)
def test_send_state_uses_durable_origin_and_actor_authority(
    origin_kind,
    actor_kind,
    payload,
    state,
    allowed,
):
    row = SimpleNamespace(origin_kind=origin_kind, actor_kind=actor_kind)
    assert _send_state_allowed(row, payload, state) is allowed
