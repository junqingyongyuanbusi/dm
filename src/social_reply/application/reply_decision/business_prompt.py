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
    current = (
        await session.execute(
            select(
                models.ReplyBusinessPrompt.active_version_id,
                models.ReplyBusinessPrompt.content_hash,
            )
            .where(
                models.ReplyBusinessPrompt.tenant_id == tenant_id,
                models.ReplyBusinessPrompt.brand_id == brand_id,
            )
        )
    ).one_or_none()
    if current is None:
        return version_id is None and content_hash == DEFAULT_BUSINESS_PROMPT.content_hash
    return (
        version_id == current.active_version_id
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
