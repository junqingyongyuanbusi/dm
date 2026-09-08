import secrets
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError

from social_reply.application.account_management.access import (
    lock_session_authorities,
    lock_user_authority,
)
from social_reply.application.account_management.auth import (
    hash_password,
    principal_from_session_id,
    principal_from_session_row,
    verify_dummy_password,
    verify_password,
)
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.shared.config import DEFAULT_TENANT_ID, get_settings


class SystemUserManagementError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class SystemUserAuthenticationError(SystemUserManagementError):
    pass


class SystemUserValidationError(SystemUserManagementError):
    pass


class SystemUserConflictError(SystemUserManagementError):
    pass


class SystemUserNotFoundError(SystemUserManagementError):
    pass


@dataclass(frozen=True)
class SystemUserActor:
    actor: str
    session_id: uuid.UUID
    user_id: uuid.UUID | None = None


def validate_system_username(username: str) -> str:
    normalized = username.strip()
    if not normalized or len(normalized) > 128 or any(c.isspace() for c in normalized):
        raise SystemUserValidationError("invalid_username")
    if secrets.compare_digest(normalized, get_settings().admin_username):
        raise SystemUserConflictError("username_conflicts_with_superadmin")
    return normalized


def validate_system_user_role(role: str) -> str:
    normalized = role.strip().upper()
    if normalized not in {"USER", "WORKSPACE_ADMIN"}:
        raise SystemUserValidationError("invalid_user_role")
    return normalized


def validate_system_user_status(user_status: str) -> str:
    normalized = user_status.strip().lower()
    if normalized not in {"active", "disabled"}:
        raise SystemUserValidationError("invalid_user_status")
    return normalized


async def require_bootstrap_reauthentication(bootstrap_password: str) -> None:
    if not bootstrap_password:
        raise SystemUserValidationError("bootstrap_password_required")
    if secrets.compare_digest(bootstrap_password, get_settings().admin_password.get_secret_value()):
        return
    await verify_dummy_password(bootstrap_password)
    raise SystemUserAuthenticationError("bootstrap_password_invalid")


async def _reauthenticate_manager(actor: SystemUserActor, password: str) -> None:
    principal = await principal_from_session_id(actor.session_id)
    if (
        principal is None
        or principal.must_change_password
        or not principal.is_workspace_admin
        or DEFAULT_TENANT_ID not in principal.allowed_tenants
    ):
        raise SystemUserAuthenticationError("user_management_access_denied")
    if principal.is_superadmin:
        await require_bootstrap_reauthentication(password)
        return
    async with get_session_factory()() as session:
        user = await session.get(models.AdminUser, principal.user_id)
        if user is not None and await verify_password(user.password_hash, password):
            return
    raise SystemUserAuthenticationError("confirmation_password_invalid")


@asynccontextmanager
async def _management_transaction(
    actor: SystemUserActor, target_user_ids: tuple[uuid.UUID, ...] = ()
):
    async with get_session_factory()() as session, session.begin():
        # Serialize role/status changes, including the last-admin invariant.
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"social-reply:workspace-users:{DEFAULT_TENANT_ID}"},
        )
        principal = await principal_from_session_row(session, actor.session_id)
        initial_user_id = principal.user_id if principal is not None else None
        staff_ids = set(target_user_ids)
        if initial_user_id is not None:
            staff_ids.add(initial_user_id)
        for staff_id in sorted(staff_ids, key=str):
            await lock_user_authority(session, staff_id)
        session_ids = {actor.session_id}
        if staff_ids:
            session_ids.update(
                await session.scalars(
                    select(models.AdminSession.id).where(models.AdminSession.user_id.in_(staff_ids))
                )
            )
        await lock_session_authorities(session, session_ids)
        session.expire_all()
        principal = await principal_from_session_row(session, actor.session_id, for_update=True)
        if principal is not None and principal.user_id != initial_user_id:
            raise SystemUserAuthenticationError("user_management_identity_changed")
        if (
            principal is None
            or principal.must_change_password
            or not principal.is_workspace_admin
            or DEFAULT_TENANT_ID not in principal.allowed_tenants
        ):
            raise SystemUserAuthenticationError("user_management_access_denied")
        yield session, SystemUserActor(principal.actor, principal.session_id, principal.user_id)


async def create_system_user(
    *,
    username: str,
    initial_password: str,
    role: str,
    bootstrap_password: str,
    actor: SystemUserActor,
) -> uuid.UUID:
    await _reauthenticate_manager(actor, bootstrap_password)
    username = validate_system_username(username)
    role = validate_system_user_role(role)
    try:
        password_hash = await hash_password(initial_password)
    except ValueError as exc:
        raise SystemUserValidationError(str(exc)) from exc
    user_id = uuid.uuid4()
    try:
        async with _management_transaction(actor) as (session, current_actor):
            session.add(
                models.AdminUser(
                    id=user_id,
                    username=username,
                    password_hash=password_hash,
                    tenant_id=DEFAULT_TENANT_ID,
                    role=role,
                    must_change_password=True,
                    status="active",
                )
            )
            session.add(
                _audit_log(
                    category="user_management",
                    actor=current_actor,
                    action="CREATE_USER",
                    user_id=user_id,
                    detail={"username": username, "role": role},
                )
            )
    except IntegrityError as exc:
        raise SystemUserConflictError("username_already_exists") from exc
    return user_id


async def _protect_last_admin(
    session, user: models.AdminUser, *, actor: SystemUserActor, emergency_reason: str
) -> None:
    if user.role != "WORKSPACE_ADMIN" or user.status != "active":
        return
    remaining = await session.scalar(
        select(func.count())
        .select_from(models.AdminUser)
        .where(
            models.AdminUser.tenant_id == user.tenant_id,
            models.AdminUser.role == "WORKSPACE_ADMIN",
            models.AdminUser.status == "active",
            models.AdminUser.id != user.id,
        )
    )
    if remaining:
        return
    if actor.user_id is None:
        # The transaction reconstructed this actor from an authenticated bootstrap session.
        if not emergency_reason.strip() or len(emergency_reason) > 500:
            raise SystemUserValidationError("bootstrap_emergency_reason_required")
        return
    raise SystemUserConflictError("last_workspace_admin_required")


async def _revoke_staff(session, user_id: uuid.UUID, reason: str) -> None:
    from social_reply.application.account_management.staff_lifecycle import revoke_staff_authority

    await revoke_staff_authority(session, user_id=user_id, reason=reason)
    await session.execute(delete(models.AdminSession).where(models.AdminSession.user_id == user_id))


async def set_system_user_status(
    *,
    user_id: uuid.UUID,
    user_status: str,
    bootstrap_password: str,
    actor: SystemUserActor,
    emergency_reason: str = "",
) -> None:
    await _reauthenticate_manager(actor, bootstrap_password)
    target = validate_system_user_status(user_status)
    async with _management_transaction(actor, (user_id,)) as (session, current_actor):
        user = await _load_default_user_for_update(session, user_id)
        previous = user.status
        if previous == target:
            return
        if target == "disabled":
            await _protect_last_admin(
                session, user, actor=current_actor, emergency_reason=emergency_reason
            )
            await _revoke_staff(session, user.id, "USER_DISABLED")
        user.status = target
        session.add(
            _audit_log(
                category="user_management",
                actor=current_actor,
                action="SET_USER_STATUS",
                user_id=user_id,
                detail={
                    "previous_status": previous,
                    "status": target,
                    "emergency_reason": emergency_reason.strip(),
                    "bootstrap_emergency": current_actor.user_id is None
                    and bool(emergency_reason.strip()),
                },
            )
        )


async def set_system_user_role(
    *,
    user_id: uuid.UUID,
    role: str,
    bootstrap_password: str,
    actor: SystemUserActor,
    emergency_reason: str = "",
) -> None:
    await _reauthenticate_manager(actor, bootstrap_password)
    target = validate_system_user_role(role)
    async with _management_transaction(actor, (user_id,)) as (session, current_actor):
        user = await _load_default_user_for_update(session, user_id)
        previous = user.role
        if previous == target:
            return
        if target == "USER":
            await _protect_last_admin(
                session, user, actor=current_actor, emergency_reason=emergency_reason
            )
        await _revoke_staff(session, user.id, "USER_ROLE_CHANGED")
        user.role = target
        session.add(
            _audit_log(
                category="user_management",
                actor=current_actor,
                action="SET_USER_ROLE",
                user_id=user_id,
                detail={
                    "previous_role": previous,
                    "role": target,
                    "emergency_reason": emergency_reason.strip(),
                    "bootstrap_emergency": current_actor.user_id is None
                    and bool(emergency_reason.strip()),
                },
            )
        )


async def force_system_user_password_reset(
    *,
    user_id: uuid.UUID,
    initial_password: str,
    bootstrap_password: str,
    actor: SystemUserActor,
) -> None:
    await _reauthenticate_manager(actor, bootstrap_password)
    try:
        password_hash = await hash_password(initial_password)
    except ValueError as exc:
        raise SystemUserValidationError(str(exc)) from exc
    async with _management_transaction(actor, (user_id,)) as (session, current_actor):
        user = await _load_default_user_for_update(session, user_id)
        await _revoke_staff(session, user.id, "PASSWORD_RESET")
        user.password_hash = password_hash
        user.must_change_password = True
        user.password_changed_at = None
        user.updated_at = datetime.now(UTC)
        session.add(
            _audit_log(
                category="user_management",
                actor=current_actor,
                action="FORCE_PASSWORD_RESET",
                user_id=user_id,
                detail={"must_change_password": True, "sessions_revoked": True},
            )
        )


async def revoke_system_user_sessions(
    *,
    user_id: uuid.UUID,
    bootstrap_password: str,
    actor: SystemUserActor,
) -> None:
    await _reauthenticate_manager(actor, bootstrap_password)
    async with _management_transaction(actor, (user_id,)) as (session, current_actor):
        user = await _load_default_user_for_update(session, user_id)
        count = await session.scalar(
            select(func.count())
            .select_from(models.AdminSession)
            .where(models.AdminSession.user_id == user.id)
        )
        await _revoke_staff(session, user.id, "SESSIONS_REVOKED")
        session.add(
            _audit_log(
                category="session_management",
                actor=current_actor,
                action="REVOKE_USER_SESSIONS",
                user_id=user_id,
                detail={"revoked_session_count": int(count or 0)},
            )
        )


async def _load_default_user_for_update(session, user_id: uuid.UUID) -> models.AdminUser:
    # All staff locks were acquired in sorted order by _management_transaction.
    user = await session.scalar(
        select(models.AdminUser)
        .where(
            models.AdminUser.id == user_id,
            models.AdminUser.tenant_id == DEFAULT_TENANT_ID,
        )
        .with_for_update()
    )
    if user is None:
        raise SystemUserNotFoundError("system_user_not_found")
    return user


def _audit_log(
    *,
    category: str,
    actor: SystemUserActor,
    action: str,
    user_id: uuid.UUID,
    detail: dict[str, object],
) -> models.AuditLog:
    return models.AuditLog(
        tenant_id=DEFAULT_TENANT_ID,
        category=category,
        actor=actor.actor,
        action=action,
        subject_type="admin_user",
        subject_id=str(user_id),
        detail={
            **detail,
            "actor_session_id": str(actor.session_id),
            "actor_user_id": str(actor.user_id) if actor.user_id else None,
        },
    )
