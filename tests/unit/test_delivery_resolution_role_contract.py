import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from social_reply.application.account_management.auth import Principal
from social_reply.application.message_delivery import recovery
from social_reply.infrastructure.database import models


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["AGENT", "MANAGER", "OPERATOR"])
@pytest.mark.parametrize("resolution", ["CONFIRMED_SENT", "CANCEL"])
async def test_reply_capability_cannot_resolve_another_members_delivery(
    monkeypatch,
    role,
    resolution,
):
    account_id = uuid.uuid4()
    principal = Principal(
        session_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        username="member",
        actor="user:member",
        tenant_id="tenant-a",
        allowed_tenants=frozenset({"tenant-a"}),
        role=role,
        operator_reply_enabled=True,
        account_access_ids=frozenset({account_id}),
    )
    assert principal.has_capability("reply")
    outbox = models.OutboxMessage(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        platform_account_id=account_id,
        status="NEEDS_REVIEW",
        attempt_count=1,
        initiator_user_id=uuid.uuid4(),
        last_error_code="PROVIDER_RESULT_UNKNOWN",
    )
    account = models.PlatformAccount(id=account_id, tenant_id="tenant-a", platform="telegram")
    context = SimpleNamespace(outbox=outbox, account=account)
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.scalar.return_value = datetime.now(UTC)
    monkeypatch.setattr(recovery, "get_session_factory", lambda: lambda: session)
    monkeypatch.setattr(recovery, "lock_user_authority", AsyncMock())
    monkeypatch.setattr(recovery, "principal_from_session_row", AsyncMock(return_value=principal))
    load_context = AsyncMock(return_value=context)
    monkeypatch.setattr(recovery, "_load_locked_delivery_context", load_context)
    monkeypatch.setattr(recovery, "_resolve_replay", AsyncMock(return_value=None))
    record_resolution = AsyncMock()
    materialize = AsyncMock()
    monkeypatch.setattr(recovery, "_record_manual_resolution", record_resolution)
    monkeypatch.setattr(recovery, "materialize_sent_outbox", materialize)

    with pytest.raises(recovery.DeliveryRecoveryConflict, match="delivery_admin_authority_revoked"):
        await recovery.resolve_needs_review_outbox(
            outbox_id=outbox.id,
            required_tenant_id="tenant-a",
            actor=principal.actor,
            expected_status="NEEDS_REVIEW",
            expected_attempt_count=1,
            review_reason="Checked provider result",
            verification_source="PROVIDER_DASHBOARD",
            resolution=resolution,
            provider_message_id="provider-message-123" if resolution == "CONFIRMED_SENT" else None,
            principal=principal,
        )

    assert outbox.status == "NEEDS_REVIEW"
    assert outbox.last_error_code == "PROVIDER_RESULT_UNKNOWN"
    assert outbox.platform_message_id is None
    assert outbox.sent_at is None
    load_context.assert_not_awaited()
    record_resolution.assert_not_awaited()
    materialize.assert_not_awaited()
    session.flush.assert_not_awaited()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_original_agent_sender_retry_authority_still_uses_reply(monkeypatch):
    account_id = uuid.uuid4()
    principal = Principal(
        session_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        username="sender",
        actor="user:sender",
        tenant_id="tenant-a",
        allowed_tenants=frozenset({"tenant-a"}),
        role="AGENT",
        account_access_ids=frozenset({account_id}),
    )
    conversation_id = uuid.uuid4()
    context = SimpleNamespace(
        account=models.PlatformAccount(id=account_id, tenant_id="tenant-a"),
        conversation=SimpleNamespace(id=conversation_id),
        outbox=SimpleNamespace(
            actor_kind="ADMIN_HUMAN",
            origin_kind="MANUAL_REPLY",
            payload={},
            initiator_session_id=principal.session_id,
            initiator_user_id=principal.user_id,
            human_work_item_version=3,
        ),
    )
    session = SimpleNamespace(
        scalar=AsyncMock(
            return_value=SimpleNamespace(
                version=3,
                assigned_user_id=principal.user_id,
                assigned_actor=principal.actor,
            )
        )
    )
    monkeypatch.setattr(recovery, "principal_from_session_row", AsyncMock(return_value=principal))
    await recovery._require_human_retry_authority(session, context)
