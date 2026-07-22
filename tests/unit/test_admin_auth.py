import uuid

import pytest
from fastapi import HTTPException

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
