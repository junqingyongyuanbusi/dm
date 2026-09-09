"""Workspace member access commands; all writes share staff authority revocation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select

from social_reply.application.account_management.auth import Principal
from social_reply.application.account_management.system_user_management import (
    SystemUserActor,
    SystemUserAuthenticationError,
    SystemUserConflictError,
    SystemUserNotFoundError,
    SystemUserValidationError,
    _audit_log,
    _load_default_user_for_update,
    _management_transaction,
    _reauthenticate_manager,
    _revoke_staff,
)
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.shared.config import DEFAULT_TENANT_ID

MAX_MEMBER_ACCESS_ACCOUNTS = 500


@dataclass(frozen=True)
class MemberAccessSelection:
    account_ids: frozenset[UUID]
    operator_reply_enabled: bool = False
    operator_takeover_enabled: bool = False


@dataclass(frozen=True)
class MemberAccountAccess:
    account_id: UUID
    name: str
    platform: str
    owned: bool
    granted: bool


@dataclass(frozen=True)
class WorkspaceMemberAccess:
    user_id: UUID
    username: str
    role: str
    operator_reply_enabled: bool
    operator_takeover_enabled: bool
    accounts: tuple[MemberAccountAccess, ...]
    revision: str = ""


def _access_revision(
    member: models.AdminUser,
    accounts: Sequence[models.PlatformAccount],
    grants: Sequence[models.AccountAccessGrant],
) -> str:
    snapshot = {
        "user_id": str(member.id),
        "role": member.role,
        "reply": member.operator_reply_enabled,
        "takeover": member.operator_takeover_enabled,
        "accounts": sorted(
            (str(account.id), account.owner_user_id == member.id) for account in accounts
        ),
        "grants": sorted(str(grant.platform_account_id) for grant in grants if grant.active),
    }
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()


def require_workspace_member_manager(principal: Principal) -> None:
    # Bootstrap administration stays a separate identity/control plane.
    if principal.user_id is None or principal.role != "WORKSPACE_ADMIN":
        raise HTTPException(status_code=403, detail="workspace_admin_required")
    principal.require_tenant(DEFAULT_TENANT_ID)
    if principal.must_change_password:
        raise HTTPException(status_code=403, detail="password_change_required")


def parse_member_access_form(form: Mapping[str, str]) -> MemberAccessSelection:
    account_fields = [key for key in form if key.startswith("account_")]
    if len(account_fields) > MAX_MEMBER_ACCESS_ACCOUNTS:
        raise SystemUserValidationError("member_access_account_limit_exceeded")
    try:
        account_ids = frozenset(UUID(key.removeprefix("account_")) for key in account_fields)
    except ValueError as exc:
        raise SystemUserValidationError("invalid_platform_account_id") from exc
    switch_names = ("operator_reply_enabled", "operator_takeover_enabled")
    if any(form[key] != "on" for key in account_fields):
        raise SystemUserValidationError("invalid_account_access_selection")
    if any(name in form and form[name] != "on" for name in switch_names):
        raise SystemUserValidationError("invalid_operator_permission")
    return MemberAccessSelection(
        account_ids=account_ids,
        operator_reply_enabled=form.get("operator_reply_enabled") == "on",
        operator_takeover_enabled=form.get("operator_takeover_enabled") == "on",
    )


def validate_member_account_scope(
    account_ids: frozenset[UUID], accounts: Sequence[models.PlatformAccount]
) -> None:
    allowed_ids = {account.id for account in accounts if account.tenant_id == DEFAULT_TENANT_ID}
    if not account_ids.issubset(allowed_ids):
        raise SystemUserNotFoundError("platform_account_not_found")


async def _load_member(session, user_id: UUID) -> models.AdminUser:
    member = await session.scalar(
        select(models.AdminUser).where(
            models.AdminUser.id == user_id,
            models.AdminUser.tenant_id == DEFAULT_TENANT_ID,
        )
    )
    if member is None:
        raise SystemUserNotFoundError("system_user_not_found")
    return member


async def _load_accounts(session) -> list[models.PlatformAccount]:
    accounts = list(
        await session.scalars(
            select(models.PlatformAccount)
            .where(models.PlatformAccount.tenant_id == DEFAULT_TENANT_ID)
            .order_by(models.PlatformAccount.id)
            .limit(MAX_MEMBER_ACCESS_ACCOUNTS + 1)
        )
    )
    # Never silently truncate a replacement form and revoke unseen grants.
    if len(accounts) > MAX_MEMBER_ACCESS_ACCOUNTS:
        raise SystemUserValidationError("member_access_account_limit_exceeded")
    return accounts


async def _load_grants(session, user_id: UUID) -> list[models.AccountAccessGrant]:
    grants = list(
        await session.scalars(
            select(models.AccountAccessGrant)
            .where(
                models.AccountAccessGrant.tenant_id == DEFAULT_TENANT_ID,
                models.AccountAccessGrant.user_id == user_id,
            )
            .order_by(models.AccountAccessGrant.id)
            .limit(MAX_MEMBER_ACCESS_ACCOUNTS + 1)
        )
    )
    if len(grants) > MAX_MEMBER_ACCESS_ACCOUNTS:
        raise SystemUserValidationError("member_access_account_limit_exceeded")
    return grants


async def get_workspace_member_access(
    *, principal: Principal, user_id: UUID
) -> WorkspaceMemberAccess:
    require_workspace_member_manager(principal)
    async with get_session_factory()() as session:
        member = await _load_member(session, user_id)
        accounts = await _load_accounts(session)
        grants = await _load_grants(session, user_id)
        granted_ids = {grant.platform_account_id for grant in grants if grant.active}
        return WorkspaceMemberAccess(
            user_id=member.id,
            username=member.username,
            role=member.role,
            operator_reply_enabled=member.operator_reply_enabled,
            operator_takeover_enabled=member.operator_takeover_enabled,
            accounts=tuple(
                MemberAccountAccess(
                    account_id=account.id,
                    name=account.name,
                    platform=account.platform,
                    owned=account.owner_user_id == user_id,
                    granted=account.id in granted_ids,
                )
                for account in accounts
            ),
            revision=_access_revision(member, accounts, grants),
        )


async def set_workspace_member_access(
    *,
    user_id: UUID,
    selection: MemberAccessSelection,
    bootstrap_password: str,
    actor: SystemUserActor,
    expected_revision: str,
) -> None:
    if (
        not isinstance(selection.account_ids, frozenset)
        or any(not isinstance(account_id, UUID) for account_id in selection.account_ids)
        or len(selection.account_ids) > MAX_MEMBER_ACCESS_ACCOUNTS
        or type(selection.operator_reply_enabled) is not bool
        or type(selection.operator_takeover_enabled) is not bool
    ):
        raise SystemUserValidationError("invalid_member_access_selection")
    if len(expected_revision) != 64 or any(
        character not in "0123456789abcdef" for character in expected_revision
    ):
        raise SystemUserValidationError("invalid_member_access_revision")
    await _reauthenticate_manager(actor, bootstrap_password)
    async with _management_transaction(actor, (user_id,)) as (session, current_actor):
        if current_actor.user_id is None:
            raise SystemUserAuthenticationError("workspace_admin_required")
        # Read scope after authority locks, but before taking business-row locks.
        member = await _load_member(session, user_id)
        accounts = await _load_accounts(session)
        validate_member_account_scope(selection.account_ids, accounts)
        grants = await _load_grants(session, user_id)
        if expected_revision != _access_revision(member, accounts, grants):
            raise SystemUserConflictError("member_access_changed")
        owned_ids = frozenset(
            account.id for account in accounts if account.owner_user_id == user_id
        )
        desired_ids = selection.account_ids - owned_ids
        previous_ids = frozenset(grant.platform_account_id for grant in grants if grant.active)
        previous_reply = member.operator_reply_enabled
        previous_takeover = member.operator_takeover_enabled
        if (
            desired_ids == previous_ids
            and selection.operator_reply_enabled == previous_reply
            and selection.operator_takeover_enabled == previous_takeover
        ):
            return
        # This cancels pending human sends, releases claims and revokes sessions.
        # It must precede account/grant/user row locks to preserve lifecycle lock order.
        await _revoke_staff(session, user_id, "MEMBER_ACCESS_CHANGED")
        member = await _load_default_user_for_update(session, user_id)
        existing_grants = {grant.platform_account_id: grant for grant in grants}
        for grant in grants:
            grant.active = grant.platform_account_id in desired_ids
        for account_id in sorted(desired_ids - existing_grants.keys(), key=str):
            session.add(
                models.AccountAccessGrant(
                    tenant_id=DEFAULT_TENANT_ID,
                    user_id=user_id,
                    platform_account_id=account_id,
                    active=True,
                )
            )
        member.operator_reply_enabled = selection.operator_reply_enabled
        member.operator_takeover_enabled = selection.operator_takeover_enabled
        session.add(
            _audit_log(
                category="user_management",
                actor=current_actor,
                action="SET_MEMBER_ACCESS",
                user_id=user_id,
                detail={
                    "previous_account_ids": sorted(map(str, previous_ids)),
                    "account_ids": sorted(map(str, desired_ids)),
                    "owned_account_ids": sorted(map(str, owned_ids)),
                    "previous_operator_reply_enabled": previous_reply,
                    "operator_reply_enabled": selection.operator_reply_enabled,
                    "previous_operator_takeover_enabled": previous_takeover,
                    "operator_takeover_enabled": selection.operator_takeover_enabled,
                    "sessions_revoked": True,
                },
            )
        )
