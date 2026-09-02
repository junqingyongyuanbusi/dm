import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.application.account_management.reply_prompt_policy import (
    ReplyBusinessPromptVersionSummary,
    list_reply_prompt_versions,
    load_current_reply_business_prompt,
    rollback_reply_business_prompt,
    save_reply_business_prompt,
)
from social_reply.application.reply_decision.business_prompt import ResolvedBusinessPrompt
from social_reply.infrastructure.database import models


@dataclass(frozen=True)
class ReplyBusinessPromptEditorView:
    tenant_id: str
    brand_id: str
    current_content: str
    current_revision: int
    content_hash: str
    updated_by: str
    updated_at: datetime | None
    is_default: bool
    versions: tuple[ReplyBusinessPromptVersionSummary, ...]


@dataclass(frozen=True)
class SaveReplyBusinessPromptCommand:
    tenant_id: str
    brand_id: str
    content: str
    expected_revision: int
    actor: str
    change_note: str | None


@dataclass(frozen=True)
class RollbackReplyBusinessPromptCommand:
    tenant_id: str
    brand_id: str
    source_version_id: uuid.UUID
    expected_revision: int
    actor: str


async def load_reply_business_prompt_editor_view(
    session: AsyncSession,
    *,
    tenant_id: str,
    brand_id: str,
) -> ReplyBusinessPromptEditorView:
    resolved_prompt = await load_current_reply_business_prompt(
        session,
        tenant_id,
        brand_id,
    )
    current_pointer = await session.scalar(
        select(models.ReplyBusinessPrompt).where(
            models.ReplyBusinessPrompt.tenant_id == tenant_id,
            models.ReplyBusinessPrompt.brand_id == brand_id,
        )
    )
    versions = await list_reply_prompt_versions(session, tenant_id, brand_id)
    return ReplyBusinessPromptEditorView(
        tenant_id=tenant_id,
        brand_id=brand_id,
        current_content=resolved_prompt.instructions.text,
        current_revision=resolved_prompt.revision or 0,
        content_hash=resolved_prompt.content_hash,
        updated_by=current_pointer.updated_by if current_pointer is not None else "system",
        updated_at=current_pointer.updated_at if current_pointer is not None else None,
        is_default=resolved_prompt.is_default,
        versions=versions,
    )


async def execute_save_reply_business_prompt(
    session: AsyncSession,
    command: SaveReplyBusinessPromptCommand,
) -> ResolvedBusinessPrompt:
    return await save_reply_business_prompt(
        session,
        tenant_id=command.tenant_id,
        brand_id=command.brand_id,
        content=command.content,
        expected_revision=command.expected_revision,
        actor=command.actor,
        change_note=command.change_note,
    )


async def execute_rollback_reply_business_prompt(
    session: AsyncSession,
    command: RollbackReplyBusinessPromptCommand,
) -> ResolvedBusinessPrompt:
    return await rollback_reply_business_prompt(
        session,
        tenant_id=command.tenant_id,
        brand_id=command.brand_id,
        source_version_id=command.source_version_id,
        expected_revision=command.expected_revision,
        actor=command.actor,
    )
