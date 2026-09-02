import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from social_reply.application.account_management.auth import (
    hash_password,
    verify_dummy_password,
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


def validate_system_username(username: str) -> str:
    normalized_username = username.strip()
    if (
        not normalized_username
        or len(normalized_username) > 128
        or any(character.isspace() for character in normalized_username)
    ):
        raise SystemUserValidationError("invalid_username")
    if secrets.compare_digest(normalized_username, get_settings().admin_username):
        raise SystemUserConflictError("username_conflicts_with_superadmin")
    return normalized_username


def validate_system_user_role(role: str) -> str:
    normalized_role = role.strip().upper()
    if normalized_role != "USER":
        raise SystemUserValidationError("invalid_user_role")
    return normalized_role


def validate_system_user_status(user_status: str) -> str:
    normalized_status = user_status.strip().lower()
    if normalized_status not in {"active", "disabled"}:
        raise SystemUserValidationError("invalid_user_status")
    return normalized_status


async def require_bootstrap_reauthentication(bootstrap_password: str) -> None:
    if not bootstrap_password:
        raise SystemUserValidationError("bootstrap_password_required")
    expected_password = get_settings().admin_password.get_secret_value()
    if secrets.compare_digest(bootstrap_password, expected_password):
        return
    await verify_dummy_password(bootstrap_password)
    raise SystemUserAuthenticationError("bootstrap_password_invalid")


async def create_system_user(
    *,
    username: str,
    initial_password: str,
    role: str,
    bootstrap_password: str,
    actor: SystemUserActor,
) -> uuid.UUID:
    await require_bootstrap_reauthentication(bootstrap_password)
    normalized_username = validate_system_username(username)
    normalized_role = validate_system_user_role(role)
    try:
        password_hash = await hash_password(initial_password)
    except ValueError as exc:
        raise SystemUserValidationError(str(exc)) from exc

    user_id = uuid.uuid4()
    try:
        async with get_session_factory()() as session, session.begin():
            session.add(
                models.AdminUser(
                    id=user_id,
                    username=normalized_username,
                    password_hash=password_hash,
                    tenant_id=DEFAULT_TENANT_ID,
                    role=normalized_role,
                    must_change_password=True,
                    status="active",
                )
            )
            session.add(
                _audit_log(
                    category="user_management",
                    actor=actor,
                    action="CREATE_USER",
                    user_id=user_id,
                    detail={"username": normalized_username, "role": normalized_role},
                )
            )
    except IntegrityError as exc:
        raise SystemUserConflictError("username_already_exists") from exc
    return user_id


async def set_system_user_status(
    *,
    user_id: uuid.UUID,
    user_status: str,
    bootstrap_password: str,
    actor: SystemUserActor,
) -> None:
    await require_bootstrap_reauthentication(bootstrap_password)
    normalized_status = validate_system_user_status(user_status)
    async with get_session_factory()() as session, session.begin():
        user = await _load_default_user_for_update(session, user_id)
        if user.status == normalized_status:
            return
        await session.execute(
            update(models.AdminUser)
            .where(models.AdminUser.id == user_id)
            .values(status=normalized_status, updated_at=datetime.now(UTC))
        )
        if normalized_status == "disabled":
            await session.execute(
                delete(models.AdminSession).where(models.AdminSession.user_id == user_id)
            )
        session.add(
            _audit_log(
                category="user_management",
                actor=actor,
                action="SET_USER_STATUS",
                user_id=user_id,
                detail={"previous_status": user.status, "status": normalized_status},
            )
        )


async def force_system_user_password_reset(
    *,
    user_id: uuid.UUID,
    initial_password: str,
    bootstrap_password: str,
    actor: SystemUserActor,
) -> None:
    await require_bootstrap_reauthentication(bootstrap_password)
    try:
        password_hash = await hash_password(initial_password)
    except ValueError as exc:
        raise SystemUserValidationError(str(exc)) from exc
    async with get_session_factory()() as session, session.begin():
        await _load_default_user_for_update(session, user_id)
        await session.execute(
            update(models.AdminUser)
            .where(models.AdminUser.id == user_id)
            .values(
                password_hash=password_hash,
                must_change_password=True,
                password_changed_at=None,
                updated_at=datetime.now(UTC),
            )
        )
        await session.execute(
            delete(models.AdminSession).where(models.AdminSession.user_id == user_id)
        )
        session.add(
            _audit_log(
                category="user_management",
                actor=actor,
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
    await require_bootstrap_reauthentication(bootstrap_password)
    async with get_session_factory()() as session, session.begin():
        await _load_default_user_for_update(session, user_id)
        delete_result = await session.execute(
            delete(models.AdminSession).where(models.AdminSession.user_id == user_id)
        )
        session.add(
            _audit_log(
                category="session_management",
                actor=actor,
                action="REVOKE_USER_SESSIONS",
                user_id=user_id,
                detail={"revoked_session_count": int(delete_result.rowcount or 0)},
            )
        )


async def _load_default_user_for_update(session, user_id: uuid.UUID) -> models.AdminUser:
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
        detail={**detail, "actor_session_id": str(actor.session_id)},
    )
