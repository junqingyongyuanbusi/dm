import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.migration_support import (
    assert_alembic_succeeds,
    temporary_database,
)

pytestmark = pytest.mark.integration

_OWNERSHIP_REVISION = "c8f1a4d7e203"
_PROFILE_REVISION = "d4e9a2f6b710"


async def test_channel_profile_columns_upgrade_and_downgrade_cleanly() -> None:
    async with temporary_database("social_reply_channel_profiles") as database_url:
        await assert_alembic_succeeds(
            database_url,
            "upgrade",
            _OWNERSHIP_REVISION,
        )
        account_id = uuid.uuid4()
        engine = create_async_engine(database_url)
        async with engine.begin() as connection:
            columns_before = {
                row[0]
                for row in await connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name='platform_accounts'"
                    )
                )
            }
            await connection.execute(
                text(
                    "INSERT INTO platform_accounts ("
                    "id, tenant_id, brand_id, platform, name, external_account_id, "
                    "public_id, config, capability, config_version, "
                    "automation_default, status) VALUES ("
                    ":id, 'tenant-a', 'default', 'telegram', 'Existing Bot', "
                    "'42', 'tg_existing', '{}'::jsonb, '{}'::jsonb, 1, "
                    "'BOT_DRAFT_ONLY', 'active')"
                ),
                {"id": account_id},
            )
        await engine.dispose()

        assert "provider_username" not in columns_before
        assert "avatar_url" not in columns_before
        assert "profile_updated_at" not in columns_before

        await assert_alembic_succeeds(database_url, "upgrade", _PROFILE_REVISION)
        engine = create_async_engine(database_url)
        async with engine.connect() as connection:
            revision = await connection.scalar(
                text("SELECT version_num FROM alembic_version")
            )
            profile_columns = {
                row.column_name: (row.data_type, row.is_nullable)
                for row in await connection.execute(
                    text(
                        "SELECT column_name, data_type, is_nullable "
                        "FROM information_schema.columns "
                        "WHERE table_name='platform_accounts' AND column_name IN ("
                        "'provider_username', 'avatar_url', 'profile_updated_at')"
                    )
                )
            }
            profile_values = (
                await connection.execute(
                    text(
                        "SELECT provider_username, avatar_url, profile_updated_at "
                        "FROM platform_accounts WHERE id=:id"
                    ),
                    {"id": account_id},
                )
            ).one()
        await engine.dispose()

        assert revision == _PROFILE_REVISION
        assert profile_columns == {
            "provider_username": ("text", "YES"),
            "avatar_url": ("text", "YES"),
            "profile_updated_at": (
                "timestamp with time zone",
                "YES",
            ),
        }
        assert profile_values == (None, None, None)

        await assert_alembic_succeeds(
            database_url,
            "downgrade",
            _OWNERSHIP_REVISION,
        )
        engine = create_async_engine(database_url)
        async with engine.connect() as connection:
            columns_after = {
                row[0]
                for row in await connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name='platform_accounts'"
                    )
                )
            }
        await engine.dispose()

        assert "provider_username" not in columns_after
        assert "avatar_url" not in columns_after
        assert "profile_updated_at" not in columns_after
