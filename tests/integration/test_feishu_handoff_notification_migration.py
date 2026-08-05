import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from tests.integration.migration_support import (
    assert_alembic_succeeds,
    temporary_database,
)

pytestmark = pytest.mark.integration

_BASE_REVISION = "d3f6a1b8c904"
_HEAD_REVISION = "b7e4c2d9a615"


async def test_upgrade_downgrade_and_reupgrade_feishu_handoff_notifications():
    async with temporary_database("social_reply_feishu_handoff") as database_url:
        await assert_alembic_succeeds(database_url, "upgrade", _BASE_REVISION)
        await assert_alembic_succeeds(database_url, "upgrade", "head")

        engine = create_async_engine(database_url)
        async with engine.connect() as connection:
            revision = (
                await connection.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one()
            tables = {
                row[0]
                for row in await connection.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema='public'"
                    )
                )
            }
            work_columns = {
                row[0]
                for row in await connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name='human_work_items'"
                    )
                )
            }
            constraints = {
                row[0]
                for row in await connection.execute(
                    text(
                        "SELECT constraint_name FROM information_schema.table_constraints "
                        "WHERE table_name='handoff_notification_intents'"
                    )
                )
            }
        await engine.dispose()

        assert revision == _HEAD_REVISION
        assert {
            "tenant_feishu_handoff_configs",
            "feishu_handoff_operators",
            "handoff_notification_intents",
            "feishu_card_action_receipts",
        } <= tables
        assert {"resolved_actor", "resolution_evidence", "resolution_outbox_id"} <= work_columns
        assert {
            "fk_handoff_notification_intents_tenant_work",
            "fk_handoff_notification_intents_tenant_conversation",
            "ck_handoff_notification_intents_sending_lease",
        } <= constraints

        await assert_alembic_succeeds(database_url, "downgrade", _BASE_REVISION)
        engine = create_async_engine(database_url)
        async with engine.connect() as connection:
            tables_after_downgrade = {
                row[0]
                for row in await connection.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema='public'"
                    )
                )
            }
            work_columns_after_downgrade = {
                row[0]
                for row in await connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name='human_work_items'"
                    )
                )
            }
        await engine.dispose()

        assert "handoff_notification_intents" not in tables_after_downgrade
        assert "resolution_evidence" not in work_columns_after_downgrade

        await assert_alembic_succeeds(database_url, "upgrade", "head")
        engine = create_async_engine(database_url)
        async with engine.connect() as connection:
            reupgraded_revision = (
                await connection.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one()
        await engine.dispose()
        assert reupgraded_revision == _HEAD_REVISION
