import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.domain.reply.business_prompt import (
    DEFAULT_BUSINESS_PROMPT,
    BusinessPromptInstructions,
)
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.advisory_locks import (
    acquire_shared_xact_lock,
    acquire_xact_lock,
)

logger = logging.getLogger(__name__)


class BusinessPromptConfigurationError(RuntimeError):
    pass


class BusinessPromptSuperseded(RuntimeError):
    pass


@dataclass(frozen=True)
class ResolvedBusinessPrompt:
    instructions: BusinessPromptInstructions
    version_id: uuid.UUID | None
    revision: int | None

    @property
    def content_hash(self) -> str:
        return self.instructions.content_hash

    @property
    def is_default(self) -> bool:
        return self.version_id is None


DEFAULT_RESOLVED_BUSINESS_PROMPT = ResolvedBusinessPrompt(
    instructions=DEFAULT_BUSINESS_PROMPT,
    version_id=None,
    revision=None,
)


def business_prompt_lock_key(tenant_id: str, brand_id: str) -> str:
    return f"social-reply:business-prompt:{tenant_id}:{brand_id}"


async def acquire_business_prompt_xact_lock(
    session: AsyncSession,
    tenant_id: str,
    brand_id: str,
) -> None:
    await acquire_xact_lock(session, business_prompt_lock_key(tenant_id, brand_id))


async def acquire_business_prompt_shared_xact_lock(
    session: AsyncSession,
    tenant_id: str,
    brand_id: str,
) -> None:
    await acquire_shared_xact_lock(
        session,
        business_prompt_lock_key(tenant_id, brand_id),
    )


async def load_business_prompt(
    session: AsyncSession,
    tenant_id: str,
    brand_id: str,
) -> ResolvedBusinessPrompt:
    deployed = await _load_deployed_business_prompt(session, tenant_id, brand_id)
    if deployed is not None:
        return deployed

    # Compatibility fallback for legacy scopes that predate the Agent control plane. Once a
    # scope has any deployment, runtime resolution is pinned to that append-only release stream.
    return await load_latest_business_prompt_draft(session, tenant_id, brand_id)


async def load_latest_business_prompt_draft(
    session: AsyncSession,
    tenant_id: str,
    brand_id: str,
) -> ResolvedBusinessPrompt:
    """Load the mutable editor pointer without changing production resolution."""
    row = (
        await session.execute(
            select(
                models.ReplyBusinessPrompt.active_version_id,
                models.ReplyBusinessPrompt.revision,
                models.ReplyBusinessPrompt.content_hash,
                models.ReplyBusinessPromptVersion.content,
                models.ReplyBusinessPromptVersion.content_hash.label("version_content_hash"),
                models.ReplyBusinessPromptVersion.revision.label("version_revision"),
            )
            .join(
                models.ReplyBusinessPromptVersion,
                models.ReplyBusinessPromptVersion.id
                == models.ReplyBusinessPrompt.active_version_id,
            )
            .where(
                models.ReplyBusinessPrompt.tenant_id == tenant_id,
                models.ReplyBusinessPrompt.brand_id == brand_id,
                models.ReplyBusinessPromptVersion.tenant_id == tenant_id,
                models.ReplyBusinessPromptVersion.brand_id == brand_id,
            )
        )
    ).one_or_none()
    if row is None:
        return DEFAULT_RESOLVED_BUSINESS_PROMPT

    try:
        instructions = BusinessPromptInstructions(row.content)
    except ValueError as exc:
        logger.error(
            "Stored business prompt is invalid tenant=%s brand=%s revision=%s",
            tenant_id,
            brand_id,
            row.revision,
        )
        raise BusinessPromptConfigurationError("stored_business_prompt_invalid") from exc
    if (
        row.revision != row.version_revision
        or row.content_hash != row.version_content_hash
        or instructions.content_hash != row.content_hash
    ):
        raise BusinessPromptConfigurationError("stored_business_prompt_provenance_invalid")
    return ResolvedBusinessPrompt(
        instructions=instructions,
        version_id=row.active_version_id,
        revision=row.revision,
    )


async def _load_deployed_business_prompt(
    session: AsyncSession,
    tenant_id: str,
    brand_id: str,
) -> ResolvedBusinessPrompt | None:
    row = (
        await session.execute(
            select(
                models.AgentVersion.business_prompt_version_id,
                models.AgentVersion.configuration,
                models.ReplyBusinessPromptVersion.content,
                models.ReplyBusinessPromptVersion.content_hash,
                models.ReplyBusinessPromptVersion.revision,
            )
            .select_from(models.AgentDeployment)
            .join(
                models.Agent,
                (models.Agent.tenant_id == models.AgentDeployment.tenant_id)
                & (models.Agent.id == models.AgentDeployment.agent_id),
            )
            .join(
                models.AgentVersion,
                (models.AgentVersion.tenant_id == models.AgentDeployment.tenant_id)
                & (models.AgentVersion.agent_id == models.AgentDeployment.agent_id)
                & (models.AgentVersion.id == models.AgentDeployment.agent_version_id),
            )
            .outerjoin(
                models.ReplyBusinessPromptVersion,
                (
                    models.ReplyBusinessPromptVersion.id
                    == models.AgentVersion.business_prompt_version_id
                )
                & (models.ReplyBusinessPromptVersion.tenant_id == tenant_id)
                & (models.ReplyBusinessPromptVersion.brand_id == brand_id),
            )
            .where(
                models.AgentDeployment.tenant_id == tenant_id,
                models.AgentDeployment.environment == "production",
                models.Agent.legacy_brand_id == brand_id,
            )
            .order_by(models.AgentDeployment.revision.desc())
            .limit(1)
        )
    ).one_or_none()
    if row is None:
        return None
    if row.business_prompt_version_id is None:
        return DEFAULT_RESOLVED_BUSINESS_PROMPT
    if row.content is None or row.content_hash is None or row.revision is None:
        raise BusinessPromptConfigurationError("deployed_business_prompt_missing")
    try:
        instructions = BusinessPromptInstructions(row.content)
    except ValueError as exc:
        raise BusinessPromptConfigurationError("deployed_business_prompt_invalid") from exc
    configured_revision = row.configuration.get("business_prompt_revision")
    configured_version_id = row.configuration.get("business_prompt_version_id")
    if (
        instructions.content_hash != row.content_hash
        or (configured_revision is not None and configured_revision != row.revision)
        or configured_version_id != str(row.business_prompt_version_id)
    ):
        raise BusinessPromptConfigurationError("deployed_business_prompt_provenance_invalid")
    return ResolvedBusinessPrompt(
        instructions=instructions,
        version_id=row.business_prompt_version_id,
        revision=row.revision,
    )


async def business_prompt_provenance_is_current(
    session: AsyncSession,
    *,
    tenant_id: str,
    brand_id: str,
    version_id: uuid.UUID | None,
    content_hash: str,
    acquire_lock: bool = True,
) -> bool:
    if acquire_lock:
        await acquire_business_prompt_shared_xact_lock(session, tenant_id, brand_id)
    current = await load_business_prompt(session, tenant_id, brand_id)
    return (
        version_id == current.version_id
        and content_hash == current.content_hash
    )


async def require_current_business_prompt(
    session: AsyncSession,
    *,
    tenant_id: str,
    brand_id: str,
    prompt: ResolvedBusinessPrompt,
) -> None:
    current = await business_prompt_provenance_is_current(
        session,
        tenant_id=tenant_id,
        brand_id=brand_id,
        version_id=prompt.version_id,
        content_hash=prompt.content_hash,
    )
    if not current:
        raise BusinessPromptSuperseded("reply_business_prompt_superseded")


def business_prompt_version_label(base: str, prompt: ResolvedBusinessPrompt) -> str:
    return base if prompt.is_default else f"{base}#bp{prompt.revision}"
