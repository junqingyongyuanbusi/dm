import asyncio
import hashlib
import hmac
import secrets
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import HTTPException, Request
from sqlalchemy import delete, select

from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.shared.config import DEFAULT_TENANT_ID, get_settings

_SESSION_TTL = timedelta(hours=8)
_PASSWORD_MIN_LENGTH = 12
_PASSWORD_MAX_LENGTH = 128
_PASSWORD_HASHER = PasswordHasher()
_DUMMY_HASH = _PASSWORD_HASHER.hash("not-a-real-password-value")
_PASSWORD_WORK_LIMIT: asyncio.Semaphore | None = None


def _password_work_limit() -> asyncio.Semaphore:
    global _PASSWORD_WORK_LIMIT
    if _PASSWORD_WORK_LIMIT is None:
        _PASSWORD_WORK_LIMIT = asyncio.Semaphore(4)
    return _PASSWORD_WORK_LIMIT


_BOOTSTRAP_MARKER = object()


@dataclass(frozen=True)
class FeishuActionProof:
    """Durable identity for a callback-authenticated Feishu card action."""

    receipt_id: uuid.UUID
    tenant_id: str
    notification_account_id: uuid.UUID
    notification_intent_id: uuid.UUID
    customer_account_id: uuid.UUID
    operator_open_id: str
    action: str
    action_nonce: uuid.UUID
    request_digest: str
    provider_message_id: str


@dataclass(frozen=True)
class Principal:
    session_id: uuid.UUID | None
    username: str
    actor: str
    allowed_tenants: frozenset[str]
    user_id: uuid.UUID | None = None
    tenant_id: str | None = None
    must_change_password: bool = False
    role: str = "USER"
    authentication_kind: str = "SESSION"
    action_proof: FeishuActionProof | None = None
    _bootstrap_marker: object | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )

    @property
    def is_superadmin(self) -> bool:
        # SUPERADMIN is exclusively the environment-backed bootstrap identity;
        # a database user or callback proof must never gain this authority from a role string.
        return (
            self.authentication_kind == "SESSION"
            and self.session_id is not None
            and self.user_id is None
            and self.action_proof is None
            and self._bootstrap_marker is _BOOTSTRAP_MARKER
            and self.role == "SUPERADMIN"
        )

    @property
    def is_admin(self) -> bool:
        # Business capability only; system routes must require is_superadmin explicitly.
        return self.is_workspace_admin

    @property
    def is_workspace_admin(self) -> bool:
        return self.is_superadmin or (self.user_id is not None and self.role == "WORKSPACE_ADMIN")

    @property
    def is_feishu_action(self) -> bool:
        return self.authentication_kind == "FEISHU_ACTION"

    def require_tenant(self, tenant_id: str) -> None:
        if tenant_id not in self.allowed_tenants:
            raise HTTPException(status_code=403, detail="tenant_access_denied")

    def can_access_account(self, account: models.PlatformAccount) -> bool:
        if account.tenant_id not in self.allowed_tenants:
            return False
        return self.is_workspace_admin or (
            self.user_id is not None
            and (account.owner_user_id == self.user_id or account.shared_with_support is True)
        )

    def require_admin(self) -> None:
        if not self.is_workspace_admin:
            raise HTTPException(status_code=403, detail="admin_required")

    def require_tenant_admin(self) -> None:
        if not self.is_workspace_admin:
            raise HTTPException(status_code=403, detail="tenant_admin_required")

    def require_superadmin(self) -> None:
        if not self.is_superadmin:
            raise HTTPException(status_code=403, detail="superadmin_required")

    def require_account(self, account: models.PlatformAccount) -> None:
        if not self.can_access_account(account):
            raise HTTPException(status_code=404, detail="platform_account_not_found")


def validate_password(password: str) -> None:
    if not _PASSWORD_MIN_LENGTH <= len(password) <= _PASSWORD_MAX_LENGTH:
        raise ValueError(
            f"password_length_must_be_between_{_PASSWORD_MIN_LENGTH}_and_{_PASSWORD_MAX_LENGTH}"
        )


async def hash_password(password: str) -> str:
    validate_password(password)
    async with _password_work_limit():
        return await asyncio.to_thread(_PASSWORD_HASHER.hash, password)


async def verify_password(password_hash: str, password: str) -> bool:
    try:
        async with _password_work_limit():
            return await asyncio.to_thread(_PASSWORD_HASHER.verify, password_hash, password)
    except (InvalidHashError, VerificationError, VerifyMismatchError):
        return False


async def password_needs_rehash(password_hash: str) -> bool:
    try:
        async with _password_work_limit():
            return await asyncio.to_thread(_PASSWORD_HASHER.check_needs_rehash, password_hash)
    except InvalidHashError:
        return False


async def verify_dummy_password(password: str) -> None:
    await verify_password(_DUMMY_HASH, password)


def _hmac_hex(domain: str, value: str) -> str:
    secret = get_settings().admin_session_secret.get_secret_value().encode()
    return hmac.new(secret, f"{domain}:{value}".encode(), hashlib.sha256).hexdigest()


def _token_digest(token: str) -> str:
    return _hmac_hex("admin-session", token)


def _bootstrap_fingerprint() -> str:
    settings = get_settings()
    value = f"{settings.admin_username}\0{settings.admin_password.get_secret_value()}"
    return _hmac_hex("bootstrap-admin", value)


def _credential_fingerprint(password_hash: str) -> str:
    return _hmac_hex("admin-credential", password_hash)


async def issue_session() -> tuple[str, uuid.UUID]:
    raw_token = secrets.token_urlsafe(32)
    session_id = uuid.uuid4()
    async with get_session_factory()() as session:
        session.add(
            models.AdminSession(
                id=session_id,
                token_digest=_token_digest(raw_token),
                user_id=None,
                bootstrap_fingerprint=_bootstrap_fingerprint(),
                credential_fingerprint=None,
                expires_at=datetime.now(UTC) + _SESSION_TTL,
            )
        )
        settings = get_settings()
        session.add(
            models.AuditLog(
                tenant_id=DEFAULT_TENANT_ID,
                category="authentication",
                actor=f"user:{settings.admin_username}",
                action="LOGIN_SUCCESS",
                subject_type="bootstrap_admin",
                subject_id=settings.admin_username,
                detail={"username": settings.admin_username, "role": "SUPERADMIN"},
            )
        )
        await session.commit()
    return raw_token, session_id


async def revoke_session(raw_token: str) -> None:
    if not raw_token:
        return
    async with get_session_factory()() as session:
        stored_session = await session.scalar(
            select(models.AdminSession).where(
                models.AdminSession.token_digest == _token_digest(raw_token)
            )
        )
        if stored_session is None:
            return
        if stored_session.user_id is None:
            settings = get_settings()
            tenant_id = DEFAULT_TENANT_ID
            actor = f"user:{settings.admin_username}"
            subject_type = "bootstrap_admin"
            subject_id = settings.admin_username
            detail = {"username": settings.admin_username, "role": "SUPERADMIN"}
        else:
            from social_reply.application.account_management.access import lock_user_authority

            await lock_user_authority(session, stored_session.user_id)
            user = await session.get(models.AdminUser, stored_session.user_id)
            if user is None:
                return
            tenant_id = user.tenant_id
            actor = f"user:{user.username}"
            subject_type = "admin_user"
            subject_id = str(user.id)
            detail = {"username": user.username, "role": user.role}
        from social_reply.application.account_management.access import lock_session_authorities

        await lock_session_authorities(session, (stored_session.id,))
        await session.execute(
            delete(models.AdminSession).where(
                models.AdminSession.token_digest == _token_digest(raw_token)
            )
        )
        session.add(
            models.AuditLog(
                tenant_id=tenant_id,
                category="authentication",
                actor=actor,
                action="LOGOUT",
                subject_type=subject_type,
                subject_id=subject_id,
                detail=detail,
            )
        )
        await session.commit()


async def authenticate(username: str, password: str) -> tuple[Principal, str] | None:
    settings = get_settings()
    if secrets.compare_digest(username, settings.admin_username):
        if not secrets.compare_digest(password, settings.admin_password.get_secret_value()):
            await verify_dummy_password(password)
            return None
        raw_token, session_id = await issue_session()
        return _bootstrap_principal(session_id, verified=True), raw_token

    async with get_session_factory()() as session:
        user = (
            await session.execute(
                select(models.AdminUser).where(models.AdminUser.username == username)
            )
        ).scalar_one_or_none()
    if user is None:
        await verify_dummy_password(password)
        return None
    if user.status != "active" or user.tenant_id not in settings.allowed_admin_tenants:
        await verify_dummy_password(password)
        return None
    if not await verify_password(user.password_hash, password):
        return None
    verified_hash = user.password_hash
    async with get_session_factory()() as session:
        from social_reply.application.account_management.access import lock_user_authority

        await lock_user_authority(session, user.id)
        stored_user = (
            await session.execute(
                select(models.AdminUser).where(models.AdminUser.id == user.id).with_for_update()
            )
        ).scalar_one_or_none()
        if (
            stored_user is None
            or stored_user.status != "active"
            or stored_user.tenant_id not in settings.allowed_admin_tenants
            or stored_user.password_hash != verified_hash
        ):
            return None
        if await password_needs_rehash(stored_user.password_hash):
            stored_user.password_hash = await hash_password(password)
        raw_token = secrets.token_urlsafe(32)
        session_id = uuid.uuid4()
        session.add(
            models.AdminSession(
                id=session_id,
                token_digest=_token_digest(raw_token),
                user_id=stored_user.id,
                bootstrap_fingerprint=None,
                credential_fingerprint=_credential_fingerprint(stored_user.password_hash),
                expires_at=datetime.now(UTC) + _SESSION_TTL,
            )
        )
        session.add(
            models.AuditLog(
                tenant_id=stored_user.tenant_id,
                category="authentication",
                actor=f"user:{stored_user.username}",
                action="LOGIN_SUCCESS",
                subject_type="admin_user",
                subject_id=str(stored_user.id),
                detail={"username": stored_user.username, "role": stored_user.role},
            )
        )
        await session.commit()
        principal = _user_principal(session_id, stored_user)
    return principal, raw_token


def _bootstrap_principal(session_id: uuid.UUID, *, verified: bool = False) -> Principal:
    settings = get_settings()
    principal = Principal(
        session_id=session_id,
        username=settings.admin_username,
        actor=f"user:{settings.admin_username}",
        allowed_tenants=settings.allowed_admin_tenants,
        tenant_id=None,
        role="SUPERADMIN",
    )
    if verified:
        object.__setattr__(principal, "_bootstrap_marker", _BOOTSTRAP_MARKER)
    return principal


def _user_principal(session_id: uuid.UUID, user: models.AdminUser) -> Principal:
    return Principal(
        session_id=session_id,
        user_id=user.id,
        username=user.username,
        actor=f"user:{user.username}",
        tenant_id=user.tenant_id,
        allowed_tenants=frozenset({user.tenant_id}),
        must_change_password=user.must_change_password,
        role=user.role,
    )


async def principal_from_token(raw_token: str) -> Principal | None:
    if not raw_token:
        return None
    now = datetime.now(UTC)
    digest = _token_digest(raw_token)
    async with get_session_factory()() as session:
        row = (
            await session.execute(
                select(models.AdminSession, models.AdminUser)
                .outerjoin(models.AdminUser, models.AdminSession.user_id == models.AdminUser.id)
                .where(
                    models.AdminSession.token_digest == digest,
                    models.AdminSession.expires_at > now,
                )
            )
        ).one_or_none()
    if row is None:
        return None
    stored_session, user = row
    if stored_session.user_id is None:
        if not hmac.compare_digest(
            stored_session.bootstrap_fingerprint or "", _bootstrap_fingerprint()
        ):
            return None
        return _bootstrap_principal(stored_session.id, verified=True)
    settings = get_settings()
    if (
        user is None
        or user.status != "active"
        or user.tenant_id not in settings.allowed_admin_tenants
        or not hmac.compare_digest(
            stored_session.credential_fingerprint or "",
            _credential_fingerprint(user.password_hash),
        )
    ):
        return None
    return _user_principal(stored_session.id, user)


async def principal_from_session_row(
    session, session_id: uuid.UUID | str, *, for_update: bool = False
) -> Principal | None:
    try:
        session_uuid = uuid.UUID(str(session_id))
    except (TypeError, ValueError):
        return None
    stmt = (
        select(models.AdminSession, models.AdminUser)
        .outerjoin(models.AdminUser, models.AdminSession.user_id == models.AdminUser.id)
        .where(
            models.AdminSession.id == session_uuid,
            models.AdminSession.expires_at > datetime.now(UTC),
        )
        .execution_options(populate_existing=True)
    )
    if for_update:
        from social_reply.application.account_management.access import lock_session_authorities

        await lock_session_authorities(session, (session_uuid,))
        stmt = stmt.with_for_update(of=models.AdminSession)
    row = (await session.execute(stmt)).one_or_none()
    if row is None:
        return None
    stored_session, user = row
    if stored_session.expires_at is None or stored_session.expires_at <= datetime.now(UTC):
        return None
    if stored_session.user_id is None:
        if not hmac.compare_digest(
            stored_session.bootstrap_fingerprint or "", _bootstrap_fingerprint()
        ):
            return None
        return _bootstrap_principal(stored_session.id, verified=True)
    settings = get_settings()
    if (
        user is None
        or user.status != "active"
        or user.tenant_id not in settings.allowed_admin_tenants
        or not hmac.compare_digest(
            stored_session.credential_fingerprint or "",
            _credential_fingerprint(user.password_hash),
        )
    ):
        return None
    return _user_principal(stored_session.id, user)


async def principal_from_session_id(session_id: uuid.UUID | str) -> Principal | None:
    async with get_session_factory()() as session:
        return await principal_from_session_row(session, session_id)


_AUTHENTICATED_PRINCIPAL: ContextVar[Principal | None] = ContextVar(
    "reply_authenticated_principal",
    default=None,
)


def authenticated_principal_context() -> Principal | None:
    """Return the principal authenticated by the current HTTP request task."""
    return _AUTHENTICATED_PRINCIPAL.get()


async def current_principal(request: Request) -> Principal | None:
    principal = await principal_from_token(request.cookies.get("reply_admin_session", ""))
    _AUTHENTICATED_PRINCIPAL.set(principal)
    return principal


async def require_principal(request: Request) -> Principal:
    principal = await current_principal(request)
    if principal is None:
        raise HTTPException(status_code=401, detail="admin_login_required")
    if principal.must_change_password:
        raise HTTPException(status_code=428, detail="password_change_required")
    return principal
