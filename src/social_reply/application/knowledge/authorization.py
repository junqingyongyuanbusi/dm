"""Final authorization for tenant knowledge mutations."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.application.account_management.access import lock_user_authority
from social_reply.application.account_management.auth import (
    Principal,
    principal_from_session_row,
)


class KnowledgeApplicationError(ValueError):
    """Base error exposed by knowledge application commands."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class KnowledgeValidationError(KnowledgeApplicationError):
    pass


class KnowledgeAuthorizationError(KnowledgeApplicationError):
    pass


@dataclass(frozen=True)
class AuthorizedKnowledgeWrite:
    """Identity reloaded from the transaction that owns a knowledge mutation."""

    actor: str
    principal: Principal | None
    system: bool = False


class _SystemKnowledgeImportCapability:
    """Opaque capability held only by the trusted local import facade."""

    __slots__ = ()


_SYSTEM_KNOWLEDGE_IMPORT_CAPABILITY = _SystemKnowledgeImportCapability()
SYSTEM_KNOWLEDGE_IMPORT_ACTOR = "system:knowledge-import"


def _trusted_system_import_capability() -> _SystemKnowledgeImportCapability:
    """Return the process-local capability used by the CLI import facade."""

    return _SYSTEM_KNOWLEDGE_IMPORT_CAPABILITY


def validate_knowledge_principal_input(principal: Principal | None) -> Principal:
    if not isinstance(principal, Principal):
        raise KnowledgeAuthorizationError("knowledge_principal_required")
    if (
        principal.session_id is None
        or principal.authentication_kind != "SESSION"
        or principal.action_proof is not None
    ):
        raise KnowledgeAuthorizationError("knowledge_principal_invalid")
    if principal.user_id is None and (
        not principal.is_superadmin or principal.tenant_id is not None
    ):
        raise KnowledgeAuthorizationError("knowledge_principal_invalid")
    return principal


async def authorize_knowledge_write(
    session: AsyncSession,
    *,
    principal: Principal | None,
    tenant_id: str,
) -> AuthorizedKnowledgeWrite:
    """Reload and authorize a named admin or verified bootstrap session.

    The staff authority advisory lock is acquired before the session row is reloaded.  User
    lifecycle mutations use the same lock, so a command that started with an old request
    principal cannot pass after that user's session, status, role, or password authority was
    changed.  The returned actor is transaction-verified and must be used for audit writes.
    """

    candidate = validate_knowledge_principal_input(principal)
    try:
        session_id = uuid.UUID(str(candidate.session_id))
    except (TypeError, ValueError) as exc:
        raise KnowledgeAuthorizationError("knowledge_session_invalid") from exc

    if candidate.user_id is None:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": "social-reply:bootstrap-authority"},
        )
    else:
        await lock_user_authority(session, candidate.user_id)

    current = await principal_from_session_row(session, session_id, for_update=True)
    if (
        current is None
        or current.session_id != session_id
        or current.user_id != candidate.user_id
        or current.must_change_password
        or not current.is_workspace_admin
        or tenant_id not in current.allowed_tenants
        or (
            current.user_id is not None
            and current.tenant_id != tenant_id
        )
        or (
            current.user_id is None
            and (not current.is_superadmin or current.tenant_id is not None)
        )
    ):
        raise KnowledgeAuthorizationError("knowledge_authorization_denied")

    return AuthorizedKnowledgeWrite(actor=current.actor, principal=current)


def validate_knowledge_import_input(
    *,
    principal: Principal | None,
    system_import_capability: object | None,
) -> None:
    if system_import_capability is not None:
        if (
            principal is not None
            or system_import_capability is not _SYSTEM_KNOWLEDGE_IMPORT_CAPABILITY
        ):
            raise KnowledgeAuthorizationError("knowledge_system_import_capability_invalid")
        return
    validate_knowledge_principal_input(principal)


async def authorize_knowledge_import(
    session: AsyncSession,
    *,
    principal: Principal | None,
    tenant_id: str,
    system_import_capability: object | None,
) -> AuthorizedKnowledgeWrite:
    """Authorize a browser principal or the explicit local system import facade."""

    if system_import_capability is not None:
        if (
            principal is not None
            or system_import_capability is not _SYSTEM_KNOWLEDGE_IMPORT_CAPABILITY
        ):
            raise KnowledgeAuthorizationError("knowledge_system_import_capability_invalid")
        return AuthorizedKnowledgeWrite(
            actor=SYSTEM_KNOWLEDGE_IMPORT_ACTOR,
            principal=None,
            system=True,
        )
    return await authorize_knowledge_write(
        session,
        principal=principal,
        tenant_id=tenant_id,
    )
