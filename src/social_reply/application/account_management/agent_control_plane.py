import hashlib
import json
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.infrastructure.database import models


def _agent_name(brand_id: str) -> str:
    if brand_id == "default":
        return "Default Agent"
    normalized = brand_id.replace("_", " ").replace("-", " ").strip() or brand_id
    return f"{normalized.title()} Agent"[:128]


def _version_configuration(
    *,
    tenant_id: str,
    brand_id: str,
    business_prompt_version_id: uuid.UUID,
    business_prompt_revision: int,
) -> dict[str, object]:
    return {
        "business_prompt_revision": business_prompt_revision,
        "business_prompt_version_id": str(business_prompt_version_id),
        "runtime_scope": {"brand_id": brand_id, "tenant_id": tenant_id},
        "schema_version": 1,
        "source": "business_prompt_change",
    }


def _configuration_hash(configuration: dict[str, object]) -> str:
    payload = json.dumps(
        configuration,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
    agent = await session.scalar(
        select(models.Agent)
        .where(
            models.Agent.tenant_id == tenant_id,
            models.Agent.legacy_brand_id == brand_id,
        )
        .with_for_update()
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

    latest_version = await session.scalar(
        select(models.AgentVersion)
        .where(
            models.AgentVersion.tenant_id == tenant_id,
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
        tenant_id=tenant_id,
        brand_id=brand_id,
        business_prompt_version_id=business_prompt_version_id,
        business_prompt_revision=business_prompt_revision,
    )
    version = models.AgentVersion(
        tenant_id=tenant_id,
        agent_id=agent.id,
        legacy_brand_id=brand_id,
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
            tenant_id=tenant_id,
            category="admin_action",
            actor=actor,
            action="CREATE_AGENT_VERSION",
            subject_type="agent",
            subject_id=str(agent.id),
            detail={
                "agent_version_revision": revision,
                "business_prompt_revision": business_prompt_revision,
                "business_prompt_version_id": str(business_prompt_version_id),
                "legacy_brand_id": brand_id,
            },
        )
    )
    await session.flush([version])
    await _append_legacy_runtime_deployment(
        session,
        tenant_id=tenant_id,
        brand_id=brand_id,
        agent=agent,
        agent_version=version,
        actor=actor,
    )
    return version


async def _append_legacy_runtime_deployment(
    session: AsyncSession,
    *,
    tenant_id: str,
    brand_id: str,
    agent: models.Agent,
    agent_version: models.AgentVersion,
    actor: str,
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
                "source": "legacy_prompt_activation",
            },
        )
    )
    await session.flush([deployment])
    return deployment
