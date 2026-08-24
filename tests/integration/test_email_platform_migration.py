import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from tests.integration.migration_support import (
    assert_alembic_succeeds,
    run_alembic,
    temporary_database,
)

pytestmark = pytest.mark.integration

_BASE_REVISION = "b7e4c2d9a615"
_EMAIL_REVISION = "e9a1c4f7b620"
_HEAD_REVISION = "c3e7a9f1b204"


async def test_email_contract_upgrade_constraints_and_fail_closed_downgrade():
    async with temporary_database("social_reply_email") as database_url:
        await assert_alembic_succeeds(database_url, "upgrade", _BASE_REVISION)
        await assert_alembic_succeeds(database_url, "upgrade", _EMAIL_REVISION)

        account_id = uuid.uuid4()
        checkpoint_id = uuid.uuid4()
        run_id = uuid.uuid4()
        engine = create_async_engine(database_url)
        async with engine.begin() as connection:
            rate_index = (
                await connection.execute(
                    text(
                        "SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema() "
                        "AND tablename = 'outbox_messages' "
                        "AND indexname = 'ix_outbox_email_bot_sent_account_time'"
                    )
                )
            ).scalar_one()
            assert "(platform_account_id, sent_at, conversation_id)" in rate_index
            assert "status = 'SENT'::text" in rate_index
            assert "destination_type = 'email_reply'::text" in rate_index
            assert "origin_kind = 'DECISION'::text" in rate_index
            assert "actor_kind = 'BOT'::text" in rate_index
            index_valid = (
                await connection.execute(
                    text(
                        "SELECT index_row.indisvalid AND index_row.indisready "
                        "FROM pg_index AS index_row "
                        "JOIN pg_class AS index_class ON index_class.oid = index_row.indexrelid "
                        "WHERE index_class.relname = "
                        "'ix_outbox_email_bot_sent_account_time'"
                    )
                )
            ).scalar_one()
            assert index_valid is True
            validated_constraints = dict(
                (
                    await connection.execute(
                        text(
                            "SELECT conname, convalidated FROM pg_constraint WHERE conname IN ("
                            "'ck_platform_accounts_platform', "
                            "'ck_platform_checkpoints_stream', "
                            "'ck_platform_checkpoints_scope', "
                            "'ck_sync_gaps_type')"
                        )
                    )
                ).all()
            )
            assert validated_constraints == {
                "ck_platform_accounts_platform": True,
                "ck_platform_checkpoints_scope": True,
                "ck_platform_checkpoints_stream": True,
                "ck_sync_gaps_type": True,
            }
            await connection.execute(
                text(
                    "INSERT INTO platform_accounts ("
                    "id, tenant_id, brand_id, platform, name, config, capability, "
                    "config_version, automation_default, status) VALUES ("
                    ":id, 'default', 'b1', 'email', 'email-contract', '{}'::jsonb, "
                    '\'{"dm": true, "max_text_length": 4000}\'::jsonb, '
                    "1, 'BOT_DRAFT_ONLY', 'active')"
                ),
                {"id": account_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO platform_checkpoints ("
                    "id, tenant_id, platform_account_id, stream, scope_key) VALUES ("
                    ":id, 'default', :account_id, 'EMAIL_IMAP', '')"
                ),
                {"id": checkpoint_id, "account_id": account_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO sync_runs ("
                    "id, checkpoint_id, claim_token, mode, status) VALUES ("
                    ":id, :checkpoint_id, :claim_token, 'POLL', 'GAPPED')"
                ),
                {"id": run_id, "checkpoint_id": checkpoint_id, "claim_token": uuid.uuid4()},
            )
            await connection.execute(
                text(
                    "INSERT INTO sync_gaps ("
                    "id, checkpoint_id, sync_run_id, gap_type, status) VALUES ("
                    ":id, :checkpoint_id, :run_id, 'EMAIL_UIDVALIDITY_CHANGED', 'OPEN')"
                ),
                {"id": uuid.uuid4(), "checkpoint_id": checkpoint_id, "run_id": run_id},
            )

        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO platform_checkpoints ("
                        "id, tenant_id, platform_account_id, stream, scope_key) VALUES ("
                        ":id, 'default', :account_id, 'EMAIL_IMAP', 'INBOX')"
                    ),
                    {"id": uuid.uuid4(), "account_id": account_id},
                )
        await engine.dispose()

        blocked = await run_alembic(database_url, "downgrade", _BASE_REVISION)
        assert blocked.returncode != 0
        assert "cannot downgrade while email platform accounts exist" in (
            blocked.stdout + blocked.stderr
        )

        engine = create_async_engine(database_url)
        async with engine.begin() as connection:
            revision = (
                await connection.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one()
            assert revision == _EMAIL_REVISION
            blocked_index_valid = (
                await connection.execute(
                    text(
                        "SELECT index_row.indisvalid "
                        "FROM pg_index AS index_row "
                        "JOIN pg_class AS index_class ON index_class.oid = index_row.indexrelid "
                        "WHERE index_class.relname = "
                        "'ix_outbox_email_bot_sent_account_time'"
                    )
                )
            ).scalar_one()
            assert blocked_index_valid is True
            await connection.execute(
                text("DELETE FROM platform_accounts WHERE id = :id"),
                {"id": account_id},
            )
        await engine.dispose()

        await assert_alembic_succeeds(database_url, "downgrade", _BASE_REVISION)
        engine = create_async_engine(database_url)
        async with engine.connect() as connection:
            assert (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM pg_indexes WHERE schemaname = current_schema() "
                        "AND indexname = 'ix_outbox_email_bot_sent_account_time'"
                    )
                )
            ).scalar_one() == 0
            definitions = {
                row.conname: row.definition
                for row in await connection.execute(
                    text(
                        "SELECT conname, pg_get_constraintdef(oid) AS definition "
                        "FROM pg_constraint WHERE conname IN ("
                        "'ck_platform_accounts_platform', "
                        "'ck_platform_checkpoints_stream', "
                        "'ck_platform_checkpoints_scope', "
                        "'ck_sync_gaps_type')"
                    )
                )
            }
        await engine.dispose()
        assert "email" not in definitions["ck_platform_accounts_platform"]
        assert "EMAIL_IMAP" not in definitions["ck_platform_checkpoints_stream"]
        assert "EMAIL_IMAP" not in definitions["ck_platform_checkpoints_scope"]
        assert "EMAIL_UIDVALIDITY_CHANGED" not in definitions["ck_sync_gaps_type"]

        await assert_alembic_succeeds(database_url, "upgrade", "head")
        engine = create_async_engine(database_url)
        async with engine.connect() as connection:
            revision = (
                await connection.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one()
            rate_index_count = (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM pg_indexes WHERE schemaname = current_schema() "
                        "AND indexname = 'ix_outbox_email_bot_sent_account_time'"
                    )
                )
            ).scalar_one()
            reupgrade_index_valid = (
                await connection.execute(
                    text(
                        "SELECT index_row.indisvalid AND index_row.indisready "
                        "FROM pg_index AS index_row "
                        "JOIN pg_class AS index_class ON index_class.oid = index_row.indexrelid "
                        "WHERE index_class.relname = "
                        "'ix_outbox_email_bot_sent_account_time'"
                    )
                )
            ).scalar_one()
        await engine.dispose()
        assert revision == _HEAD_REVISION
        assert rate_index_count == 1
        assert reupgrade_index_valid is True


async def test_email_contract_upgrade_replaces_interrupted_same_name_index():
    async with temporary_database("social_reply_email_index_retry") as database_url:
        await assert_alembic_succeeds(database_url, "upgrade", _BASE_REVISION)
        engine = create_async_engine(database_url)
        async with engine.begin() as connection:
            await connection.execute(
                text("CREATE INDEX ix_outbox_email_bot_sent_account_time ON outbox_messages (id)")
            )
        await engine.dispose()

        await assert_alembic_succeeds(database_url, "upgrade", _EMAIL_REVISION)
        engine = create_async_engine(database_url)
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT pg_get_indexdef(index_class.oid) AS definition, "
                        "index_row.indisvalid, index_row.indisready "
                        "FROM pg_index AS index_row "
                        "JOIN pg_class AS index_class ON index_class.oid = index_row.indexrelid "
                        "WHERE index_class.relname = "
                        "'ix_outbox_email_bot_sent_account_time'"
                    )
                )
            ).one()
        await engine.dispose()

        assert "(platform_account_id, sent_at, conversation_id)" in row.definition
        assert row.indisvalid is True
        assert row.indisready is True
