import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from social_reply.application.account_management.auth import (
    authenticate,
    hash_password,
    issue_session,
)
from social_reply.application.account_management.system_user_management import (
    SystemUserActor,
    SystemUserConflictError,
    SystemUserValidationError,
    create_system_user,
    force_system_user_password_reset,
    revoke_system_user_sessions,
    set_system_user_role,
    set_system_user_status,
)
from social_reply.infrastructure.database import models

pytestmark = pytest.mark.integration
_PASSWORD = "company-staff-test-password"
_BOOTSTRAP_PASSWORD = "test-admin-password"


async def _staff(session, username, role="USER", user_id=None):
    user = models.AdminUser(
        id=user_id or uuid.uuid4(),
        username=username,
        password_hash=await hash_password(_PASSWORD),
        tenant_id="default",
        role=role,
        status="active",
        must_change_password=False,
    )
    session.add(user)
    await session.commit()
    result = await authenticate(username, _PASSWORD)
    assert result is not None
    principal, _token = result
    return user, principal


async def _bootstrap():
    _token, session_id = await issue_session()
    return SystemUserActor(actor="ignored-client-actor", session_id=session_id)


async def _legacy_work(session):
    account = models.PlatformAccount(
        tenant_id="default",
        brand_id="default",
        platform="telegram",
        name="Legacy company bot",
        external_account_id=str(uuid.uuid4()),
        public_id=str(uuid.uuid4()),
        config={"delivery_mode": "direct"},
        capability={"dm": True},
        shared_with_support=True,
        status="active",
    )
    session.add(account)
    await session.flush()
    contact = models.Contact(
        tenant_id="default",
        platform="telegram",
        platform_account_id=account.id,
        external_user_id=str(uuid.uuid4()),
    )
    session.add(contact)
    await session.flush()
    conversation = models.Conversation(
        tenant_id="default",
        brand_id="default",
        platform="telegram",
        platform_account_id=account.id,
        contact_id=contact.id,
        conversation_key=str(uuid.uuid4()),
    )
    session.add(conversation)
    await session.flush()
    work = models.HumanWorkItem(
        tenant_id="default",
        conversation_id=conversation.id,
        status="CLAIMED",
        reason_code="HUMAN_REQUEST",
        assigned_actor="user:historical-bootstrap",
        assigned_user_id=None,
        claimed_at=datetime.now(UTC),
        version=2,
    )
    state = models.AutomationState(
        conversation_id=conversation.id,
        state="HUMAN_ACTIVE",
        state_version=2,
    )
    session.add_all([work, state])
    await session.commit()
    return account, conversation, work


@pytest.mark.parametrize("operation", ["disable", "role", "reset", "revoke"])
async def test_unrelated_null_assignee_does_not_block_employee_revocation(
    session, migrated_db, operation
):
    user, principal = await _staff(session, f"staff-{operation}")
    _account, conversation, _work = await _legacy_work(session)
    conversation_id = conversation.id
    job = models.ProvisioningJob(
        tenant_id="default",
        brand_id="default",
        platform="telegram",
        operation="CONNECT_ACCOUNT",
        actor=principal.actor,
        owner_user_id=user.id,
        initiator_user_id=user.id,
        initiator_session_id=principal.session_id,
        authority_kind="STAFF_SESSION",
        authority_version=1,
        idempotency_key=str(uuid.uuid4()),
        request={},
        status="PENDING",
    )
    session.add(job)
    await session.commit()
    actor = await _bootstrap()
    common = {"user_id": user.id, "actor": actor, "bootstrap_password": _BOOTSTRAP_PASSWORD}
    if operation == "disable":
        await set_system_user_status(**common, user_status="disabled")
    elif operation == "role":
        await set_system_user_role(**common, role="WORKSPACE_ADMIN")
    elif operation == "reset":
        await force_system_user_password_reset(
            **common, initial_password="replacement-password-123"
        )
    else:
        await revoke_system_user_sessions(**common)
    session.expire_all()
    assert (
        await session.scalar(
            select(func.count())
            .select_from(models.AdminSession)
            .where(models.AdminSession.user_id == principal.user_id)
        )
        == 0
    )
    await session.refresh(job)
    assert job.status not in {"PENDING", "PROCESSING", "RUNNING"}
    state = await session.get(models.AutomationState, conversation_id)
    assert state.state in {"HANDOFF_PENDING", "HUMAN_ACTIVE"}


async def test_bootstrap_can_emergency_disable_last_admin_with_reason(session, migrated_db):
    user, principal = await _staff(session, "only-company-admin", "WORKSPACE_ADMIN")
    user_id = user.id
    _account, _conversation, _work = await _legacy_work(session)
    named_actor = SystemUserActor(principal.actor, principal.session_id, principal.user_id)
    with pytest.raises(SystemUserConflictError, match="last_workspace_admin_required"):
        await set_system_user_role(
            user_id=user_id,
            role="USER",
            actor=named_actor,
            bootstrap_password=_PASSWORD,
        )
    bootstrap = await _bootstrap()
    with pytest.raises(SystemUserValidationError, match="bootstrap_emergency_reason_required"):
        await set_system_user_status(
            user_id=user_id,
            user_status="disabled",
            actor=bootstrap,
            bootstrap_password=_BOOTSTRAP_PASSWORD,
        )
    await set_system_user_status(
        user_id=user_id,
        user_status="disabled",
        actor=bootstrap,
        bootstrap_password=_BOOTSTRAP_PASSWORD,
        emergency_reason="Company account compromised",
    )
    session.expire_all()
    disabled = await session.get(models.AdminUser, user_id)
    assert disabled.status == "disabled"
    assert await session.get(models.AdminSession, principal.session_id) is None
    audit = await session.scalar(
        select(models.AuditLog).where(
            models.AuditLog.action == "SET_USER_STATUS", models.AuditLog.subject_id == str(user_id)
        )
    )
    assert audit.detail["bootstrap_emergency"] is True
    assert audit.detail["emergency_reason"] == "Company account compromised"
    replacement_id = await create_system_user(
        username="replacement-company-admin",
        initial_password="replacement-initial-password",
        role="WORKSPACE_ADMIN",
        bootstrap_password=_BOOTSTRAP_PASSWORD,
        actor=bootstrap,
    )
    replacement = await session.get(models.AdminUser, replacement_id)
    assert replacement.role == "WORKSPACE_ADMIN"
    assert replacement.must_change_password is True
