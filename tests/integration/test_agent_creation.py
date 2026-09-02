import httpx
import pytest
from sqlalchemy import func, select

from apps.api.main import create_app
from social_reply.application.account_management.auth import hash_password
from social_reply.application.reply_decision.business_prompt import load_business_prompt
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory

pytestmark = pytest.mark.integration


def _app_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://test",
        follow_redirects=False,
    )


async def _login_admin(client: httpx.AsyncClient) -> str:
    page = await client.get("/admin/login")
    assert page.status_code == 200
    csrf = client.cookies["reply_admin_csrf"]
    response = await client.post(
        "/admin/login",
        data={
            "csrf_token": csrf,
            "username": "admin",
            "password": "test-admin-password",
        },
    )
    assert response.status_code == 303
    return csrf


async def test_admin_can_create_agent_and_continue_to_instructions(migrated_db) -> None:
    async with _app_client() as client:
        csrf = await _login_admin(client)
        page = await client.get("/app/t/default/agents/new")
        assert page.status_code == 200
        assert 'action="/app/t/default/agents"' in page.text
        assert "创建后会发生什么" in page.text

        response = await client.post(
            "/app/t/default/agents",
            data={
                "csrf_token": csrf,
                "name": "Indonesia Support",
                "slug": "indonesia_support",
                "description": "Handle Indonesian customer support.",
            },
        )
        assert response.status_code == 303
        assert response.headers["location"] == (
            "/app/t/default/agents/indonesia_support/instructions?notice=agent_created"
        )

        instructions = await client.get(response.headers["location"])
        agent_list = await client.get("/app/t/default/agents")
        agent_channels = await client.get(
            "/app/t/default/agents/indonesia_support/channels"
        )
        scoped_channel_setup = await client.get(
            "/app/t/default/channels?brand_id=indonesia_support"
        )

    assert instructions.status_code == 200
    assert "Agent 已创建" in instructions.text
    assert "<h1>Indonesia Support</h1>" in instructions.text
    assert agent_list.status_code == 200
    assert "Indonesia Support" in agent_list.text
    assert 'href="/app/t/default/agents/new"' in agent_list.text
    assert agent_channels.status_code == 200
    assert "/app/t/default/channels?brand_id=indonesia_support" in agent_channels.text
    assert scoped_channel_setup.status_code == 200
    assert "绑定到 Agent 作用域 indonesia_support" in scoped_channel_setup.text
    assert scoped_channel_setup.text.count(
        'name="brand_id" value="indonesia_support"'
    ) >= 4
    async with get_session_factory()() as session:
        agent = await session.scalar(
            select(models.Agent).where(
                models.Agent.tenant_id == "default",
                models.Agent.legacy_brand_id == "indonesia_support",
            )
        )
        version_count = await session.scalar(
            select(func.count())
            .select_from(models.AgentVersion)
            .where(models.AgentVersion.agent_id == agent.id)
        )
        deployment_count = await session.scalar(
            select(func.count())
            .select_from(models.AgentDeployment)
            .where(models.AgentDeployment.agent_id == agent.id)
        )
    assert agent.name == "Indonesia Support"
    assert version_count == 1
    assert deployment_count == 0


async def test_agent_creation_enforces_csrf_validation_and_scope_uniqueness(
    migrated_db,
) -> None:
    async with _app_client() as client:
        csrf = await _login_admin(client)
        invalid_csrf = await client.post(
            "/app/t/default/agents",
            data={
                "csrf_token": "wrong-token",
                "name": "Support",
                "slug": "support",
                "description": "",
            },
        )
        assert invalid_csrf.status_code == 403

        invalid_channel_scope = await client.get(
            "/app/t/default/channels?brand_id=support%20team"
        )
        missing_channel_scope = await client.get(
            "/app/t/default/channels?brand_id=missing_agent"
        )
        assert invalid_channel_scope.status_code == 422
        assert missing_channel_scope.status_code == 404

        invalid_scope = await client.post(
            "/app/t/default/agents",
            data={
                "csrf_token": csrf,
                "name": "Support",
                "slug": "support team",
                "description": "",
            },
        )
        assert invalid_scope.status_code == 422
        assert "请检查名称" in invalid_scope.text

        first = await client.post(
            "/app/t/default/agents",
            data={
                "csrf_token": csrf,
                "name": "Support",
                "slug": "support",
                "description": "",
            },
        )
        duplicate = await client.post(
            "/app/t/default/agents",
            data={
                "csrf_token": csrf,
                "name": "Duplicate",
                "slug": "support",
                "description": "",
            },
        )

    assert first.status_code == 303
    assert duplicate.status_code == 409
    assert "已被使用" in duplicate.text


async def test_agent_instructions_save_as_draft_then_deploy_explicitly(migrated_db) -> None:
    async with _app_client() as client:
        csrf = await _login_admin(client)
        created = await client.post(
            "/app/t/default/agents",
            data={
                "csrf_token": csrf,
                "name": "Release Support",
                "slug": "release_support",
                "description": "Exercise explicit releases.",
            },
        )
        assert created.status_code == 303
        saved = await client.post(
            "/app/t/default/agents/release_support/instructions/save",
            data={
                "csrf_token": csrf,
                "expected_revision": "0",
                "content": "Answer directly and escalate financial risk.",
                "change_note": "Initial release candidate",
            },
        )
        assert saved.status_code == 303

        async with get_session_factory()() as session:
            agent = await session.scalar(
                select(models.Agent).where(
                    models.Agent.tenant_id == "default",
                    models.Agent.legacy_brand_id == "release_support",
                )
            )
            latest_version = await session.scalar(
                select(models.AgentVersion)
                .where(models.AgentVersion.agent_id == agent.id)
                .order_by(models.AgentVersion.revision.desc())
            )
            agent_id = agent.id
            latest_version_id = latest_version.id
            session.add(
                models.PlatformAccount(
                    tenant_id="default",
                    brand_id="release_support",
                    platform="telegram",
                    name="Release Support",
                    external_account_id="release-support-1",
                    public_id="release-support-public",
                    config={},
                    capability={},
                    automation_default="BOT_DRAFT_ONLY",
                    status="active",
                )
            )
            await session.commit()

        editor = await client.get(
            "/app/t/default/agents/release_support/instructions"
        )
        assert editor.status_code == 200
        assert "尚未发布" in editor.text
        assert (
            f"/instructions/releases/{latest_version_id}/deploy" in editor.text
        )

        invalid_csrf = await client.post(
            f"/app/t/default/agents/release_support/instructions/releases/"
            f"{latest_version_id}/deploy",
            data={
                "csrf_token": "wrong-token",
                "expected_deployment_revision": "0",
            },
        )
        assert invalid_csrf.status_code == 403

        deployed = await client.post(
            f"/app/t/default/agents/release_support/instructions/releases/"
            f"{latest_version_id}/deploy",
            data={
                "csrf_token": csrf,
                "expected_deployment_revision": "0",
            },
        )
        assert deployed.status_code == 303
        assert deployed.headers["location"].endswith("?notice=deployed")
        deployed_page = await client.get(deployed.headers["location"])
        assert deployed_page.status_code == 200
        assert "生产已是最新版本" in deployed_page.text
        assert "生产中" in deployed_page.text

    async with get_session_factory()() as session:
        deployment = await session.scalar(
            select(models.AgentDeployment).where(
                models.AgentDeployment.tenant_id == "default",
                models.AgentDeployment.agent_id == agent_id,
            )
        )
        runtime_prompt = await load_business_prompt(
            session,
            "default",
            "release_support",
        )
    assert deployment.agent_version_id == latest_version_id
    assert deployment.revision == 1
    assert runtime_prompt.instructions.text == (
        "Answer directly and escalate financial risk."
    )


async def test_database_user_cannot_create_agents(migrated_db) -> None:
    async with get_session_factory()() as session:
        session.add(
            models.AdminUser(
                username="agent-viewer",
                password_hash=await hash_password("agent-viewer-password-123"),
                tenant_id="default",
                role="USER",
                must_change_password=False,
                status="active",
            )
        )
        await session.commit()

    async with _app_client() as client:
        await client.get("/admin/login")
        csrf = client.cookies["reply_admin_csrf"]
        login = await client.post(
            "/admin/login",
            data={
                "csrf_token": csrf,
                "username": "agent-viewer",
                "password": "agent-viewer-password-123",
            },
        )
        assert login.status_code == 303
        page = await client.get("/app/t/default/agents/new")
        create = await client.post(
            "/app/t/default/agents",
            data={
                "csrf_token": csrf,
                "name": "Forbidden",
                "slug": "forbidden",
                "description": "",
            },
        )

    assert page.status_code == 403
    assert create.status_code == 403
