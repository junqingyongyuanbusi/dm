import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from social_reply.application.account_management import system_user_management as management
from social_reply.application.account_management import workspace_member_access as access
from social_reply.application.account_management.auth import Principal
from social_reply.shared.config import DEFAULT_TENANT_ID


@pytest.mark.parametrize("role", ["WORKSPACE_ADMIN", "MANAGER", "OPERATOR", "AGENT", "VIEWER"])
def test_member_roles_are_accepted(role):
    assert management.validate_system_user_role(f" {role.lower()} ") == role


def test_legacy_user_is_normalized_to_agent():
    assert management.validate_system_user_role("USER") == "AGENT"


@pytest.mark.parametrize("role", ["SUPERADMIN", "ADMIN", "", "OWNER"])
def test_privileged_or_unknown_roles_are_rejected(role):
    with pytest.raises(management.SystemUserValidationError, match="invalid_user_role"):
        management.validate_system_user_role(role)


def test_access_form_parses_distinct_account_ids_and_explicit_switches():
    account_id = uuid.uuid4()
    selection = access.parse_member_access_form(
        {
            f"account_{account_id}": "on",
            "operator_reply_enabled": "on",
            "csrf_token": "ignored-by-parser",
        }
    )
    assert selection.account_ids == frozenset({account_id})
    assert selection.operator_reply_enabled is True
    assert selection.operator_takeover_enabled is False


@pytest.mark.parametrize(
    "form",
    [
        {"account_invalid": "on"},
        {f"account_{uuid.uuid4()}": "false"},
        {"operator_reply_enabled": "false"},
        {"operator_takeover_enabled": "1"},
    ],
)
def test_malformed_access_form_fails_closed(form):
    with pytest.raises(management.SystemUserValidationError):
        access.parse_member_access_form(form)


@pytest.mark.parametrize("role", ["MANAGER", "OPERATOR", "AGENT", "VIEWER", "USER"])
@pytest.mark.asyncio
async def test_every_admin_demotion_checks_last_admin(monkeypatch, role):
    target_user = SimpleNamespace(id=uuid.uuid4(), role="WORKSPACE_ADMIN")
    actor = management.SystemUserActor("user:admin", uuid.uuid4(), uuid.uuid4())
    session = MagicMock()

    @asynccontextmanager
    async def transaction(*_arguments):
        yield session, actor

    monkeypatch.setattr(management, "_reauthenticate_manager", AsyncMock())
    monkeypatch.setattr(management, "_management_transaction", transaction)
    monkeypatch.setattr(
        management, "_load_default_user_for_update", AsyncMock(return_value=target_user)
    )
    protection = AsyncMock(
        side_effect=management.SystemUserConflictError("last_workspace_admin_required")
    )
    revocation = AsyncMock()
    monkeypatch.setattr(management, "_protect_last_admin", protection)
    monkeypatch.setattr(management, "_revoke_staff", revocation)
    with pytest.raises(management.SystemUserConflictError, match="last_workspace_admin_required"):
        await management.set_system_user_role(
            user_id=target_user.id,
            role=role,
            bootstrap_password="confirmation",
            actor=actor,
        )
    protection.assert_awaited_once()
    revocation.assert_not_awaited()


def test_cross_tenant_account_selection_is_rejected_without_identifier_disclosure():
    allowed_account = SimpleNamespace(id=uuid.uuid4(), tenant_id=DEFAULT_TENANT_ID)
    foreign_account_id = uuid.uuid4()
    with pytest.raises(
        management.SystemUserNotFoundError, match="platform_account_not_found"
    ) as raised:
        access.validate_member_account_scope(frozenset({foreign_account_id}), [allowed_account])
    assert str(foreign_account_id) not in str(raised.value)


def test_account_scope_accepts_only_current_tenant_accounts():
    account = SimpleNamespace(id=uuid.uuid4(), tenant_id="foreign")
    with pytest.raises(management.SystemUserNotFoundError, match="platform_account_not_found"):
        access.validate_member_account_scope(frozenset({account.id}), [account])


def _principal(role="WORKSPACE_ADMIN", tenant_id=DEFAULT_TENANT_ID, user_id=None):
    return Principal(
        session_id=uuid.uuid4(),
        username="manager",
        actor="user:manager",
        allowed_tenants=frozenset({tenant_id}),
        tenant_id=tenant_id,
        user_id=user_id or uuid.uuid4(),
        role=role,
    )


@pytest.mark.parametrize("role", ["MANAGER", "OPERATOR", "AGENT", "VIEWER", "USER", "SUPERADMIN"])
def test_non_workspace_admin_cannot_manage_member_access(role):
    with pytest.raises(HTTPException) as raised:
        access.require_workspace_member_manager(_principal(role))
    assert raised.value.status_code == 403


def test_foreign_workspace_admin_cannot_manage_default_workspace():
    with pytest.raises(HTTPException) as raised:
        access.require_workspace_member_manager(_principal(tenant_id="foreign"))
    assert raised.value.status_code == 403


@pytest.mark.asyncio
async def test_foreign_or_missing_member_uses_scoped_query_and_generic_error():
    session = SimpleNamespace(scalar=AsyncMock(return_value=None))
    member_id = uuid.uuid4()
    with pytest.raises(management.SystemUserNotFoundError, match="system_user_not_found"):
        await access._load_member(session, member_id)
    statement = session.scalar.await_args.args[0]
    assert DEFAULT_TENANT_ID in statement.compile().params.values()
    assert member_id in statement.compile().params.values()


@pytest.fixture
def member_command(monkeypatch):
    actor = management.SystemUserActor("user:manager", uuid.uuid4(), uuid.uuid4())
    member = SimpleNamespace(
        id=uuid.uuid4(),
        role="OPERATOR",
        tenant_id=DEFAULT_TENANT_ID,
        operator_reply_enabled=False,
        operator_takeover_enabled=False,
    )
    session = MagicMock()
    revocation = AsyncMock()
    reauthentication = AsyncMock()
    accounts = AsyncMock(return_value=[])
    grants = AsyncMock(return_value=[])

    @asynccontextmanager
    async def transaction(_actor, target_ids):
        assert target_ids == (member.id,)
        yield session, actor

    monkeypatch.setattr(access, "_management_transaction", transaction)
    monkeypatch.setattr(access, "_reauthenticate_manager", reauthentication)
    monkeypatch.setattr(access, "_load_member", AsyncMock(return_value=member))
    monkeypatch.setattr(access, "_load_accounts", accounts)
    monkeypatch.setattr(access, "_load_grants", grants)
    monkeypatch.setattr(access, "_load_default_user_for_update", AsyncMock(return_value=member))
    monkeypatch.setattr(access, "_revoke_staff", revocation)
    return SimpleNamespace(
        actor=actor,
        member=member,
        session=session,
        revocation=revocation,
        reauthentication=reauthentication,
        accounts=accounts,
        grants=grants,
    )


async def _save_member(command, selection, expected_revision=None):
    revision = expected_revision or access._access_revision(
        command.member,
        command.accounts.return_value,
        command.grants.return_value,
    )
    await access.set_workspace_member_access(
        user_id=command.member.id,
        selection=selection,
        bootstrap_password="confirmation",
        actor=command.actor,
        expected_revision=revision,
    )


@pytest.mark.asyncio
async def test_switch_change_revokes_staff_and_audits_in_same_transaction(member_command):
    command = member_command
    await _save_member(command, access.MemberAccessSelection(frozenset(), True, True))
    command.reauthentication.assert_awaited_once_with(command.actor, "confirmation")
    command.revocation.assert_awaited_once_with(
        command.session, command.member.id, "MEMBER_ACCESS_CHANGED"
    )
    assert command.member.operator_reply_enabled is True
    assert command.member.operator_takeover_enabled is True
    audit = command.session.add.call_args.args[0]
    assert audit.action == "SET_MEMBER_ACCESS"
    assert audit.detail["sessions_revoked"] is True
    assert audit.detail["previous_operator_reply_enabled"] is False
    assert "confirmation" not in repr(audit.detail)


@pytest.mark.asyncio
async def test_unchanged_permissions_do_not_revoke_sessions(member_command):
    await _save_member(member_command, access.MemberAccessSelection(frozenset()))
    member_command.revocation.assert_not_awaited()
    member_command.session.add.assert_not_called()


@pytest.mark.asyncio
async def test_stale_form_cannot_restore_permissions_revoked_by_another_admin(member_command):
    member_command.member.operator_reply_enabled = True
    stale_revision = access._access_revision(member_command.member, [], [])
    member_command.member.operator_reply_enabled = False
    with pytest.raises(management.SystemUserConflictError, match="member_access_changed"):
        await _save_member(
            member_command,
            access.MemberAccessSelection(frozenset(), True, True),
            expected_revision=stale_revision,
        )
    assert member_command.member.operator_reply_enabled is False
    member_command.revocation.assert_not_awaited()
    member_command.session.add.assert_not_called()


@pytest.mark.asyncio
async def test_cross_tenant_post_is_rejected_before_revocation(member_command):
    with pytest.raises(management.SystemUserNotFoundError, match="platform_account_not_found"):
        await _save_member(member_command, access.MemberAccessSelection(frozenset({uuid.uuid4()})))
    member_command.revocation.assert_not_awaited()
    member_command.session.add.assert_not_called()


@pytest.mark.asyncio
async def test_revocation_failure_prevents_permission_mutation_and_audit(member_command):
    member_command.revocation.side_effect = RuntimeError("revocation_failed")
    with pytest.raises(RuntimeError, match="revocation_failed"):
        await _save_member(member_command, access.MemberAccessSelection(frozenset(), True))
    assert member_command.member.operator_reply_enabled is False
    member_command.session.add.assert_not_called()


@pytest.mark.asyncio
async def test_wrong_confirmation_password_prevents_permission_changes(member_command):
    member_command.reauthentication.side_effect = management.SystemUserAuthenticationError(
        "confirmation_password_invalid"
    )
    with pytest.raises(
        management.SystemUserAuthenticationError, match="confirmation_password_invalid"
    ):
        await _save_member(member_command, access.MemberAccessSelection(frozenset(), True))
    assert member_command.member.operator_reply_enabled is False
    member_command.revocation.assert_not_awaited()
    member_command.session.add.assert_not_called()


@pytest.mark.asyncio
async def test_owned_account_omission_does_not_remove_implicit_access(member_command):
    owned_account = SimpleNamespace(
        id=uuid.uuid4(),
        owner_user_id=member_command.member.id,
        tenant_id=DEFAULT_TENANT_ID,
    )
    member_command.accounts.return_value = [owned_account]
    await _save_member(member_command, access.MemberAccessSelection(frozenset()))
    assert owned_account.owner_user_id == member_command.member.id
    member_command.revocation.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_grant_is_reactivated_after_lifecycle_revocation(member_command):
    account_id = uuid.uuid4()
    account = SimpleNamespace(id=account_id, owner_user_id=None, tenant_id=DEFAULT_TENANT_ID)
    grant = SimpleNamespace(platform_account_id=account_id, active=True)
    member_command.accounts.return_value = [account]
    member_command.grants.return_value = [grant]

    async def revoke(*_arguments):
        grant.active = False

    member_command.revocation.side_effect = revoke
    await _save_member(member_command, access.MemberAccessSelection(frozenset({account_id}), True))
    assert grant.active is True
    assert member_command.member.operator_reply_enabled is True


@pytest.mark.asyncio
async def test_unchecked_explicit_account_grant_is_revoked_and_audited(member_command):
    account_id = uuid.uuid4()
    member_command.accounts.return_value = [
        SimpleNamespace(id=account_id, owner_user_id=None, tenant_id=DEFAULT_TENANT_ID)
    ]
    grant = SimpleNamespace(platform_account_id=account_id, active=True)
    member_command.grants.return_value = [grant]
    await _save_member(member_command, access.MemberAccessSelection(frozenset()))
    assert grant.active is False
    member_command.revocation.assert_awaited_once()
    audit = member_command.session.add.call_args.args[0]
    assert audit.detail["previous_account_ids"] == [str(account_id)]
    assert audit.detail["account_ids"] == []


@pytest.mark.asyncio
async def test_access_account_limit_fails_instead_of_truncating():
    session = SimpleNamespace(
        scalars=AsyncMock(return_value=[object()] * (access.MAX_MEMBER_ACCESS_ACCOUNTS + 1))
    )
    with pytest.raises(
        management.SystemUserValidationError, match="member_access_account_limit_exceeded"
    ):
        await access._load_accounts(session)
    assert (
        access.MAX_MEMBER_ACCESS_ACCOUNTS + 1
        in session.scalars.await_args.args[0].compile().params.values()
    )


@pytest.mark.parametrize(
    "previous, target",
    [("AGENT", "MANAGER"), ("VIEWER", "WORKSPACE_ADMIN"), ("OPERATOR", "VIEWER")],
)
@pytest.mark.asyncio
async def test_role_changes_revoke_authority_and_audit(monkeypatch, previous, target):
    member = SimpleNamespace(id=uuid.uuid4(), role=previous)
    actor = management.SystemUserActor("user:manager", uuid.uuid4(), uuid.uuid4())
    session = MagicMock()

    @asynccontextmanager
    async def transaction(*_arguments):
        yield session, actor

    monkeypatch.setattr(management, "_reauthenticate_manager", AsyncMock())
    monkeypatch.setattr(management, "_management_transaction", transaction)
    monkeypatch.setattr(management, "_load_default_user_for_update", AsyncMock(return_value=member))
    monkeypatch.setattr(management, "_protect_last_admin", AsyncMock())
    revocation = AsyncMock()
    monkeypatch.setattr(management, "_revoke_staff", revocation)
    await management.set_system_user_role(
        user_id=member.id,
        role=target,
        bootstrap_password="confirmation",
        actor=actor,
    )
    assert member.role == target
    revocation.assert_awaited_once_with(session, member.id, "USER_ROLE_CHANGED")
    audit = session.add.call_args.args[0]
    assert audit.detail["previous_role"] == previous
    assert audit.detail["role"] == target


def test_member_form_escapes_accounts_and_locks_owner_checkboxes():
    from social_reply.application.account_management.users import _member_access_form

    owner_id = uuid.uuid4()
    member = access.WorkspaceMemberAccess(
        user_id=uuid.uuid4(),
        username="agent",
        role="AGENT",
        operator_reply_enabled=False,
        operator_takeover_enabled=False,
        accounts=(
            access.MemberAccountAccess(owner_id, "<script>alert(1)</script>", "email", True, False),
        ),
    )
    markup = _member_access_form(member, 'csrf"token')
    assert f'name="account_{owner_id}" checked disabled' in markup
    assert "<script>" not in markup
    assert "&lt;script&gt;" in markup
    assert 'value="csrf&quot;token"' in markup
    assert 'name="bootstrap_password"' in markup


def test_role_dropdown_has_five_roles_and_defaults_legacy_to_agent():
    from social_reply.application.account_management.users import _role_field

    markup = _role_field("USER")
    assert markup.count("<option") == 5
    assert 'value="AGENT" selected' in markup
    assert 'value="USER"' not in markup
    assert 'value="SUPERADMIN"' not in markup


@pytest.fixture
def member_http(monkeypatch):
    from fastapi import FastAPI

    from social_reply.application.account_management import users

    application = FastAPI()
    application.include_router(users.router)
    authentication = AsyncMock(return_value=_principal())
    command = AsyncMock()
    snapshot = access.WorkspaceMemberAccess(
        user_id=uuid.uuid4(),
        username="test-member",
        role="OPERATOR",
        operator_reply_enabled=False,
        operator_takeover_enabled=False,
        accounts=(),
        revision="a" * 64,
    )
    read = AsyncMock(return_value=snapshot)
    monkeypatch.setattr(users, "_web_principal", authentication)
    monkeypatch.setattr(users, "get_workspace_member_access", read)
    monkeypatch.setattr(users, "set_workspace_member_access", command)
    return SimpleNamespace(
        application=application,
        authentication=authentication,
        command=command,
        snapshot=snapshot,
        read=read,
    )


@pytest.mark.parametrize(
    "locale, label", [("en", "Allow operator replies"), ("zh-CN", "允许运营回复消息")]
)
@pytest.mark.asyncio
async def test_access_get_renders_locale_and_issues_csrf_cookie(member_http, locale, label):
    from httpx import ASGITransport, AsyncClient

    from social_reply.application.account_management.ui_i18n import reset_locale, set_locale

    locale_token = set_locale(locale)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=member_http.application),
            base_url="https://test",
        ) as client:
            response = await client.get(f"/admin/users/{member_http.snapshot.user_id}/access")
        assert response.status_code == 200
        assert label in response.text
        assert 'name="access_revision" value="' + "a" * 64 in response.text
        assert "reply_admin_csrf" in response.cookies
    finally:
        reset_locale(locale_token)


@pytest.mark.parametrize("role", ["MANAGER", "OPERATOR", "AGENT", "VIEWER", "SUPERADMIN"])
@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.asyncio
async def test_access_http_rejects_non_workspace_managers(member_http, role, method):
    from httpx import ASGITransport, AsyncClient

    from social_reply.application.account_management.auth import _bootstrap_principal

    principal = (
        _bootstrap_principal(uuid.uuid4(), verified=True)
        if role == "SUPERADMIN"
        else _principal(role)
    )
    member_http.authentication.return_value = principal
    async with AsyncClient(
        transport=ASGITransport(app=member_http.application),
        base_url="https://test",
        cookies={"reply_admin_csrf": "csrf"},
    ) as client:
        response = await client.request(
            method,
            f"/admin/users/{member_http.snapshot.user_id}/access",
            data={"csrf_token": "csrf"},
        )
    assert response.status_code == 403
    member_http.read.assert_not_awaited()
    member_http.command.assert_not_awaited()


@pytest.mark.parametrize("submitted_csrf", ["", "wrong"])
@pytest.mark.asyncio
async def test_access_post_rejects_invalid_csrf_before_command(member_http, submitted_csrf):
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=member_http.application),
        base_url="https://test",
        cookies={"reply_admin_csrf": "csrf"},
    ) as client:
        response = await client.post(
            f"/admin/users/{member_http.snapshot.user_id}/access",
            data={"csrf_token": submitted_csrf},
        )
    assert response.status_code == 403
    member_http.command.assert_not_awaited()


@pytest.mark.asyncio
async def test_access_post_passes_password_revision_selection_and_redirects(member_http):
    from httpx import ASGITransport, AsyncClient

    account_id = uuid.uuid4()
    async with AsyncClient(
        transport=ASGITransport(app=member_http.application),
        base_url="https://test",
        cookies={"reply_admin_csrf": "csrf"},
    ) as client:
        response = await client.post(
            f"/admin/users/{member_http.snapshot.user_id}/access",
            data={
                "csrf_token": "csrf",
                "bootstrap_password": "confirmation",
                "access_revision": "a" * 64,
                f"account_{account_id}": "on",
                "operator_takeover_enabled": "on",
            },
        )
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/users?notice=access-updated"
    arguments = member_http.command.await_args.kwargs
    assert arguments["bootstrap_password"] == "confirmation"
    assert arguments["expected_revision"] == "a" * 64
    assert arguments["selection"] == access.MemberAccessSelection(
        frozenset({account_id}), False, True
    )


@pytest.mark.asyncio
async def test_new_account_grant_is_scoped_and_revokes_prior_authority(member_command):
    account_id = uuid.uuid4()
    member_command.accounts.return_value = [
        SimpleNamespace(id=account_id, owner_user_id=None, tenant_id=DEFAULT_TENANT_ID),
    ]
    await _save_member(member_command, access.MemberAccessSelection(frozenset({account_id})))
    grant = member_command.session.add.call_args_list[0].args[0]
    assert grant.tenant_id == DEFAULT_TENANT_ID
    assert grant.platform_account_id == account_id
    assert grant.user_id == member_command.member.id
    assert grant.active is True
    member_command.revocation.assert_awaited_once()
