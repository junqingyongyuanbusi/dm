import re
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import distinct, select
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.application.account_management.agent_control_plane import (
    AgentControlPlaneValidationError,
    append_agent_version_for_business_prompt,
    authorize_agent_control_plane_write,
    normalize_agent_tenant_id,
)
from social_reply.application.account_management.auth import Principal
from social_reply.application.reply_decision.business_prompt import (
    ResolvedBusinessPrompt,
    acquire_business_prompt_xact_lock,
    load_latest_business_prompt_draft,
)
from social_reply.domain.reply.business_prompt import (
    BusinessPromptInstructions,
    normalize_business_prompt_change_note,
)
from social_reply.infrastructure.database import models

_BRAND_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class ReplyBusinessPromptConflict(RuntimeError):
    pass


class ReplyBusinessPromptScopeError(ValueError):
    pass


async def _authorize_prompt_write(
    session: AsyncSession,
    *,
    tenant_id: str,
    principal: Principal | None,
) -> str:
    try:
        current_principal = await authorize_agent_control_plane_write(
            session,
            tenant_id=tenant_id,
            principal=principal,
        )
    except AgentControlPlaneValidationError as exc:
        raise ReplyBusinessPromptScopeError(
            "reply_business_prompt_authorization_denied"
        ) from exc
    return current_principal.actor


def _normalize_prompt_tenant_id(value: str) -> str:
    try:
        return normalize_agent_tenant_id(value)
    except AgentControlPlaneValidationError as exc:
        raise ReplyBusinessPromptScopeError("invalid_agent_tenant_id") from exc


@dataclass(frozen=True)
class ReplyBusinessPromptVersionSummary:
    id: uuid.UUID
    revision: int
    content: str
    content_hash: str
    change_note: str | None
    created_by: str
    created_at: datetime
    is_active: bool
    agent_version_id: uuid.UUID | None = None
    agent_revision: int | None = None
    is_deployed: bool = False


def normalize_brand_id(value: str) -> str:
    brand_id = value.strip()
    if _BRAND_ID.fullmatch(brand_id) is None:
        raise ReplyBusinessPromptScopeError("invalid_brand_id")
    return brand_id


async def list_reply_prompt_brands(session: AsyncSession, tenant_id: str) -> tuple[str, ...]:
    account_brands = (
        await session.scalars(
            select(distinct(models.PlatformAccount.brand_id)).where(
                models.PlatformAccount.tenant_id == tenant_id
            )
        )
    ).all()
    configured_brands = (
        await session.scalars(
            select(distinct(models.ReplyBusinessPrompt.brand_id)).where(
                models.ReplyBusinessPrompt.tenant_id == tenant_id
            )
        )
    ).all()
    return tuple(sorted({"default", *account_brands, *configured_brands}))


async def require_reply_prompt_brand(
    session: AsyncSession,
    tenant_id: str,
    brand_id: str,
) -> str:
    normalized = normalize_brand_id(brand_id)
    if normalized == "default":
        return normalized
    exists = await session.scalar(
        select(models.PlatformAccount.id)
        .where(
            models.PlatformAccount.tenant_id == tenant_id,
            models.PlatformAccount.brand_id == normalized,
        )
        .limit(1)
    )
    configured = await session.scalar(
        select(models.ReplyBusinessPrompt.id)
        .where(
            models.ReplyBusinessPrompt.tenant_id == tenant_id,
            models.ReplyBusinessPrompt.brand_id == normalized,
        )
        .limit(1)
    )
    agent = await session.scalar(
        select(models.Agent.id)
        .where(
            models.Agent.tenant_id == tenant_id,
            models.Agent.legacy_brand_id == normalized,
            models.Agent.status == "active",
        )
        .limit(1)
    )
    if exists is None and configured is None and agent is None:
        raise ReplyBusinessPromptScopeError("reply_business_prompt_brand_not_found")
    return normalized


async def list_reply_prompt_versions(
    session: AsyncSession,
    tenant_id: str,
    brand_id: str,
    *,
    limit: int = 30,
) -> tuple[ReplyBusinessPromptVersionSummary, ...]:
    current_version_id = await session.scalar(
        select(models.ReplyBusinessPrompt.active_version_id).where(
            models.ReplyBusinessPrompt.tenant_id == tenant_id,
            models.ReplyBusinessPrompt.brand_id == brand_id,
        )
    )
    rows = (
        (
            await session.execute(
                select(models.ReplyBusinessPromptVersion)
                .where(
                    models.ReplyBusinessPromptVersion.tenant_id == tenant_id,
                    models.ReplyBusinessPromptVersion.brand_id == brand_id,
                )
                .order_by(models.ReplyBusinessPromptVersion.revision.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return tuple(
        ReplyBusinessPromptVersionSummary(
            id=row.id,
            revision=row.revision,
            content=row.content,
            content_hash=row.content_hash,
            change_note=row.change_note,
            created_by=row.created_by,
            created_at=row.created_at,
            is_active=row.id == current_version_id,
        )
        for row in rows
    )


async def _activate_new_version(
    session: AsyncSession,
    *,
    tenant_id: str,
    brand_id: str,
    instructions: BusinessPromptInstructions,
    expected_revision: int,
    actor: str,
    change_note: str | None,
    audit_action: str,
    rollback_source_revision: int | None = None,
) -> ResolvedBusinessPrompt:
    current = await session.scalar(
        select(models.ReplyBusinessPrompt)
        .where(
            models.ReplyBusinessPrompt.tenant_id == tenant_id,
            models.ReplyBusinessPrompt.brand_id == brand_id,
        )
        .with_for_update()
    )
    actual_revision = current.revision if current is not None else 0
    if expected_revision != actual_revision:
        raise ReplyBusinessPromptConflict("reply_business_prompt_revision_conflict")

    next_revision = actual_revision + 1
    version_id = uuid.uuid4()
    version = models.ReplyBusinessPromptVersion(
        id=version_id,
        tenant_id=tenant_id,
        brand_id=brand_id,
        revision=next_revision,
        content=instructions.text,
        content_hash=instructions.content_hash,
        change_note=change_note,
        created_by=actor,
    )
    session.add(version)
    # SQLAlchemy does not infer the dependency through this composite foreign key.
    # Materialize the immutable version before advancing the active pointer.
    await session.flush([version])
    if current is None:
        session.add(
            models.ReplyBusinessPrompt(
                tenant_id=tenant_id,
                brand_id=brand_id,
                active_version_id=version_id,
                revision=next_revision,
                content_hash=instructions.content_hash,
                updated_by=actor,
            )
        )
    else:
        current.active_version_id = version_id
        current.revision = next_revision
        current.content_hash = instructions.content_hash
        current.updated_by = actor

    await append_agent_version_for_business_prompt(
        session,
        tenant_id=tenant_id,
        brand_id=brand_id,
        business_prompt_version_id=version_id,
        business_prompt_revision=next_revision,
        actor=actor,
        change_note=change_note,
    )

    detail: dict[str, object] = {
        "revision": next_revision,
        "content_hash": instructions.content_hash,
        "change_note": change_note,
    }
    if rollback_source_revision is not None:
        detail["rollback_source_revision"] = rollback_source_revision
    session.add(
        models.AuditLog(
            tenant_id=tenant_id,
            category="admin_action",
            actor=actor,
            action=audit_action,
            subject_type="reply_business_prompt",
            subject_id=f"{tenant_id}:{brand_id}",
            detail=detail,
        )
    )
    await session.flush()
    return ResolvedBusinessPrompt(
        instructions=instructions,
        version_id=version_id,
        revision=next_revision,
    )


async def save_reply_business_prompt(
    session: AsyncSession,
    *,
    tenant_id: str,
    brand_id: str,
    content: str,
    expected_revision: int,
    actor: str,
    change_note: str | None,
    principal: Principal | None = None,
) -> ResolvedBusinessPrompt:
    if expected_revision < 0:
        raise ReplyBusinessPromptConflict("reply_business_prompt_revision_conflict")
    tenant_id = _normalize_prompt_tenant_id(tenant_id)
    actor = await _authorize_prompt_write(
        session,
        tenant_id=tenant_id,
        principal=principal,
    )
    normalized_brand_id = await require_reply_prompt_brand(session, tenant_id, brand_id)
    instructions = BusinessPromptInstructions(content)
    normalized_change_note = normalize_business_prompt_change_note(change_note)
    await acquire_business_prompt_xact_lock(session, tenant_id, normalized_brand_id)
    return await _activate_new_version(
        session,
        tenant_id=tenant_id,
        brand_id=normalized_brand_id,
        instructions=instructions,
        expected_revision=expected_revision,
        actor=actor,
        change_note=normalized_change_note,
        audit_action="SET_REPLY_BUSINESS_PROMPT",
    )


async def rollback_reply_business_prompt(
    session: AsyncSession,
    *,
    tenant_id: str,
    brand_id: str,
    source_version_id: uuid.UUID,
    expected_revision: int,
    actor: str,
    principal: Principal | None = None,
) -> ResolvedBusinessPrompt:
    tenant_id = _normalize_prompt_tenant_id(tenant_id)
    actor = await _authorize_prompt_write(
        session,
        tenant_id=tenant_id,
        principal=principal,
    )
    normalized_brand_id = await require_reply_prompt_brand(session, tenant_id, brand_id)
    await acquire_business_prompt_xact_lock(session, tenant_id, normalized_brand_id)
    source = await session.scalar(
        select(models.ReplyBusinessPromptVersion).where(
            models.ReplyBusinessPromptVersion.id == source_version_id,
            models.ReplyBusinessPromptVersion.tenant_id == tenant_id,
            models.ReplyBusinessPromptVersion.brand_id == normalized_brand_id,
        )
    )
    if source is None:
        raise ReplyBusinessPromptScopeError("reply_business_prompt_version_not_found")
    instructions = BusinessPromptInstructions(source.content)
    return await _activate_new_version(
        session,
        tenant_id=tenant_id,
        brand_id=normalized_brand_id,
        instructions=instructions,
        expected_revision=expected_revision,
        actor=actor,
        change_note=f"Rollback to revision {source.revision}",
        audit_action="ROLLBACK_REPLY_BUSINESS_PROMPT",
        rollback_source_revision=source.revision,
    )


async def load_current_reply_business_prompt(
    session: AsyncSession,
    tenant_id: str,
    brand_id: str,
) -> ResolvedBusinessPrompt:
    normalized_brand_id = await require_reply_prompt_brand(session, tenant_id, brand_id)
    return await load_latest_business_prompt_draft(session, tenant_id, normalized_brand_id)
