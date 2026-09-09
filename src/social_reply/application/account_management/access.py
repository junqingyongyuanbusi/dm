"""Company account access shared by HTTP commands and background workers."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import and_, false, or_, select

from social_reply.application.account_management.permissions import SUPPORTED_ROLES
from social_reply.infrastructure.database import models

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from social_reply.application.account_management.auth import Principal


def account_read_condition(principal: Principal, tenant_id: str):
    if tenant_id not in principal.allowed_tenants:
        return false()
    tenant = models.PlatformAccount.tenant_id == tenant_id
    if principal.is_workspace_admin:
        return tenant
    if principal.user_id is None or principal.role not in SUPPORTED_ROLES:
        return false()
    return and_(
        tenant,
        or_(
            models.PlatformAccount.owner_user_id == principal.user_id,
            select(models.AccountAccessGrant.id).where(
                models.AccountAccessGrant.tenant_id == models.PlatformAccount.tenant_id,
                models.AccountAccessGrant.platform_account_id == models.PlatformAccount.id,
                models.AccountAccessGrant.user_id == principal.user_id,
                models.AccountAccessGrant.active.is_(True),
            ).correlate(models.PlatformAccount).exists(),
        ),
    )


def user_can_access_account(
    user: models.AdminUser, account: models.PlatformAccount,
    *, account_access_ids: frozenset[uuid.UUID] = frozenset(),
) -> bool:
    return (
        user.status == "active"
        and user.tenant_id == account.tenant_id
        and user.role in SUPPORTED_ROLES
        and (
            user.role == "WORKSPACE_ADMIN"
            or account.owner_user_id == user.id
            or account.id in account_access_ids
        )
    )


async def user_can_access_account_in_session(
    session: AsyncSession, user: models.AdminUser, account: models.PlatformAccount,
) -> bool:
    if user_can_access_account(user, account):
        return True
    if (
        user.status != "active"
        or user.tenant_id != account.tenant_id
        or user.role not in SUPPORTED_ROLES
    ):
        return False
    return await session.scalar(select(models.AccountAccessGrant.id).where(
        models.AccountAccessGrant.tenant_id == account.tenant_id,
        models.AccountAccessGrant.platform_account_id == account.id,
        models.AccountAccessGrant.user_id == user.id,
        models.AccountAccessGrant.active.is_(True),
    )) is not None


async def require_reauthorization(
    session: AsyncSession,
    *,
    principal: Principal,
    account: models.PlatformAccount,
    expected_config_version: int | None = None,
) -> None:
    if (
        account.tenant_id not in principal.allowed_tenants
        or not principal.has_capability("connect")
        or not principal.can_access_account(account)
    ):
        raise PermissionError("account_reauthorization_denied")
    if expected_config_version is not None and account.config_version != expected_config_version:
        raise ValueError("account_reauthorization_version_conflict")
    if principal.is_workspace_admin:
        return
    if principal.user_id is None:
        raise PermissionError("account_reauthorization_denied")
    granted = await session.scalar(
        select(models.AccountReauthorizationGrant.id).where(
            models.AccountReauthorizationGrant.tenant_id == account.tenant_id,
            models.AccountReauthorizationGrant.platform_account_id == account.id,
            models.AccountReauthorizationGrant.user_id == principal.user_id,
            models.AccountReauthorizationGrant.active.is_(True),
        )
    )
    if granted is None:
        raise PermissionError("account_reauthorization_denied")


async def lock_user_authority(session: AsyncSession, user_id: uuid.UUID) -> None:
    # Mutating staff authority and executing their commands use the same transaction lock.
    from sqlalchemy import text

    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"social-reply:staff-authority:{user_id}"},
    )


def session_authority_key(session_id: uuid.UUID) -> str:
    return f"social-reply:session-authority:{session_id}"


async def lock_session_authorities(session: AsyncSession, session_ids) -> None:
    from sqlalchemy import text

    for session_id in sorted(set(session_ids), key=str):
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": session_authority_key(session_id)},
        )
