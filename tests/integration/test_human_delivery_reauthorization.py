import uuid

import pytest
from tests.integration.test_tenant_delivery_recovery import (
    _PASSWORD,
    _install_dispatch_spy,
    _seed_delivery,
    _seed_user,
)

from social_reply.application.account_management.auth import authenticate
from social_reply.application.message_delivery import recovery
from social_reply.infrastructure.database import models

pytestmark = pytest.mark.integration


@pytest.fixture
async def recovery_admin(session):
    result = await authenticate("admin", "test-admin-password")
    assert result is not None
    return result[0]


def _resolution(context, principal, resolution="CONFIRMED_NOT_SENT_RETRY"):
    return {
        "outbox_id": context.outbox_id,
        "required_tenant_id": context.tenant_id,
        "actor": principal.actor,
        "principal": principal,
        "expected_status": "NEEDS_REVIEW",
        "expected_attempt_count": 2,
        "review_reason": "Provider confirms no message was sent",
        "verification_source": "PROVIDER_DASHBOARD",
        "resolution": resolution,
        "provider_message_id": None,
    }


@pytest.mark.parametrize("origin", ["DECISION", "DRAFT_APPROVAL", "MANUAL_REPLY"])
async def test_recovery_cannot_reauthorize_historical_human_send(
    session, monkeypatch, origin, recovery_admin
):
    context = await _seed_delivery(session, status="NEEDS_REVIEW", actor_kind="ADMIN_HUMAN")
    outbox = await session.get(models.OutboxMessage, context.outbox_id)
    outbox.origin_kind = origin
    outbox.payload = {**outbox.payload, "approval": "admin"}
    await session.commit()
    dispatched = _install_dispatch_spy(monkeypatch)
    with pytest.raises(recovery.DeliveryRecoveryConflict, match="human_outbox_requires_reapproval"):
        await recovery.resolve_needs_review_outbox(**_resolution(context, recovery_admin))
    await session.refresh(outbox)
    assert outbox.status == "NEEDS_REVIEW"
    assert dispatched == []
    result = await recovery.resolve_needs_review_outbox(
        **_resolution(context, recovery_admin, "CANCEL")
    )
    assert result.status == "CANCELLED"
    assert dispatched == []


@pytest.mark.parametrize("origin", ["DECISION", "DRAFT_APPROVAL"])
@pytest.mark.parametrize("revoked", [False, True])
async def test_human_retry_checks_original_approver_is_still_authorized(
    session, monkeypatch, origin, revoked, recovery_admin
):
    user = await _seed_user(
        session, username=f"approver-{uuid.uuid4().hex}", role="WORKSPACE_ADMIN"
    )
    auth = await authenticate(user.username, _PASSWORD)
    assert auth is not None
    principal, _token = auth
    context = await _seed_delivery(session, status="NEEDS_REVIEW", actor_kind="ADMIN_HUMAN")
    outbox = await session.get(models.OutboxMessage, context.outbox_id)
    outbox.origin_kind = origin
    outbox.payload = {**outbox.payload, "approval": "admin"}
    outbox.initiator_user_id = user.id
    outbox.initiator_session_id = principal.session_id
    if revoked:
        user.status = "disabled"
    await session.commit()
    dispatched = _install_dispatch_spy(monkeypatch)
    if revoked:
        with pytest.raises(
            recovery.DeliveryRecoveryConflict, match="human_outbox_authority_revoked"
        ):
            await recovery.resolve_needs_review_outbox(**_resolution(context, recovery_admin))
        await session.refresh(outbox)
        assert outbox.status == "NEEDS_REVIEW"
        assert dispatched == []
    else:
        result = await recovery.resolve_needs_review_outbox(**_resolution(context, recovery_admin))
        assert result.status == "PENDING"
        assert dispatched == [str(context.outbox_id)]


@pytest.mark.parametrize("resolution", ["CANCEL", "CONFIRMED_SENT", "CONFIRMED_NOT_SENT_RETRY"])
@pytest.mark.parametrize("revocation", ["sessions", "disabled", "demoted"])
async def test_stale_recovery_request_cannot_mutate_after_operator_revocation(
    session, monkeypatch, recovery_admin, resolution, revocation
):
    import asyncio

    from sqlalchemy import func, select

    from social_reply.application.account_management.system_user_management import (
        SystemUserActor,
        revoke_system_user_sessions,
        set_system_user_role,
        set_system_user_status,
    )

    user = await _seed_user(
        session, username=f"recovery-operator-{uuid.uuid4().hex}", role="WORKSPACE_ADMIN"
    )
    result = await authenticate(user.username, _PASSWORD)
    assert result is not None
    stale_principal = result[0]
    user_id = user.id
    context = await _seed_delivery(session, status="NEEDS_REVIEW")
    dispatched = _install_dispatch_spy(monkeypatch)
    initially_authenticated = asyncio.Event()
    continue_request = asyncio.Event()

    async def delayed_command():
        initially_authenticated.set()
        await continue_request.wait()
        values = _resolution(context, stale_principal, resolution)
        if resolution == "CONFIRMED_SENT":
            values["provider_message_id"] = "verified-provider-message"
        return await recovery.resolve_needs_review_outbox(**values)

    request_task = asyncio.create_task(delayed_command())
    await initially_authenticated.wait()
    actor = SystemUserActor(recovery_admin.actor, recovery_admin.session_id)
    common = {"user_id": user_id, "actor": actor, "bootstrap_password": "test-admin-password"}
    try:
        if revocation == "sessions":
            await revoke_system_user_sessions(**common)
        elif revocation == "disabled":
            await set_system_user_status(
                **common, user_status="disabled", emergency_reason="Security incident"
            )
        else:
            await set_system_user_role(**common, role="USER", emergency_reason="Security incident")
    finally:
        continue_request.set()
    with pytest.raises(recovery.DeliveryRecoveryConflict, match="delivery_admin_authority_revoked"):
        await asyncio.wait_for(request_task, timeout=5)
    session.expire_all()
    outbox = await session.get(models.OutboxMessage, context.outbox_id)
    assert outbox.status == "NEEDS_REVIEW"
    assert outbox.platform_message_id is None
    assert outbox.attempt_count == 2
    assert (
        await session.scalar(
            select(func.count())
            .select_from(models.Message)
            .where(models.Message.source_outbox_id == context.outbox_id)
        )
        == 0
    )
    assert (
        await session.scalar(
            select(func.count())
            .select_from(models.AuditLog)
            .where(
                models.AuditLog.category == "delivery_recovery",
                models.AuditLog.subject_id == str(context.outbox_id),
            )
        )
        == 0
    )
    assert dispatched == []
