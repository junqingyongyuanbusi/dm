import pytest
from sqlalchemy import func, select

from social_reply.application.account_management.agent_control_plane import (
    AgentControlPlaneConflict,
    AgentControlPlaneValidationError,
    create_agent,
    deploy_agent_version,
    ensure_agent_deployment_for_channel_scope,
)
from social_reply.application.account_management.reply_prompt_policy import (
    rollback_reply_business_prompt,
    save_reply_business_prompt,
)
from social_reply.application.reply_decision.business_prompt import (
    business_prompt_provenance_is_current,
    load_business_prompt,
)
from social_reply.infrastructure.database import models

pytestmark = pytest.mark.integration


async def test_first_class_agent_creation_starts_undeployed_and_accepts_instructions(
    session,
) -> None:
    agent = await create_agent(
        session,
        tenant_id="tenant-a",
        slug="indonesia_support",
        name="  Indonesia   Support  ",
        description="Handles Indonesian support questions.",
        actor="user:operator",
    )
    await session.commit()

    initial_version = await session.scalar(
        select(models.AgentVersion).where(
            models.AgentVersion.tenant_id == "tenant-a",
            models.AgentVersion.agent_id == agent.id,
        )
    )
    deployment_count = await session.scalar(
        select(func.count())
        .select_from(models.AgentDeployment)
        .where(models.AgentDeployment.agent_id == agent.id)
    )

    assert agent.name == "Indonesia Support"
    assert agent.legacy_brand_id == "indonesia_support"
    assert initial_version.revision == 1
    assert initial_version.business_prompt_version_id is None
    assert initial_version.configuration["source"] == "agent_creation"
    assert deployment_count == 0

    prompt = await save_reply_business_prompt(
        session,
        tenant_id="tenant-a",
        brand_id="indonesia_support",
        content="Reply in Bahasa Indonesia and escalate payment risk.",
        expected_revision=0,
        actor="user:operator",
        change_note="Initial business instructions",
    )
    await session.commit()
    versions = list(
        await session.scalars(
            select(models.AgentVersion)
            .where(models.AgentVersion.agent_id == agent.id)
            .order_by(models.AgentVersion.revision)
        )
    )

    assert [version.revision for version in versions] == [1, 2]
    assert versions[1].business_prompt_version_id == prompt.version_id
    with pytest.raises(
        AgentControlPlaneValidationError,
        match="agent_channel_scope_not_found",
    ):
        await deploy_agent_version(
            session,
            tenant_id="tenant-a",
            brand_id="indonesia_support",
            agent_version_id=versions[1].id,
            expected_deployment_revision=0,
            actor="user:operator",
        )
    await session.rollback()


async def test_agent_scope_is_unique_within_tenant_but_reusable_across_tenants(session) -> None:
    await create_agent(
        session,
        tenant_id="tenant-a",
        slug="support",
        name="Tenant A Support",
        description=None,
        actor="user:operator",
    )
    await session.commit()

    with pytest.raises(AgentControlPlaneConflict, match="agent_scope_already_exists"):
        await create_agent(
            session,
            tenant_id="tenant-a",
            slug="support",
            name="Duplicate Support",
            description=None,
            actor="user:operator",
        )
    await session.rollback()

    second_agent = await create_agent(
        session,
        tenant_id="tenant-b",
        slug="support",
        name="Tenant B Support",
        description=None,
        actor="user:operator",
    )
    await session.commit()

    assert second_agent.tenant_id == "tenant-b"


async def test_prompt_changes_append_agent_versions_without_implicit_deployment(session) -> None:
    first_prompt = await save_reply_business_prompt(
        session,
        tenant_id="tenant-a",
        brand_id="default",
        content="Keep answers concise.",
        expected_revision=0,
        actor="user:operator",
        change_note="Initial instructions",
    )
    await session.commit()
    second_prompt = await save_reply_business_prompt(
        session,
        tenant_id="tenant-a",
        brand_id="default",
        content="Keep answers concise and mention next steps.",
        expected_revision=1,
        actor="user:operator",
        change_note="Clarify next steps",
    )
    await session.commit()
    rollback_prompt = await rollback_reply_business_prompt(
        session,
        tenant_id="tenant-a",
        brand_id="default",
        source_version_id=first_prompt.version_id,
        expected_revision=2,
        actor="user:operator",
    )
    await session.commit()

    agent = await session.scalar(
        select(models.Agent).where(
            models.Agent.tenant_id == "tenant-a",
            models.Agent.legacy_brand_id == "default",
        )
    )
    versions = (
        (
            await session.execute(
                select(models.AgentVersion)
                .where(
                    models.AgentVersion.tenant_id == "tenant-a",
                    models.AgentVersion.agent_id == agent.id,
                )
                .order_by(models.AgentVersion.revision)
            )
        )
        .scalars()
        .all()
    )
    deployment_count = await session.scalar(
        select(func.count())
        .select_from(models.AgentDeployment)
        .where(
            models.AgentDeployment.tenant_id == "tenant-a",
            models.AgentDeployment.agent_id == agent.id,
        )
    )
    audit_count = await session.scalar(
        select(func.count())
        .select_from(models.AuditLog)
        .where(
            models.AuditLog.tenant_id == "tenant-a",
            models.AuditLog.action == "CREATE_AGENT_VERSION",
            models.AuditLog.subject_id == str(agent.id),
        )
    )

    assert agent.name == "Default Agent"
    assert [version.revision for version in versions] == [1, 2, 3]
    assert [version.business_prompt_version_id for version in versions] == [
        first_prompt.version_id,
        second_prompt.version_id,
        rollback_prompt.version_id,
    ]
    assert [version.configuration["business_prompt_revision"] for version in versions] == [1, 2, 3]
    assert all(len(version.content_hash) == 64 for version in versions)
    assert deployment_count == 0
    assert audit_count == 3


async def test_channel_backed_agent_uses_explicit_append_only_deployments(
    session,
) -> None:
    session.add(
        models.PlatformAccount(
            tenant_id="tenant-a",
            brand_id="support",
            platform="telegram",
            name="Support",
            external_account_id="support-1",
            public_id="support-public",
            config={},
            capability={},
            automation_default="BOT_DRAFT_ONLY",
            status="active",
        )
    )
    await session.commit()
    first_prompt = await save_reply_business_prompt(
        session,
        tenant_id="tenant-a",
        brand_id="support",
        content="Answer support questions.",
        expected_revision=0,
        actor="user:operator",
        change_note="Initial support instructions",
    )
    await session.commit()
    second_prompt = await save_reply_business_prompt(
        session,
        tenant_id="tenant-a",
        brand_id="support",
        content="Answer support questions and provide the next action.",
        expected_revision=1,
        actor="user:operator",
        change_note="Add next action",
    )
    await session.commit()

    agent = await session.scalar(
        select(models.Agent).where(
            models.Agent.tenant_id == "tenant-a",
            models.Agent.legacy_brand_id == "support",
        )
    )
    versions = (
        (
            await session.execute(
                select(models.AgentVersion)
                .where(models.AgentVersion.agent_id == agent.id)
                .order_by(models.AgentVersion.revision)
            )
        )
        .scalars()
        .all()
    )
    deployments = (
        (
            await session.execute(
                select(models.AgentDeployment)
                .where(models.AgentDeployment.agent_id == agent.id)
                .order_by(models.AgentDeployment.revision)
            )
        )
        .scalars()
        .all()
    )

    assert [version.business_prompt_version_id for version in versions] == [
        first_prompt.version_id,
        second_prompt.version_id,
    ]
    assert deployments == []

    first_deployment = await deploy_agent_version(
        session,
        tenant_id="tenant-a",
        brand_id="support",
        agent_version_id=versions[0].id,
        expected_deployment_revision=0,
        actor="user:operator",
    )
    await session.commit()
    first_runtime = await load_business_prompt(session, "tenant-a", "support")
    assert first_runtime.version_id == first_prompt.version_id
    assert await business_prompt_provenance_is_current(
        session,
        tenant_id="tenant-a",
        brand_id="support",
        version_id=first_runtime.version_id,
        content_hash=first_runtime.content_hash,
        acquire_lock=False,
    )

    second_deployment = await deploy_agent_version(
        session,
        tenant_id="tenant-a",
        brand_id="support",
        agent_version_id=versions[1].id,
        expected_deployment_revision=1,
        actor="user:operator",
    )
    await session.commit()
    second_runtime = await load_business_prompt(session, "tenant-a", "support")
    assert second_runtime.version_id == second_prompt.version_id
    assert not await business_prompt_provenance_is_current(
        session,
        tenant_id="tenant-a",
        brand_id="support",
        version_id=first_runtime.version_id,
        content_hash=first_runtime.content_hash,
        acquire_lock=False,
    )

    rollback_deployment = await deploy_agent_version(
        session,
        tenant_id="tenant-a",
        brand_id="support",
        agent_version_id=versions[0].id,
        expected_deployment_revision=2,
        actor="user:operator",
    )
    await session.commit()
    rollback_runtime = await load_business_prompt(session, "tenant-a", "support")
    assert rollback_runtime.version_id == first_prompt.version_id

    repeated = await deploy_agent_version(
        session,
        tenant_id="tenant-a",
        brand_id="support",
        agent_version_id=versions[0].id,
        expected_deployment_revision=2,
        actor="user:operator",
    )
    await session.commit()
    assert repeated.id == rollback_deployment.id

    deployments = list(
        await session.scalars(
            select(models.AgentDeployment)
            .where(models.AgentDeployment.agent_id == agent.id)
            .order_by(models.AgentDeployment.revision)
        )
    )
    assert [deployment.agent_version_id for deployment in deployments] == [
        versions[0].id,
        versions[1].id,
        versions[0].id,
    ]
    assert [deployment.revision for deployment in deployments] == [1, 2, 3]
    assert first_deployment.revision == 1
    assert second_deployment.revision == 2

    await save_reply_business_prompt(
        session,
        tenant_id="tenant-a",
        brand_id="support",
        content="Unpublished instructions must stay out of production.",
        expected_revision=2,
        actor="user:operator",
        change_note="Keep as draft",
    )
    await session.commit()
    preserved = await ensure_agent_deployment_for_channel_scope(
        session,
        tenant_id="tenant-a",
        brand_id="support",
        actor="system:channel_reauthorization",
    )
    await session.commit()
    preserved_runtime = await load_business_prompt(session, "tenant-a", "support")
    preserved_deployment_count = await session.scalar(
        select(func.count())
        .select_from(models.AgentDeployment)
        .where(models.AgentDeployment.agent_id == agent.id)
    )
    assert preserved.id == rollback_deployment.id
    assert preserved_runtime.version_id == first_prompt.version_id
    assert preserved_deployment_count == 3

    with pytest.raises(
        AgentControlPlaneConflict,
        match="agent_deployment_revision_conflict",
    ):
        await deploy_agent_version(
            session,
            tenant_id="tenant-a",
            brand_id="support",
            agent_version_id=versions[1].id,
            expected_deployment_revision=1,
            actor="user:stale-operator",
        )
    await session.rollback()

    other_agent = await create_agent(
        session,
        tenant_id="tenant-a",
        slug="other_support",
        name="Other Support",
        description=None,
        actor="user:operator",
    )
    await session.commit()
    other_version = await session.scalar(
        select(models.AgentVersion).where(
            models.AgentVersion.agent_id == other_agent.id,
        )
    )
    with pytest.raises(
        AgentControlPlaneValidationError,
        match="agent_version_not_found",
    ):
        await deploy_agent_version(
            session,
            tenant_id="tenant-a",
            brand_id="support",
            agent_version_id=other_version.id,
            expected_deployment_revision=3,
            actor="user:operator",
        )
    await session.rollback()
