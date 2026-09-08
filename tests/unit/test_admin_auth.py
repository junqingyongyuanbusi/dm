import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from social_reply.application.account_management import auth
from social_reply.application.account_management.auth import (
    Principal,
    hash_password,
    validate_password,
    verify_password,
)


async def test_password_hash_is_argon2_and_verifies():
    value = await hash_password("correct-horse-battery-staple")
    assert value.startswith("$argon2id$")
    assert "correct-horse" not in value
    assert await verify_password(value, "correct-horse-battery-staple")
    assert not await verify_password(value, "wrong-password")


def test_password_policy_rejects_short_values():
    with pytest.raises(ValueError, match="password_length"):
        validate_password("short")


def test_principal_tenant_scope():
    principal = Principal(
        session_id=uuid.uuid4(),
        username="alice",
        actor="user:alice",
        user_id=uuid.uuid4(),
        tenant_id="tenant-a",
        allowed_tenants=frozenset({"tenant-a"}),
    )
    principal.require_tenant("tenant-a")
    with pytest.raises(HTTPException, match="tenant_access_denied"):
        principal.require_tenant("tenant-b")


def test_bootstrap_principal_requires_verified_marker(monkeypatch):
    monkeypatch.setattr(
        auth,
        "get_settings",
        lambda: SimpleNamespace(
            admin_username="bootstrap-admin",
            allowed_admin_tenants=frozenset({"default", "tenant-a"}),
        ),
    )

    unverified = auth._bootstrap_principal(uuid.uuid4())
    verified = auth._bootstrap_principal(uuid.uuid4(), verified=True)

    callback_shape = Principal(
        session_id=None,
        username="callback",
        actor="user:callback",
        allowed_tenants=frozenset({"default"}),
        role="SUPERADMIN",
        authentication_kind="FEISHU_ACTION",
    )
    assert unverified.is_superadmin is False
    assert callback_shape.is_superadmin is False
    assert verified.role == "SUPERADMIN"
    assert verified.tenant_id is None
    assert verified.is_superadmin is True
    assert verified.is_admin is True
    verified.require_superadmin()
    verified.require_admin()
    verified.require_tenant_admin()


@pytest.mark.parametrize("role", ["ADMIN", "SUPERADMIN"])
def test_database_role_string_does_not_grant_admin_permissions(role: str):
    database_user = Principal(
        session_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        username="tenant-admin",
        actor="user:tenant-admin",
        tenant_id="default",
        allowed_tenants=frozenset({"default"}),
        role=role,
    )

    assert database_user.is_admin is False
    assert database_user.is_superadmin is False
    with pytest.raises(HTTPException, match="admin_required"):
        database_user.require_admin()
    with pytest.raises(HTTPException, match="tenant_admin_required"):
        database_user.require_tenant_admin()
    with pytest.raises(HTTPException, match="superadmin_required"):
        database_user.require_superadmin()


def test_principal_default_role_does_not_grant_admin_to_database_user():
    principal = Principal(
        session_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        username="ordinary-user",
        actor="user:ordinary-user",
        tenant_id="default",
        allowed_tenants=frozenset({"default"}),
    )

    assert principal.role == "USER"
    assert principal.is_admin is False
    assert principal.is_superadmin is False
    with pytest.raises(HTTPException, match="admin_required"):
        principal.require_admin()
    with pytest.raises(HTTPException, match="tenant_admin_required"):
        principal.require_tenant_admin()
    with pytest.raises(HTTPException, match="superadmin_required"):
        principal.require_superadmin()


def test_unverified_superadmin_shape_cannot_access_accounts_across_owners():
    principal = Principal(
        session_id=uuid.uuid4(),
        username="system-admin",
        actor="bootstrap:system-admin",
        allowed_tenants=frozenset({"default"}),
        role="SUPERADMIN",
    )
    unowned_account = SimpleNamespace(
        tenant_id="default",
        owner_user_id=None,
    )

    assert principal.is_superadmin is False
    assert principal.is_admin is False
    assert principal.can_access_account(unowned_account) is False
    with pytest.raises(HTTPException, match="platform_account_not_found"):
        principal.require_account(unowned_account)


def test_database_user_account_access_remains_owner_scoped():
    user_id = uuid.uuid4()
    principal = Principal(
        session_id=uuid.uuid4(),
        user_id=user_id,
        username="ordinary-user",
        actor="user:ordinary-user",
        tenant_id="default",
        allowed_tenants=frozenset({"default"}),
    )
    owned_account = SimpleNamespace(tenant_id="default", owner_user_id=user_id)
    sibling_account = SimpleNamespace(tenant_id="default", owner_user_id=uuid.uuid4())
    unowned_account = SimpleNamespace(tenant_id="default", owner_user_id=None)

    assert principal.can_access_account(owned_account) is True
    assert principal.can_access_account(sibling_account) is False
    assert principal.can_access_account(unowned_account) is False
