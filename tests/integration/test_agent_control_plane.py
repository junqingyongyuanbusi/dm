import uuid

import pytest
from sqlalchemy import delete, func, select

from social_reply.application.account_management.agent_control_plane import (
    AgentControlPlaneAuthorizationError,
    AgentControlPlaneConflict,
    AgentControlPlaneValidationError,
    create_agent,
    deploy_agent_version,
    ensure_agent_deployment_for_channel_scope,
)
from social_reply.application.account_management.auth import (
    authenticate,
    hash_password,
    issue_session,
    principal_from_session_id,
)
from social_reply.application.account_management.reply_prompt_policy import (
    ReplyBusinessPromptScopeError,
    rollback_reply_business_prompt,
    save_reply_business_prompt,
)
from social_reply.application.account_management.reply_prompt_web import (
    DeployAgentVersionCommand,
    RollbackReplyBusinessPromptCommand,
    SaveReplyBusinessPromptCommand,
    execute_deploy_agent_version,
    execute_rollback_reply_business_prompt,
    execute_save_reply_business_prompt,
)
from social_reply.application.reply_decision.business_prompt import (
    business_prompt_provenance_is_current,
    load_business_prompt,
)
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory

pytestmark = pytest.mark.integration


_PASSWORD = "agent-control-plane-test-password"


async def _bootstrap_principal():
    _token, session_id = await issue_session()
    principal = await principal_from_session_id(session_id)
    assert principal is not None
    return principal


async def _workspace_admin_principal(session, *, tenant_id: str = "tenant-a"):
    username = f"agent-admin-{uuid.uuid4().hex}"
    user = models.AdminUser(
        id=uuid.uuid4(),
        username=username,
        password_hash=await hash_password(_PASSWORD),
        tenant_id=tenant_id,
        role="WORKSPACE_ADMIN",
        must_change_password=False,
        status="active",
    )
    session.add(user)
    await session.commit()
    result = await authenticate(username, _PASSWORD)
    assert result is not None
    principal, _token = result
    return principal





async def test_first_class_agent_creation_starts_undeployed_and_accepts_instructions(
    session,
) -> None:
    principal = await _bootstrap_principal()
    agent = await create_agent(
        session,
        tenant_id="tenant-a",
        slug="indonesia_support",
        name="  Indonesia   Support  ",
        description="Handles Indonesian support questions.",
        actor="user:operator",
        principal=principal,
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
        principal=principal,
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
            principal=principal,
        )
    await session.rollback()


async def test_agent_scope_is_unique_within_tenant_but_reusable_across_tenants(session) -> None:
    principal = await _bootstrap_principal()
    await create_agent(
        session,
        tenant_id="tenant-a",
        slug="support",
        name="Tenant A Support",
        description=None,
        actor="user:operator",
        principal=principal,
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
            principal=principal,
        )
    await session.rollback()

    second_agent = await create_agent(
        session,
        tenant_id="tenant-b",
        slug="support",
        name="Tenant B Support",
        description=None,
        actor="user:operator",
        principal=principal,
    )
    await session.commit()

    assert second_agent.tenant_id == "tenant-b"


async def test_prompt_changes_append_agent_versions_without_implicit_deployment(session) -> None:
    principal = await _bootstrap_principal()
    first_prompt = await save_reply_business_prompt(
        session,
        tenant_id="tenant-a",
        brand_id="default",
        content="Keep answers concise.",
        expected_revision=0,
        actor="user:operator",
        principal=principal,
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
        principal=principal,
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
        principal=principal,
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
    principal = await _bootstrap_principal()
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
        principal=principal,
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
        principal=principal,
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
        principal=principal,
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
        principal=principal,
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
        principal=principal,
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
        principal=principal,
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
        principal=principal,
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
            principal=principal,
        )
    await session.rollback()

    other_agent = await create_agent(
        session,
        tenant_id="tenant-a",
        slug="other_support",
        name="Other Support",
        description=None,
        actor="user:operator",
        principal=principal,
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
            principal=principal,
        )
    await session.rollback()


async def _seed_delayed_control_plane_scope(session, principal):
    session.add(
        models.PlatformAccount(
            tenant_id="tenant-a",
            brand_id="delayed-support",
            platform="telegram",
            name="Delayed support",
            external_account_id=f"delayed-{uuid.uuid4()}",
            public_id=f"delayed-public-{uuid.uuid4()}",
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
        brand_id="delayed-support",
        content="Initial delayed instructions.",
        expected_revision=0,
        actor=principal.actor,
        change_note="Initial delayed instructions",
        principal=principal,
    )
    await session.commit()
    second_prompt = await save_reply_business_prompt(
        session,
        tenant_id="tenant-a",
        brand_id="delayed-support",
        content="Updated delayed instructions.",
        expected_revision=1,
        actor=principal.actor,
        change_note="Updated delayed instructions",
        principal=principal,
    )
    await session.commit()
    agent = await session.scalar(
        select(models.Agent).where(
            models.Agent.tenant_id == "tenant-a",
            models.Agent.legacy_brand_id == "delayed-support",
        )
    )
    assert agent is not None
    versions = list(
        await session.scalars(
            select(models.AgentVersion)
            .where(models.AgentVersion.agent_id == agent.id)
            .order_by(models.AgentVersion.revision)
        )
    )
    deployment = await deploy_agent_version(
        session,
        tenant_id="tenant-a",
        brand_id="delayed-support",
        agent_version_id=versions[0].id,
        expected_deployment_revision=0,
        actor=principal.actor,
        principal=principal,
    )
    await session.commit()
    return first_prompt, second_prompt, agent, versions, deployment


async def _control_plane_snapshot(
    session, *, agent_id, tenant_id="tenant-a", brand_id="delayed-support"
):
    prompt = await session.scalar(
        select(models.ReplyBusinessPrompt).where(
            models.ReplyBusinessPrompt.tenant_id == tenant_id,
            models.ReplyBusinessPrompt.brand_id == brand_id,
        )
    )
    deployments = tuple(
        (
            deployment.id,
            deployment.agent_version_id,
            deployment.revision,
            deployment.deployed_by,
        )
        for deployment in await session.scalars(
            select(models.AgentDeployment)
            .where(
                models.AgentDeployment.tenant_id == tenant_id,
                models.AgentDeployment.agent_id == agent_id,
                models.AgentDeployment.environment == "production",
            )
            .order_by(models.AgentDeployment.revision)
        )
    )
    audits = tuple(
        (
            audit.id,
            audit.action,
            audit.actor,
            audit.subject_type,
            audit.subject_id,
            audit.detail,
        )
        for audit in await session.scalars(
            select(models.AuditLog)
            .where(models.AuditLog.tenant_id == tenant_id)
            .order_by(models.AuditLog.created_at, models.AuditLog.id)
        )
    )
    assert prompt is not None
    return (
        (prompt.active_version_id, prompt.revision, prompt.updated_by),
        deployments,
        audits,
    )


async def _invalidate_staff_session(principal, invalidation: str) -> None:
    assert principal.user_id is not None
    async with get_session_factory()() as session:
        if invalidation == "revoke":
            await session.execute(
                delete(models.AdminSession).where(models.AdminSession.id == principal.session_id)
            )
        else:
            user = await session.get(models.AdminUser, principal.user_id)
            assert user is not None
            if invalidation == "demote":
                user.role = "USER"
            elif invalidation == "must_change":
                user.must_change_password = True
            elif invalidation == "disabled":
                user.status = "disabled"
            else:
                raise AssertionError(f"unknown invalidation: {invalidation}")
        await session.commit()


@pytest.mark.parametrize("operation", ["save", "deploy", "rollback"])
@pytest.mark.parametrize("invalidation", ["revoke", "demote", "must_change", "disabled"])
async def test_delayed_staff_revocation_rejects_all_agent_writes_without_mutation(
    session, operation, invalidation
) -> None:
    principal = await _workspace_admin_principal(session)
    first_prompt, second_prompt, agent, versions, deployment = (
        await _seed_delayed_control_plane_scope(session, principal)
    )
    async with get_session_factory()() as snapshot_session:
        before = await _control_plane_snapshot(snapshot_session, agent_id=agent.id)

    await _invalidate_staff_session(principal, invalidation)

    expected_error = (
        ReplyBusinessPromptScopeError
        if operation in {"save", "rollback"}
        else AgentControlPlaneAuthorizationError
    )
    async with get_session_factory()() as write_session:
        with pytest.raises(expected_error):
            if operation == "save":
                await save_reply_business_prompt(
                    write_session,
                    tenant_id="tenant-a",
                    brand_id="delayed-support",
                    content="Must not be saved after revocation.",
                    expected_revision=second_prompt.revision,
                    actor=principal.actor,
                    change_note="stale save",
                    principal=principal,
                )
            elif operation == "deploy":
                await deploy_agent_version(
                    write_session,
                    tenant_id="tenant-a",
                    brand_id="delayed-support",
                    agent_version_id=versions[1].id,
                    expected_deployment_revision=deployment.revision,
                    actor=principal.actor,
                    principal=principal,
                )
            else:
                await rollback_reply_business_prompt(
                    write_session,
                    tenant_id="tenant-a",
                    brand_id="delayed-support",
                    source_version_id=first_prompt.version_id,
                    expected_revision=second_prompt.revision,
                    actor=principal.actor,
                    principal=principal,
                )
        await write_session.rollback()

    async with get_session_factory()() as verify_session:
        after = await _control_plane_snapshot(verify_session, agent_id=agent.id)
    assert after == before


async def test_agent_writes_accept_real_workspace_admin_and_bootstrap_principals(session) -> None:
    workspace_admin = await _workspace_admin_principal(session)
    workspace_agent = await create_agent(
        session,
        tenant_id="tenant-a",
        slug="workspace-admin-agent",
        name="Workspace Admin Agent",
        description=None,
        actor="user:forged-actor",
        principal=workspace_admin,
    )
    await session.commit()

    bootstrap = await _bootstrap_principal()
    bootstrap_agent = await create_agent(
        session,
        tenant_id="tenant-a",
        slug="bootstrap-agent",
        name="Bootstrap Agent",
        description=None,
        actor="user:forged-bootstrap-actor",
        principal=bootstrap,
    )
    await session.commit()

    assert workspace_agent.created_by == workspace_admin.actor
    assert bootstrap_agent.created_by == bootstrap.actor


async def test_system_provisioning_ensure_keeps_its_internal_no_principal_path(session) -> None:
    session.add(
        models.PlatformAccount(
            tenant_id="tenant-a",
            brand_id="system-support",
            platform="telegram",
            name="System support",
            external_account_id=f"system-{uuid.uuid4()}",
            public_id=f"system-public-{uuid.uuid4()}",
            config={},
            capability={},
            automation_default="BOT_DRAFT_ONLY",
            status="active",
        )
    )
    await session.commit()

    deployment = await ensure_agent_deployment_for_channel_scope(
        session,
        tenant_id="tenant-a",
        brand_id="system-support",
        actor="system:channel_provisioning",
    )
    await session.commit()

    assert deployment.deployed_by == "system:channel_provisioning"
    assert deployment.environment == "production"


async def test_interactive_agent_creation_without_principal_fails_closed(session) -> None:
    with pytest.raises(AgentControlPlaneAuthorizationError):
        await create_agent(
            session,
            tenant_id="tenant-a",
            slug="legacy-agent",
            name="Legacy Agent",
            description=None,
            actor="system:legacy-bypass",
        )


async def test_prompt_command_without_principal_fails_closed(session) -> None:
    with pytest.raises(ReplyBusinessPromptScopeError):
        await execute_save_reply_business_prompt(
            session,
            SaveReplyBusinessPromptCommand(
                tenant_id="tenant-a",
                brand_id="default",
                content="Legacy actor-only command must fail.",
                expected_revision=0,
                actor="user:legacy-actor",
                change_note=None,
            ),
        )


async def test_rollback_command_without_principal_fails_closed(session) -> None:
    with pytest.raises(ReplyBusinessPromptScopeError):
        await execute_rollback_reply_business_prompt(
            session,
            RollbackReplyBusinessPromptCommand(
                tenant_id="tenant-a",
                brand_id="default",
                source_version_id=uuid.uuid4(),
                expected_revision=0,
                actor="user:legacy-actor",
            ),
        )


async def test_deploy_command_without_principal_fails_closed(session) -> None:
    with pytest.raises(AgentControlPlaneAuthorizationError):
        await execute_deploy_agent_version(
            session,
            DeployAgentVersionCommand(
                tenant_id="tenant-a",
                brand_id="default",
                agent_version_id=uuid.uuid4(),
                expected_deployment_revision=0,
                actor="system:legacy-bypass",
            ),
        )


async def test_prompt_command_uses_reloaded_principal_and_ignores_actor(session) -> None:
    principal = await _workspace_admin_principal(session)
    session.add(
        models.PlatformAccount(
            tenant_id="tenant-a",
            brand_id="command-contract",
            platform="telegram",
            name="Command contract",
            external_account_id=f"command-{uuid.uuid4()}",
            public_id=f"command-public-{uuid.uuid4()}",
            config={},
            capability={},
            automation_default="BOT_DRAFT_ONLY",
            status="active",
        )
    )
    await session.commit()

    await execute_save_reply_business_prompt(
        session,
        SaveReplyBusinessPromptCommand(
            tenant_id="tenant-a",
            brand_id="command-contract",
            content="The explicit Principal controls this write.",
            expected_revision=0,
            actor="user:forged-actor",
            change_note="Principal contract",
            principal=principal,
        ),
    )
    await session.commit()

    pointer = await session.scalar(
        select(models.ReplyBusinessPrompt).where(
            models.ReplyBusinessPrompt.tenant_id == "tenant-a",
            models.ReplyBusinessPrompt.brand_id == "command-contract",
        )
    )
    version = await session.scalar(
        select(models.ReplyBusinessPromptVersion).where(
            models.ReplyBusinessPromptVersion.tenant_id == "tenant-a",
            models.ReplyBusinessPromptVersion.brand_id == "command-contract",
        )
    )
    assert pointer is not None
    assert version is not None
    assert pointer.updated_by == principal.actor
    assert version.created_by == principal.actor


async def test_deploy_and_rollback_commands_use_principal_actor(session) -> None:
    principal = await _workspace_admin_principal(session)
    first_prompt, second_prompt, agent, versions, deployment = (
        await _seed_delayed_control_plane_scope(session, principal)
    )

    deployed = await execute_deploy_agent_version(
        session,
        DeployAgentVersionCommand(
            tenant_id="tenant-a",
            brand_id="delayed-support",
            agent_version_id=versions[1].id,
            expected_deployment_revision=deployment.revision,
            actor="user:forged-deployer",
            principal=principal,
        ),
    )
    await session.commit()
    assert deployed.deployed_by == principal.actor

    await execute_rollback_reply_business_prompt(
        session,
        RollbackReplyBusinessPromptCommand(
            tenant_id="tenant-a",
            brand_id="delayed-support",
            source_version_id=first_prompt.version_id,
            expected_revision=second_prompt.revision,
            actor="user:forged-rollbacker",
            principal=principal,
        ),
    )
    await session.commit()
    pointer = await session.scalar(
        select(models.ReplyBusinessPrompt).where(
            models.ReplyBusinessPrompt.tenant_id == "tenant-a",
            models.ReplyBusinessPrompt.brand_id == "delayed-support",
        )
    )
    assert pointer is not None
    assert pointer.updated_by == principal.actor
    audit_actors = list(
        await session.scalars(
            select(models.AuditLog.actor).where(
                models.AuditLog.tenant_id == "tenant-a",
                models.AuditLog.subject_id.in_({
                    f"{pointer.tenant_id}:{pointer.brand_id}",
                    str(agent.id),
                }),
            )
        )
    )
    assert audit_actors
    assert set(audit_actors) == {principal.actor}
