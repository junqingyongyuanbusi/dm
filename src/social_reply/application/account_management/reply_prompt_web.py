import uuid
from dataclasses import dataclass, replace
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.application.account_management.agent_control_plane import (
    deploy_agent_version,
)
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
    has_channel: bool = False
    latest_agent_version_id: uuid.UUID | None = None
    latest_agent_revision: int | None = None
    deployed_agent_version_id: uuid.UUID | None = None
    deployed_agent_revision: int | None = None
    deployment_revision: int = 0


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


@dataclass(frozen=True)
class DeployAgentVersionCommand:
    tenant_id: str
    brand_id: str
    agent_version_id: uuid.UUID
    expected_deployment_revision: int
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
    agent = await session.scalar(
        select(models.Agent).where(
            models.Agent.tenant_id == tenant_id,
            models.Agent.legacy_brand_id == brand_id,
            models.Agent.status == "active",
        )
    )
    agent_versions: tuple[models.AgentVersion, ...] = ()
    latest_agent_version = None
    latest_deployment = None
    if agent is not None:
        latest_agent_version = await session.scalar(
            select(models.AgentVersion)
            .where(
                models.AgentVersion.tenant_id == tenant_id,
                models.AgentVersion.agent_id == agent.id,
            )
            .order_by(models.AgentVersion.revision.desc())
            .limit(1)
        )
        latest_deployment = await session.scalar(
            select(models.AgentDeployment)
            .where(
                models.AgentDeployment.tenant_id == tenant_id,
                models.AgentDeployment.agent_id == agent.id,
                models.AgentDeployment.environment == "production",
            )
            .order_by(models.AgentDeployment.revision.desc())
            .limit(1)
        )
        prompt_version_ids = [version.id for version in versions]
        version_scope = [
            models.AgentVersion.business_prompt_version_id.in_(prompt_version_ids)
        ]
        if latest_deployment is not None:
            version_scope.append(
                models.AgentVersion.id == latest_deployment.agent_version_id
            )
        agent_versions = tuple(
            (
                await session.scalars(
                    select(models.AgentVersion).where(
                        models.AgentVersion.tenant_id == tenant_id,
                        models.AgentVersion.agent_id == agent.id,
                        or_(*version_scope),
                    )
                )
            ).all()
        )
    agent_version_by_prompt = {
        version.business_prompt_version_id: version
        for version in agent_versions
        if version.business_prompt_version_id is not None
    }
    versions = tuple(
        replace(
            version,
            agent_version_id=(
                agent_version_by_prompt[version.id].id
                if version.id in agent_version_by_prompt
                else None
            ),
            agent_revision=(
                agent_version_by_prompt[version.id].revision
                if version.id in agent_version_by_prompt
                else None
            ),
            is_deployed=(
                latest_deployment is not None
                and version.id in agent_version_by_prompt
                and latest_deployment.agent_version_id
                == agent_version_by_prompt[version.id].id
            ),
        )
        for version in versions
    )
    deployed_agent_version = next(
        (
            version
            for version in agent_versions
            if latest_deployment is not None
            and version.id == latest_deployment.agent_version_id
        ),
        None,
    )
    has_channel = (
        await session.scalar(
            select(models.PlatformAccount.id)
            .where(
                models.PlatformAccount.tenant_id == tenant_id,
                models.PlatformAccount.brand_id == brand_id,
            )
            .limit(1)
        )
        is not None
    )
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
        has_channel=has_channel,
        latest_agent_version_id=(
            latest_agent_version.id if latest_agent_version is not None else None
        ),
        latest_agent_revision=(
            latest_agent_version.revision if latest_agent_version is not None else None
        ),
        deployed_agent_version_id=(
            deployed_agent_version.id if deployed_agent_version is not None else None
        ),
        deployed_agent_revision=(
            deployed_agent_version.revision if deployed_agent_version is not None else None
        ),
        deployment_revision=(
            latest_deployment.revision if latest_deployment is not None else 0
        ),
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


async def execute_deploy_agent_version(
    session: AsyncSession,
    command: DeployAgentVersionCommand,
) -> models.AgentDeployment:
    return await deploy_agent_version(
        session,
        tenant_id=command.tenant_id,
        brand_id=command.brand_id,
        agent_version_id=command.agent_version_id,
        expected_deployment_revision=command.expected_deployment_revision,
        actor=command.actor,
    )
