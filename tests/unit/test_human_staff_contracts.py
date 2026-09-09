import inspect
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from social_reply.application.account_management.feishu_handoff_service import (
    upsert_feishu_handoff_operator,
)
from social_reply.application.account_management.human_workflow import (
    claim_human_work_item,
    resolve_human_work_item,
    send_human_reply,
    start_human_reception,
    transfer_human_work_item,
)
from social_reply.application.account_management.staff_lifecycle import revoke_staff_authority
from social_reply.application.handoff_notifications.cards import (
    HandoffCardSnapshot,
    render_handoff_card,
)
from social_reply.application.message_delivery.outbox import (
    _validate_human_outbox_authority,
)


def _card_snapshot() -> HandoffCardSnapshot:
    created = datetime(2026, 8, 5, 10, 0, tzinfo=UTC)
    return HandoffCardSnapshot(
        notification_public_id=str(uuid.uuid4()),
        action_nonce=str(uuid.uuid4()),
        work_version=2,
        card_revision=3,
        card_state="WAITING",
        platform="telegram",
        account_name="sensitive-account-name",
        channel_type="dm",
        contact_label="Customer Name external-user-123",
        reason_code="RISK_WORD",
        latest_message="private customer body with customer@example.com",
        work_created_at=created,
        due_at=created + timedelta(minutes=30),
        rendered_at=created + timedelta(minutes=5),
        assigned_actor=None,
        claimed_at=None,
        resolved_at=None,
        restored_automation_state=None,
        conversation_url="https://reply.example.test/app/t/tenant/conversations/opaque-id",
    )


def test_public_handoff_card_contains_no_customer_payload_or_identifier():
    card = render_handoff_card(_card_snapshot())
    serialized = json.dumps(card, ensure_ascii=False)

    assert "sensitive-account-name" not in serialized
    assert "Customer Name" not in serialized
    assert "external-user-123" not in serialized
    assert "private customer body" not in serialized
    assert "customer@example.com" not in serialized
    assert "opaque-id" in serialized


def test_human_mutation_wrappers_expose_principal_fence():
    for operation in (
        claim_human_work_item,
        resolve_human_work_item,
        send_human_reply,
        transfer_human_work_item,
    ):
        parameter = inspect.signature(operation).parameters["principal"]
        assert parameter.default is None

    # Starting reception already requires an explicit principal argument; it is not optional.
    required_principal = inspect.signature(start_human_reception).parameters["principal"]
    assert required_principal.default is inspect.Parameter.empty

    assert set(inspect.signature(revoke_staff_authority).parameters) == {
        "session",
        "user_id",
        "reason",
    }
    assert set(("admin_user_id", "principal")) <= set(
        inspect.signature(upsert_feishu_handoff_operator).parameters
    )


@pytest.mark.asyncio
async def test_pending_manual_outbox_without_session_fails_closed():
    row = SimpleNamespace(
        origin_kind="MANUAL_REPLY",
        actor_kind="ADMIN_HUMAN",
        initiator_session_id=None,
        payload={},
    )

    assert (
        await _validate_human_outbox_authority(SimpleNamespace(), outbox=row)
        == "HUMAN_INITIATOR_SESSION_MISSING"
    )
