import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from tests.integration.migration_support import assert_alembic_succeeds, run_alembic

from social_reply.shared.config import get_settings

pytestmark = pytest.mark.integration

_BASE_REVISION = "c2f4a6d8e901"
_FEISHU_REVISION = "e4b7c2d9a610"
_FEISHU_ACCOUNT_ID = "00000000-0000-0000-0000-00000000fe15"


async def test_feishu_platform_constraint_upgrade_and_fail_closed_downgrade():
    base_url = make_url(get_settings().database_url)
    database_name = f"social_reply_feishu_{uuid.uuid4().hex[:12]}"
    database_url = base_url.set(database=database_name).render_as_string(hide_password=False)
    admin_engine = create_async_engine(
        base_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    async with admin_engine.connect() as connection:
        await connection.execute(text(f'CREATE DATABASE "{database_name}"'))

    try:
        await assert_alembic_succeeds(database_url, "upgrade", _BASE_REVISION)
        await assert_alembic_succeeds(database_url, "upgrade", _FEISHU_REVISION)
        engine = create_async_engine(database_url)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO platform_accounts ("
                    "id, tenant_id, brand_id, platform, name, config, capability, "
                    "config_version, automation_default, status) VALUES ("
                    ":id, 'default', 'b1', 'feishu', 'feishu-contract', '{}'::jsonb, "
                    '\'{"dm": true, "mentions": true, '
                    '"max_text_length": 4000}\'::jsonb, '
                    "1, 'BOT_DRAFT_ONLY', 'active')"
                ),
                {"id": _FEISHU_ACCOUNT_ID},
            )
        await engine.dispose()

        blocked = await run_alembic(database_url, "downgrade", _BASE_REVISION)
        assert blocked.returncode != 0
        assert "cannot downgrade while Feishu platform accounts exist" in (
            blocked.stdout + blocked.stderr
        )

        engine = create_async_engine(database_url)
        async with engine.begin() as connection:
            revision = (
                await connection.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one()
            assert revision == _FEISHU_REVISION
            await connection.execute(
                text("DELETE FROM platform_accounts WHERE id = :id"),
                {"id": _FEISHU_ACCOUNT_ID},
            )
        await engine.dispose()

        await assert_alembic_succeeds(database_url, "downgrade", _BASE_REVISION)
        engine = create_async_engine(database_url)
        async with engine.connect() as connection:
            old_definition = (
                await connection.execute(
                    text(
                        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                        "WHERE conname = 'ck_platform_accounts_platform'"
                    )
                )
            ).scalar_one()
        await engine.dispose()
        assert "feishu" not in old_definition

        await assert_alembic_succeeds(database_url, "upgrade", "head")
        engine = create_async_engine(database_url)
        async with engine.connect() as connection:
            revision = (
                await connection.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one()
            new_definition = (
                await connection.execute(
                    text(
                        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                        "WHERE conname = 'ck_platform_accounts_platform'"
                    )
                )
            ).scalar_one()
        await engine.dispose()
        assert revision == _FEISHU_REVISION
        assert "feishu" in new_definition
    finally:
        async with admin_engine.connect() as connection:
            await connection.execute(
                text(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
            )
        await admin_engine.dispose()
