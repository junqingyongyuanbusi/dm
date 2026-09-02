import hashlib
import json
import re
import uuid

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.application.reply_decision.business_prompt import (
    acquire_business_prompt_xact_lock,
)
from social_reply.infrastructure.database import models

_AGENT_SLUG = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_AGENT_NAME_MAX_CHARS = 128
_AGENT_DESCRIPTION_MAX_CHARS = 1000


class AgentControlPlaneValidationError(ValueError):
    pass


class AgentControlPlaneConflict(RuntimeError):
    pass


def _normalize_tenant_id(value: str) -> str:
    tenant_id = value.strip()
    if not tenant_id or len(tenant_id) > 64:
        raise AgentControlPlaneValidationError("invalid_agent_tenant_id")
    return tenant_id


def normalize_agent_slug(value: str) -> str:
    slug = value.strip()
    if _AGENT_SLUG.fullmatch(slug) is None:
        raise AgentControlPlaneValidationError("invalid_agent_slug")
    return slug


def normalize_agent_name(value: str) -> str:
    name = " ".join(value.split())
    if not name or len(name) > _AGENT_NAME_MAX_CHARS:
        raise AgentControlPlaneValidationError("invalid_agent_name")
    return name


def normalize_agent_description(value: str | None) -> str | None:
    if value is None:
        return None
    description = value.strip()
    if len(description) > _AGENT_DESCRIPTION_MAX_CHARS:
        raise AgentControlPlaneValidationError("invalid_agent_description")
    return description or None


def _agent_name(brand_id: str) -> str:
    if brand_id == "default":
        return "Default Agent"
    normalized = brand_id.replace("_", " ").replace("-", " ").strip() or brand_id
    return f"{normalized.title()} Agent"[:128]


def _version_configuration(
    *,
    tenant_id: str,
    brand_id: str,
    business_prompt_version_id: uuid.UUID | None,
    business_prompt_revision: int | None,
    source: str,
) -> dict[str, object]:
    return {
        "business_prompt_revision": business_prompt_revision,
        "business_prompt_version_id": (
            str(business_prompt_version_id)
            if business_prompt_version_id is not None
            else None
        ),
        "runtime_scope": {"brand_id": brand_id, "tenant_id": tenant_id},
        "schema_version": 1,
        "source": source,
    }


def _configuration_hash(configuration: dict[str, object]) -> str:
    payload = json.dumps(
        configuration,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def _load_agent_for_update(
    session: AsyncSession,
    *,
    tenant_id: str,
    brand_id: str,
) -> models.Agent | None:
    return await session.scalar(
        select(models.Agent)
        .where(
            models.Agent.tenant_id == tenant_id,
            models.Agent.legacy_brand_id == brand_id,
        )
        .with_for_update()
    )


async def _append_agent_version(
    session: AsyncSession,
    *,
    agent: models.Agent,
    business_prompt_version_id: uuid.UUID | None,
    business_prompt_revision: int | None,
    actor: str,
    change_note: str | None,
    source: str,
) -> models.AgentVersion:
    latest_version = await session.scalar(
        select(models.AgentVersion)
        .where(
            models.AgentVersion.tenant_id == agent.tenant_id,
            models.AgentVersion.agent_id == agent.id,
        )
        .order_by(models.AgentVersion.revision.desc())
        .limit(1)
        .with_for_update()
    )
    if (
        latest_version is not None
        and latest_version.business_prompt_version_id == business_prompt_version_id
    ):
        return latest_version

    revision = (latest_version.revision if latest_version is not None else 0) + 1
    configuration = _version_configuration(
        tenant_id=agent.tenant_id,
        brand_id=agent.legacy_brand_id,
        business_prompt_version_id=business_prompt_version_id,
        business_prompt_revision=business_prompt_revision,
        source=source,
    )
    version = models.AgentVersion(
        tenant_id=agent.tenant_id,
        agent_id=agent.id,
        legacy_brand_id=agent.legacy_brand_id,
        revision=revision,
        business_prompt_version_id=business_prompt_version_id,
        configuration=configuration,
        content_hash=_configuration_hash(configuration),
        change_note=change_note,
        created_by=actor,
    )
    session.add(version)
    session.add(
        models.AuditLog(
            tenant_id=agent.tenant_id,
            category="admin_action",
            actor=actor,
            action="CREATE_AGENT_VERSION",
            subject_type="agent",
            subject_id=str(agent.id),
            detail={
                "agent_version_revision": revision,
                "business_prompt_revision": business_prompt_revision,
                "business_prompt_version_id": (
                    str(business_prompt_version_id)
                    if business_prompt_version_id is not None
                    else None
                ),
                "legacy_brand_id": agent.legacy_brand_id,
                "source": source,
            },
        )
    )
    await session.flush([version])
    return version


async def create_agent(
    session: AsyncSession,
    *,
    tenant_id: str,
    slug: str,
    name: str,
    description: str | None,
    actor: str,
) -> models.Agent:
    """Create a stable Agent identity and its immutable draft v1 snapshot."""
    tenant_id = _normalize_tenant_id(tenant_id)
    slug = normalize_agent_slug(slug)
    name = normalize_agent_name(name)
    description = normalize_agent_description(description)
    if not actor.strip():
        raise AgentControlPlaneValidationError("invalid_agent_actor")

    await acquire_business_prompt_xact_lock(session, tenant_id, slug)
    existing = await session.scalar(
        select(models.Agent)
        .where(
            models.Agent.tenant_id == tenant_id,
            or_(models.Agent.slug == slug, models.Agent.legacy_brand_id == slug),
        )
        .with_for_update()
    )
    if existing is not None:
        raise AgentControlPlaneConflict("agent_scope_already_exists")

    agent = models.Agent(
        tenant_id=tenant_id,
        slug=slug,
        legacy_brand_id=slug,
        name=name,
        description=description,
        status="active",
        created_by=actor,
    )
    session.add(agent)
    await session.flush([agent])
    session.add(
        models.AuditLog(
            tenant_id=tenant_id,
            category="admin_action",
            actor=actor,
            action="CREATE_AGENT",
            subject_type="agent",
            subject_id=str(agent.id),
            detail={
                "legacy_brand_id": slug,
                "name": name,
                "status": "active",
            },
        )
    )
    prompt = await session.scalar(
        select(models.ReplyBusinessPrompt).where(
            models.ReplyBusinessPrompt.tenant_id == tenant_id,
            models.ReplyBusinessPrompt.brand_id == slug,
        )
    )
    version = await _append_agent_version(
        session,
        agent=agent,
        business_prompt_version_id=(prompt.active_version_id if prompt is not None else None),
        business_prompt_revision=(prompt.revision if prompt is not None else None),
        actor=actor,
        change_note="Initial agent configuration",
        source="agent_creation",
    )
    await _append_legacy_runtime_deployment(
        session,
        tenant_id=tenant_id,
        brand_id=slug,
        agent=agent,
        agent_version=version,
        actor=actor,
        source="agent_creation",
    )
    await session.flush()
    return agent


async def append_agent_version_for_business_prompt(
    session: AsyncSession,
    *,
    tenant_id: str,
    brand_id: str,
    business_prompt_version_id: uuid.UUID,
    business_prompt_revision: int,
    actor: str,
    change_note: str | None,
) -> models.AgentVersion:
    """Append the control-plane snapshot in the prompt writer's locked transaction.

    The caller owns the existing tenant + brand business-prompt advisory lock. This keeps the
    compatibility scope, immutable prompt version, and Agent version on one serial write path.
    While the legacy runtime still treats a prompt save as immediately active, channel-backed
    scopes also receive an append-only compatibility deployment. A later cutover can separate
    draft creation from explicit production deployment without falsifying current runtime state.
    """
    agent = await _load_agent_for_update(
        session,
        tenant_id=tenant_id,
        brand_id=brand_id,
    )
    if agent is None:
        agent = models.Agent(
            tenant_id=tenant_id,
            slug=brand_id,
            legacy_brand_id=brand_id,
            name=_agent_name(brand_id),
            description=None,
            status="active",
            created_by=actor,
        )
        session.add(agent)
        await session.flush([agent])

    version = await _append_agent_version(
        session,
        agent=agent,
        business_prompt_version_id=business_prompt_version_id,
        business_prompt_revision=business_prompt_revision,
        actor=actor,
        change_note=change_note,
        source="business_prompt_change",
    )
    await _append_legacy_runtime_deployment(
        session,
        tenant_id=tenant_id,
        brand_id=brand_id,
        agent=agent,
        agent_version=version,
        actor=actor,
    )
    return version


async def ensure_agent_deployment_for_channel_scope(
    session: AsyncSession,
    *,
    tenant_id: str,
    brand_id: str,
    actor: str = "system:channel_provisioning",
) -> models.AgentDeployment:
    """Synchronize the compatibility Agent snapshot and production deployment.

    Call this after the channel account upsert and before committing that transaction. The
    advisory lock is shared with instruction saves, so a channel can never be committed without
    a deployment that points at the runtime-active prompt snapshot.
    """
    tenant_id = _normalize_tenant_id(tenant_id)
    brand_id = normalize_agent_slug(brand_id)
    await acquire_business_prompt_xact_lock(session, tenant_id, brand_id)

    channel_id = await session.scalar(
        select(models.PlatformAccount.id)
        .where(
            models.PlatformAccount.tenant_id == tenant_id,
            models.PlatformAccount.brand_id == brand_id,
        )
        .limit(1)
    )
    if channel_id is None:
        raise AgentControlPlaneValidationError("agent_channel_scope_not_found")

    prompt = await session.scalar(
        select(models.ReplyBusinessPrompt).where(
            models.ReplyBusinessPrompt.tenant_id == tenant_id,
            models.ReplyBusinessPrompt.brand_id == brand_id,
        )
    )
    agent = await _load_agent_for_update(
        session,
        tenant_id=tenant_id,
        brand_id=brand_id,
    )
    if agent is None:
        agent = models.Agent(
            tenant_id=tenant_id,
            slug=brand_id,
            legacy_brand_id=brand_id,
            name=_agent_name(brand_id),
            description=None,
            status="active",
            created_by=actor,
        )
        session.add(agent)
        await session.flush([agent])
        session.add(
            models.AuditLog(
                tenant_id=tenant_id,
                category="admin_action",
                actor=actor,
                action="CREATE_AGENT",
                subject_type="agent",
                subject_id=str(agent.id),
                detail={
                    "legacy_brand_id": brand_id,
                    "name": agent.name,
                    "source": "channel_provisioning",
                    "status": "active",
                },
            )
        )

    version = await _append_agent_version(
        session,
        agent=agent,
        business_prompt_version_id=(prompt.active_version_id if prompt is not None else None),
        business_prompt_revision=(prompt.revision if prompt is not None else None),
        actor=actor,
        change_note="Channel deployment synchronization",
        source="channel_provisioning",
    )
    deployment = await _append_legacy_runtime_deployment(
        session,
        tenant_id=tenant_id,
        brand_id=brand_id,
        agent=agent,
        agent_version=version,
        actor=actor,
        source="channel_provisioning",
    )
    if deployment is None:
        raise RuntimeError("agent_channel_deployment_missing")
    return deployment


async def _append_legacy_runtime_deployment(
    session: AsyncSession,
    *,
    tenant_id: str,
    brand_id: str,
    agent: models.Agent,
    agent_version: models.AgentVersion,
    actor: str,
    source: str = "legacy_prompt_activation",
) -> models.AgentDeployment | None:
    channel_id = await session.scalar(
        select(models.PlatformAccount.id)
        .where(
            models.PlatformAccount.tenant_id == tenant_id,
            models.PlatformAccount.brand_id == brand_id,
        )
        .limit(1)
    )
    if channel_id is None:
        return None
    latest_deployment = await session.scalar(
        select(models.AgentDeployment)
        .where(
            models.AgentDeployment.tenant_id == tenant_id,
            models.AgentDeployment.agent_id == agent.id,
            models.AgentDeployment.environment == "production",
        )
        .order_by(models.AgentDeployment.revision.desc())
        .limit(1)
        .with_for_update()
    )
    if (
        latest_deployment is not None
        and latest_deployment.agent_version_id == agent_version.id
    ):
        return latest_deployment
    revision = (latest_deployment.revision if latest_deployment is not None else 0) + 1
    deployment = models.AgentDeployment(
        tenant_id=tenant_id,
        agent_id=agent.id,
        agent_version_id=agent_version.id,
        environment="production",
        revision=revision,
        deployed_by=actor,
    )
    session.add(deployment)
    session.add(
        models.AuditLog(
            tenant_id=tenant_id,
            category="admin_action",
            actor=actor,
            action="DEPLOY_AGENT_VERSION",
            subject_type="agent",
            subject_id=str(agent.id),
            detail={
                "agent_version_revision": agent_version.revision,
                "deployment_revision": revision,
                "environment": "production",
                "legacy_brand_id": brand_id,
                "source": source,
            },
        )
    )
    await session.flush([deployment])
    return deployment
