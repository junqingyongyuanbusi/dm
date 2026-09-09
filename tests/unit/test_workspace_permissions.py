import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from social_reply.application.account_management.access import (
    account_read_condition,
    user_can_access_account,
)
from social_reply.application.account_management.auth import Principal
from social_reply.infrastructure.database import models


def principal_for(role, **overrides):
    return Principal(
        session_id=uuid.uuid4(),
        username="member",
        actor="user:member",
        user_id=uuid.uuid4(),
        tenant_id="tenant-a",
        allowed_tenants=frozenset({"tenant-a"}),
        role=role,
        **overrides,
    )


@pytest.mark.parametrize(
    "role,allowed,denied",
    [
        ("WORKSPACE_ADMIN", {"settings.read", "configure", "connect", "reply"}, set()),
        (
            "MANAGER",
            {"home.read", "reports.read", "connect", "reply", "takeover"},
            {"team.read", "audit.read", "settings.read", "configure"},
        ),
        (
            "OPERATOR",
            {"inbox.read", "channels.read", "connect"},
            {"reply", "takeover", "configure"},
        ),
        (
            "AGENT",
            {"inbox.read", "contacts.read", "knowledge.read", "reply", "takeover"},
            {"connect", "configure"},
        ),
        ("USER", {"inbox.read", "reply", "takeover"}, {"connect", "configure"}),
        ("VIEWER", {"inbox.read", "reports.read", "audit.read"}, {"reply", "takeover", "connect"}),
        ("SUPERADMIN", set(), {"reply", "configure"}),
    ],
)
def test_role_capabilities_fail_closed(role, allowed, denied):
    principal = principal_for(role)
    assert all(principal.has_capability(capability) for capability in allowed)
    assert not any(principal.has_capability(capability) for capability in denied)
    assert not principal.has_capability("unknown.action")
    with pytest.raises(HTTPException):
        principal.require_capability("unknown.action")


def test_operator_opt_ins_are_member_specific_and_not_other_roles():
    operator = principal_for("OPERATOR", operator_reply_enabled=True)
    assert operator.has_capability("reply")
    assert not operator.has_capability("takeover")
    viewer = principal_for("VIEWER", operator_reply_enabled=True, operator_takeover_enabled=True)
    assert not viewer.has_capability("reply")
    assert not viewer.has_capability("takeover")


def test_shared_flag_is_not_an_account_access_grant():
    principal = principal_for("AGENT")
    account = models.PlatformAccount(
        id=uuid.uuid4(), tenant_id="tenant-a", shared_with_support=True
    )
    assert not principal.can_access_account(account)
    user = models.AdminUser(
        id=principal.user_id, tenant_id="tenant-a", role="AGENT", status="active"
    )
    assert not user_can_access_account(user, account)


def test_explicit_account_grants_are_tenant_bound():
    account_id = uuid.uuid4()
    principal = principal_for("AGENT", account_access_ids=frozenset({account_id}))
    account = models.PlatformAccount(id=account_id, tenant_id="tenant-a")
    assert principal.can_access_account(account)
    foreign_account = models.PlatformAccount(id=account_id, tenant_id="tenant-b")
    assert not principal.can_access_account(foreign_account)


def test_sql_account_scope_uses_active_tenant_bound_grants():
    sql = str(
        account_read_condition(principal_for("AGENT"), "tenant-a").compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "EXISTS" in sql
    assert "account_access_grants" in sql
    assert "shared_with_support" not in sql
    assert "active IS true" in sql


def test_account_access_grants_have_composite_tenant_foreign_keys():
    table = models.AccountAccessGrant.__table__
    assert {tuple(constraint.column_keys) for constraint in table.foreign_key_constraints} == {
        ("tenant_id", "user_id"),
        ("tenant_id", "platform_account_id"),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["VIEWER", "OPERATOR"])
async def test_worker_rejects_downgraded_human_sender_before_loading_delivery(monkeypatch, role):
    from social_reply.application.message_delivery import outbox as delivery

    principal = principal_for(role)
    monkeypatch.setattr(delivery, "principal_from_session_row", AsyncMock(return_value=principal))
    session = SimpleNamespace(scalar=AsyncMock())
    message = SimpleNamespace(
        payload={},
        actor_kind="ADMIN_HUMAN",
        origin_kind="MANUAL_REPLY",
        initiator_session_id=principal.session_id,
    )
    assert await delivery._validate_human_outbox_authority(session, outbox=message) == (
        "HUMAN_INITIATOR_SESSION_INVALID"
    )
    session.scalar.assert_not_called()


@pytest.mark.asyncio
async def test_worker_rechecks_connect_for_legacy_staff(monkeypatch):
    from social_reply.application.account_management import jobs

    principal = principal_for("USER")
    monkeypatch.setattr(jobs, "principal_from_session_row", AsyncMock(return_value=principal))
    job = SimpleNamespace(
        authority_kind="STAFF_SESSION",
        authority_version=1,
        initiator_session_id=principal.session_id,
        initiator_user_id=principal.user_id,
        staging_secret=None,
        tenant_id="tenant-a",
        operation="CONNECT_ACCOUNT",
    )
    with pytest.raises(PermissionError, match="initiator_session_invalid"):
        await jobs._validate_job_authority(SimpleNamespace(), job, authority_locks_held=True)


@pytest.mark.asyncio
async def test_reply_opt_in_does_not_grant_takeover():
    from social_reply.application.account_management.human_workflow import (
        HumanWorkflowError,
        _authorize_human_actor,
    )

    principal = principal_for("OPERATOR", operator_reply_enabled=True)
    account = models.PlatformAccount(
        id=uuid.uuid4(), tenant_id="tenant-a", owner_user_id=principal.user_id
    )
    user = models.AdminUser(
        id=principal.user_id,
        username=principal.username,
        role="OPERATOR",
        tenant_id="tenant-a",
        status="active",
        must_change_password=False,
        operator_reply_enabled=True,
        operator_takeover_enabled=False,
    )
    arguments = dict(
        principal=principal,
        current_principal=principal,
        staff_user=user,
        account=account,
        tenant_id="tenant-a",
        actor=principal.actor,
        user_id=principal.user_id,
    )
    with pytest.raises(HumanWorkflowError, match="human_account_access_denied"):
        await _authorize_human_actor(**arguments)
    assert (await _authorize_human_actor(**arguments, required_capability="reply"))[0] == principal


@pytest.mark.asyncio
async def test_reauthorization_needs_connect_even_with_explicit_account_scope():
    from social_reply.application.account_management.access import require_reauthorization

    account_id = uuid.uuid4()
    principal = principal_for("AGENT", account_access_ids=frozenset({account_id}))
    account = models.PlatformAccount(id=account_id, tenant_id="tenant-a", config_version=1)
    session = SimpleNamespace(scalar=AsyncMock(return_value=uuid.uuid4()))
    with pytest.raises(PermissionError, match="account_reauthorization_denied"):
        await require_reauthorization(session, principal=principal, account=account)
    session.scalar.assert_not_called()


@pytest.mark.parametrize("flag", ["true", 1, None])
def test_operator_overrides_require_boolean_true(flag):
    principal = principal_for(
        "OPERATOR", operator_reply_enabled=flag, operator_takeover_enabled=flag
    )
    assert not principal.has_capability("reply")
    assert not principal.has_capability("takeover")


def test_unknown_role_and_password_change_fail_closed():
    assert not principal_for("UNKNOWN").has_capability("inbox.read")
    assert not principal_for("WORKSPACE_ADMIN", must_change_password=True).has_capability("reply")
    operator = principal_for("OPERATOR", operator_takeover_enabled=True)
    assert operator.has_capability("takeover")
    assert not operator.has_capability("reply")


@pytest.mark.asyncio
async def test_session_reload_reflects_role_flags_and_revoked_grants():
    from social_reply.application.account_management import auth

    user_id, session_id, account_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    user_values = dict(
        id=user_id,
        username="member",
        tenant_id="tenant-a",
        status="active",
        password_hash="test-password-hash",
        must_change_password=False,
        operator_reply_enabled=True,
        operator_takeover_enabled=False,
    )
    before_user = models.AdminUser(**user_values, role="OPERATOR")
    after_user = models.AdminUser(**user_values, role="VIEWER")
    stored_session = models.AdminSession(
        id=session_id,
        user_id=user_id,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        credential_fingerprint=auth._credential_fingerprint(before_user.password_hash),
    )
    session = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                SimpleNamespace(one_or_none=lambda: (stored_session, before_user)),
                SimpleNamespace(one_or_none=lambda: (stored_session, after_user)),
            ]
        ),
        scalars=AsyncMock(side_effect=[[account_id], []]),
    )
    before = await auth.principal_from_session_row(session, session_id)
    after = await auth.principal_from_session_row(session, session_id)
    account = models.PlatformAccount(id=account_id, tenant_id="tenant-a")
    assert before.has_capability("reply")
    assert before.can_access_account(account)
    assert not after.has_capability("reply")
    assert not after.can_access_account(account)
    grant_query = session.scalars.call_args.args[0]
    compiled = str(
        grant_query.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "account_access_grants.tenant_id = 'tenant-a'" in compiled
    assert str(user_id) in compiled
    assert "account_access_grants.active IS true" in compiled


@pytest.mark.asyncio
async def test_assigned_reply_only_operator_remains_eligible():
    from social_reply.application.account_management.channel_management import (
        _account_work_assignment_is_eligible,
    )

    user_id = uuid.uuid4()
    account = models.PlatformAccount(id=uuid.uuid4(), tenant_id="tenant-a", owner_user_id=user_id)
    user = models.AdminUser(
        id=user_id,
        tenant_id="tenant-a",
        role="OPERATOR",
        status="active",
        must_change_password=False,
        operator_reply_enabled=True,
        operator_takeover_enabled=False,
    )
    work = models.HumanWorkItem(assigned_user_id=user_id)
    assert await _account_work_assignment_is_eligible(
        SimpleNamespace(),
        work,
        account=account,
        staff_rows={user_id: user},
        session_principals={},
    )


@pytest.mark.asyncio
async def test_reauthorization_grant_revocation_accepts_demoted_grantee(monkeypatch):
    from social_reply.application.account_management import channel_management as channels

    principal = principal_for("WORKSPACE_ADMIN")
    target_id = uuid.uuid4()
    target = models.AdminUser(id=target_id, tenant_id="tenant-a", role="AGENT", status="disabled")
    account = models.PlatformAccount(id=uuid.uuid4(), tenant_id="tenant-a")
    actor = channels.ChannelActor(
        actor=principal.actor,
        role="ADMIN",
        user_id=principal.user_id,
        session_id=principal.session_id,
    )
    monkeypatch.setattr(channels, "principal_from_session_row", AsyncMock(return_value=principal))
    monkeypatch.setattr(channels, "lock_user_authority", AsyncMock())
    monkeypatch.setattr(channels, "_lock_account_access_sessions", AsyncMock())
    monkeypatch.setattr(
        channels, "_lock_account_access_staff_rows", AsyncMock(return_value={target_id: target})
    )
    session = SimpleNamespace(scalar=AsyncMock(return_value=account))
    assert (
        await channels._lock_channel_reauthorization_grant_context(
            session,
            tenant_id="tenant-a",
            account_id=account.id,
            actor=actor,
            target_user_id=target_id,
            grant_enabled=False,
        )
        is account
    )
