import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from tests.integration.migration_support import assert_alembic_succeeds, run_alembic

from social_reply.shared.config import get_settings

pytestmark = pytest.mark.integration

_BASE_REVISION = "c2f4a6d8e901"
_FEISHU_REVISION = "e4b7c2d9a610"
_HEAD_REVISION = "a9d4e6f2b713"
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
        assert revision == _HEAD_REVISION
        assert "feishu" in new_definition
    finally:
        async with admin_engine.connect() as connection:
            await connection.execute(
                text(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
            )
        await admin_engine.dispose()


async def test_empty_database_feishu_dedup_index_upgrade_downgrade_reupgrade():
    base_url = make_url(get_settings().database_url)
    database_name = f"social_reply_feishu_dedup_{uuid.uuid4().hex[:12]}"
    database_url = base_url.set(database=database_name).render_as_string(hide_password=False)
    admin_engine = create_async_engine(
        base_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    async with admin_engine.connect() as connection:
        await connection.execute(text(f'CREATE DATABASE "{database_name}"'))

    try:
        await assert_alembic_succeeds(database_url, "upgrade", "head")
        engine = create_async_engine(database_url)
        async with engine.connect() as connection:
            revision = (
                await connection.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one()
            index_definition = (
                await connection.execute(
                    text(
                        "SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema() "
                        "AND tablename = 'raw_events' "
                        "AND indexname = 'uq_raw_events_feishu_webhook_external_event'"
                    )
                )
            ).scalar_one()
        await engine.dispose()
        assert revision == _HEAD_REVISION
        assert "UNIQUE INDEX" in index_definition
        assert "(platform_account_id, external_event_id)" in index_definition
        assert "source = 'feishu'::text" in index_definition
        assert "ingress_kind = 'webhook'::text" in index_definition
        assert "external_event_id IS NOT NULL" in index_definition

        await assert_alembic_succeeds(database_url, "downgrade", _FEISHU_REVISION)
        engine = create_async_engine(database_url)
        async with engine.connect() as connection:
            revision = (
                await connection.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one()
            index_count = (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM pg_indexes WHERE schemaname = current_schema() "
                        "AND tablename = 'raw_events' "
                        "AND indexname = 'uq_raw_events_feishu_webhook_external_event'"
                    )
                )
            ).scalar_one()
        await engine.dispose()
        assert revision == _FEISHU_REVISION
        assert index_count == 0

        await assert_alembic_succeeds(database_url, "upgrade", "head")
        engine = create_async_engine(database_url)
        async with engine.connect() as connection:
            revision = (
                await connection.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one()
            index_count = (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM pg_indexes WHERE schemaname = current_schema() "
                        "AND tablename = 'raw_events' "
                        "AND indexname = 'uq_raw_events_feishu_webhook_external_event'"
                    )
                )
            ).scalar_one()
        await engine.dispose()
        assert revision == _HEAD_REVISION
        assert index_count == 1
    finally:
        async with admin_engine.connect() as connection:
            await connection.execute(
                text(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
            )
        await admin_engine.dispose()


async def test_feishu_dedup_migration_recovers_invalid_concurrent_index():
    base_url = make_url(get_settings().database_url)
    database_name = f"social_reply_feishu_invalid_{uuid.uuid4().hex[:12]}"
    database_url = base_url.set(database=database_name).render_as_string(hide_password=False)
    admin_engine = create_async_engine(
        base_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    async with admin_engine.connect() as connection:
        await connection.execute(text(f'CREATE DATABASE "{database_name}"'))

    account_id = uuid.uuid4()
    first_raw_id = uuid.uuid4()
    second_raw_id = uuid.uuid4()
    try:
        await assert_alembic_succeeds(database_url, "upgrade", _FEISHU_REVISION)
        engine = create_async_engine(database_url)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO platform_accounts ("
                    "id, tenant_id, brand_id, platform, name, external_account_id, public_id, "
                    "config, capability, config_version, automation_default, status) VALUES ("
                    ":id, 'default', 'b1', 'feishu', 'invalid-index-bot', 'cli_invalid123', "
                    "'fs_invalid', '{}'::jsonb, "
                    '\'{"dm": true, "mentions": true, '
                    '"max_text_length": 4000}\'::jsonb, '
                    "1, 'BOT_DRAFT_ONLY', 'active')"
                ),
                {"id": account_id},
            )
            for raw_event_id in (first_raw_id, second_raw_id):
                await connection.execute(
                    text(
                        "INSERT INTO raw_events ("
                        "id, tenant_id, platform_account_id, source, ingress_kind, "
                        "external_event_id, payload, headers, context, schema_version, "
                        "processing_status, processing_attempt_count) VALUES ("
                        ":id, 'default', :account_id, 'feishu', 'webhook', 'evt_duplicate', "
                        "'{}'::jsonb, '{}'::jsonb, '{}'::jsonb, 1, "
                        "'IGNORED_AT_INGRESS', 0)"
                    ),
                    {"id": raw_event_id, "account_id": account_id},
                )
        await engine.dispose()

        concurrent_engine = create_async_engine(database_url, isolation_level="AUTOCOMMIT")
        with pytest.raises(IntegrityError):
            async with concurrent_engine.connect() as connection:
                await connection.execute(
                    text(
                        "CREATE UNIQUE INDEX CONCURRENTLY "
                        "uq_raw_events_feishu_webhook_external_event ON raw_events "
                        "(platform_account_id, external_event_id) "
                        "WHERE source = 'feishu' AND ingress_kind = 'webhook' "
                        "AND external_event_id IS NOT NULL"
                    )
                )
        async with concurrent_engine.connect() as connection:
            invalid = (
                await connection.execute(
                    text(
                        "SELECT NOT index_row.indisvalid "
                        "FROM pg_index AS index_row "
                        "JOIN pg_class AS index_class ON index_class.oid = index_row.indexrelid "
                        "WHERE index_class.relname = "
                        "'uq_raw_events_feishu_webhook_external_event'"
                    )
                )
            ).scalar_one()
            assert invalid is True
            await connection.execute(
                text("DELETE FROM raw_events WHERE id = :id"),
                {"id": second_raw_id},
            )
        await concurrent_engine.dispose()

        await assert_alembic_succeeds(database_url, "upgrade", "head")
        engine = create_async_engine(database_url)
        async with engine.connect() as connection:
            validity = (
                await connection.execute(
                    text(
                        "SELECT index_row.indisvalid "
                        "FROM pg_index AS index_row "
                        "JOIN pg_class AS index_class ON index_class.oid = index_row.indexrelid "
                        "WHERE index_class.relname = "
                        "'uq_raw_events_feishu_webhook_external_event'"
                    )
                )
            ).scalar_one()
        assert validity is True
        async with engine.begin() as connection:
            with pytest.raises(IntegrityError):
                await connection.execute(
                    text(
                        "INSERT INTO raw_events ("
                        "id, tenant_id, platform_account_id, source, ingress_kind, "
                        "external_event_id, payload, headers, context, schema_version, "
                        "processing_status, processing_attempt_count) VALUES ("
                        ":id, 'default', :account_id, 'feishu', 'webhook', "
                        "'evt_duplicate', '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, 1, "
                        "'IGNORED_AT_INGRESS', 0)"
                    ),
                    {"id": uuid.uuid4(), "account_id": account_id},
                )
        await engine.dispose()
    finally:
        async with admin_engine.connect() as connection:
            await connection.execute(
                text(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
            )
        await admin_engine.dispose()
