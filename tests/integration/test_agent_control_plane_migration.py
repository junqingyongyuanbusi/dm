import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from tests.integration.migration_support import (
    assert_alembic_succeeds,
    assert_upgrades_to_current_head,
    temporary_database,
)

pytestmark = pytest.mark.integration

_BASE_REVISION = "f3a7c9e1b5d2"
# Exercise the reversible control-plane chain before workspace authority migration.
_HISTORICAL_REVISION = "a8f4d2c6e901"


async def test_agent_control_plane_backfills_scopes_and_preserves_tenant_boundaries() -> None:
    prompt_version_a = uuid.uuid4()
    prompt_version_b = uuid.uuid4()
    async with temporary_database("social_reply_agent_control") as database_url:
        await assert_alembic_succeeds(database_url, "upgrade", _BASE_REVISION)
        engine = create_async_engine(database_url)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO admin_users "
                    "(id, username, password_hash, tenant_id, role, must_change_password, status) "
                    "VALUES (:id, 'tenant-a-user', 'hash', 'tenant-a', 'USER', false, 'active')"
                ),
                {"id": uuid.uuid4()},
            )
            await connection.execute(
                text(
                    "INSERT INTO platform_accounts "
                    "(id, tenant_id, brand_id, platform, name, config, capability, "
                    "config_version, automation_default, status) VALUES "
                    "(:id, 'tenant-a', 'brand-a', 'telegram', 'Brand A', '{}'::jsonb, "
                    "'{}'::jsonb, 1, 'BOT_DRAFT_ONLY', 'active')"
                ),
                {"id": uuid.uuid4()},
            )
            for tenant_id, brand_id, prompt_version_id in (
                ("tenant-a", "brand-a", prompt_version_a),
                ("tenant-b", "prompt-only", prompt_version_b),
            ):
                await connection.execute(
                    text(
                        "INSERT INTO reply_business_prompt_versions "
                        "(id, tenant_id, brand_id, revision, content, content_hash, created_by) "
                        "VALUES (:id, :tenant_id, :brand_id, 1, 'Be concise.', repeat('a', 64), "
                        "'migration-test')"
                    ),
                    {
                        "brand_id": brand_id,
                        "id": prompt_version_id,
                        "tenant_id": tenant_id,
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO reply_business_prompts "
                        "(id, tenant_id, brand_id, active_version_id, revision, content_hash, "
                        "updated_by) VALUES (:id, :tenant_id, :brand_id, :active_version_id, 1, "
                        "repeat('a', 64), 'migration-test')"
                    ),
                    {
                        "active_version_id": prompt_version_id,
                        "brand_id": brand_id,
                        "id": uuid.uuid4(),
                        "tenant_id": tenant_id,
                    },
                )
        await engine.dispose()

        await assert_alembic_succeeds(database_url, "upgrade", _HISTORICAL_REVISION)
        engine = create_async_engine(database_url)
        async with engine.connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            agents = (
                await connection.execute(
                    text(
                        "SELECT tenant_id, legacy_brand_id, name FROM agents "
                        "ORDER BY tenant_id, legacy_brand_id"
                    )
                )
            ).all()
            versions = (
                await connection.execute(
                    text(
                        "SELECT agent.tenant_id, agent.legacy_brand_id, version.revision, "
                        "version.business_prompt_version_id, version.configuration->>'source' "
                        "AS source FROM agent_versions AS version JOIN agents AS agent "
                        "ON agent.tenant_id=version.tenant_id AND agent.id=version.agent_id "
                        "ORDER BY agent.tenant_id, agent.legacy_brand_id"
                    )
                )
            ).all()
            deployments = (
                await connection.execute(
                    text(
                        "SELECT agent.tenant_id, agent.legacy_brand_id, deployment.environment, "
                        "deployment.revision FROM agent_deployments AS deployment "
                        "JOIN agents AS agent ON agent.tenant_id=deployment.tenant_id "
                        "AND agent.id=deployment.agent_id"
                    )
                )
            ).all()
            trigger_names = {
                row[0]
                for row in await connection.execute(
                    text(
                        "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal "
                        "AND tgname LIKE 'trg_agent_%_append_only'"
                    )
                )
            }

        assert revision == _HISTORICAL_REVISION
        assert agents == [
            ("tenant-a", "brand-a", "Brand A Agent"),
            ("tenant-a", "default", "Default Agent"),
            ("tenant-b", "prompt-only", "Prompt Only Agent"),
        ]
        assert [(row.tenant_id, row.legacy_brand_id, row.revision) for row in versions] == [
            ("tenant-a", "brand-a", 1),
            ("tenant-a", "default", 1),
            ("tenant-b", "prompt-only", 1),
        ]
        version_by_scope = {(row.tenant_id, row.legacy_brand_id): row for row in versions}
        assert version_by_scope[("tenant-a", "brand-a")].business_prompt_version_id == (
            prompt_version_a
        )
        assert version_by_scope[("tenant-a", "default")].business_prompt_version_id is None
        assert version_by_scope[("tenant-b", "prompt-only")].business_prompt_version_id == (
            prompt_version_b
        )
        assert {row.source for row in versions} == {"legacy_scope_backfill"}
        assert deployments == [("tenant-a", "brand-a", "production", 1)]
        assert trigger_names == {
            "trg_agent_deployments_append_only",
            "trg_agent_versions_append_only",
        }

        tenant_a_agent = await _agent_id(engine, "tenant-a", "brand-a")
        tenant_a_version = await _agent_version_id(engine, "tenant-a", tenant_a_agent)
        with pytest.raises(DBAPIError):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO agent_versions "
                        "(id, tenant_id, agent_id, legacy_brand_id, revision, configuration, "
                        "content_hash, created_by) VALUES (:id, 'tenant-b', :agent_id, 'brand-a', "
                        "2, '{}'::jsonb, repeat('b', 64), 'cross-tenant-test')"
                    ),
                    {"agent_id": tenant_a_agent, "id": uuid.uuid4()},
                )
        with pytest.raises(DBAPIError):
            async with engine.begin() as connection:
                await connection.execute(
                    text("UPDATE agent_versions SET change_note='mutated' WHERE id=:id"),
                    {"id": tenant_a_version},
                )
        with pytest.raises(DBAPIError):
            async with engine.begin() as connection:
                await connection.execute(text("DELETE FROM agent_deployments"))

        await engine.dispose()
        await assert_alembic_succeeds(database_url, "downgrade", _BASE_REVISION)
        engine = create_async_engine(database_url)
        async with engine.connect() as connection:
            remaining_tables = {
                row[0]
                for row in await connection.execute(
                    text(
                        "SELECT tablename FROM pg_tables WHERE schemaname='public' "
                        "AND tablename IN ('agents', 'agent_versions', 'agent_deployments')"
                    )
                )
            }
        await engine.dispose()
        assert remaining_tables == set()
        await assert_upgrades_to_current_head(database_url)


async def _agent_id(engine, tenant_id: str, brand_id: str) -> uuid.UUID:
    async with engine.connect() as connection:
        return await connection.scalar(
            text(
                "SELECT id FROM agents WHERE tenant_id=:tenant_id AND legacy_brand_id=:brand_id"
            ),
            {"brand_id": brand_id, "tenant_id": tenant_id},
        )


async def _agent_version_id(engine, tenant_id: str, agent_id: uuid.UUID) -> uuid.UUID:
    async with engine.connect() as connection:
        return await connection.scalar(
            text(
                "SELECT id FROM agent_versions WHERE tenant_id=:tenant_id AND agent_id=:agent_id"
            ),
            {"agent_id": agent_id, "tenant_id": tenant_id},
        )
