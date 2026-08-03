import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from tests.integration.migration_support import assert_alembic_succeeds, run_alembic

from social_reply.shared.config import get_settings

pytestmark = pytest.mark.integration


async def test_platform_account_migration_rejects_incompatible_capability():
    base_url = make_url(get_settings().database_url)
    database_name = f"social_reply_contract_{uuid.uuid4().hex[:12]}"
    database_url = base_url.set(database=database_name).render_as_string(hide_password=False)
    admin_engine = create_async_engine(
        base_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    async with admin_engine.connect() as connection:
        await connection.execute(text(f'CREATE DATABASE "{database_name}"'))

    try:
        await assert_alembic_succeeds(database_url, "upgrade", "f3a6c1d8e250")
        engine = create_async_engine(database_url)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO platform_accounts ("
                    "id, tenant_id, brand_id, platform, name, config, capability, "
                    "config_version, automation_default, status) VALUES ("
                    "'00000000-0000-0000-0000-000000000001', 'default', 'b1', "
                    "'telegram', 'invalid', '{}'::jsonb, "
                    '\'{"dm": "false", "max_text_length": 4096}\'::jsonb, '
                    "1, 'BOT_ACTIVE', 'active')"
                )
            )
        await engine.dispose()

        result = await run_alembic(database_url, "upgrade", "head")
        assert result.returncode != 0
        assert "capability violates the application contract" in (result.stdout + result.stderr)
    finally:
        async with admin_engine.connect() as connection:
            await connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname=:name AND pid <> pg_backend_pid()"
                ),
                {"name": database_name},
            )
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
        await admin_engine.dispose()


async def test_meta_route_migration_rejects_cross_family_collision():
    base_url = make_url(get_settings().database_url)
    database_name = f"social_reply_meta_route_{uuid.uuid4().hex[:12]}"
    database_url = base_url.set(database=database_name).render_as_string(hide_password=False)
    admin_engine = create_async_engine(
        base_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    async with admin_engine.connect() as connection:
        await connection.execute(text(f'CREATE DATABASE "{database_name}"'))

    try:
        await assert_alembic_succeeds(database_url, "upgrade", "c5a8e2f4d901")
        engine = create_async_engine(database_url)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO platform_apps ("
                    "id, tenant_id, platform_family, name, external_app_id, public_id, "
                    "credential_ref, config, config_version, status) VALUES "
                    "('00000000-0000-0000-0000-000000000101', 'tenant-a', 'meta', "
                    "'Facebook App', 'fb-app', 'shared-route', '', '{}'::jsonb, 1, 'active'), "
                    "('00000000-0000-0000-0000-000000000102', 'tenant-b', 'instagram', "
                    "'Instagram App', 'ig-app', 'shared-route', '', '{}'::jsonb, 1, 'active')"
                )
            )
        await engine.dispose()

        result = await run_alembic(database_url, "upgrade", "head")
        assert result.returncode != 0
        assert "cross-family Meta webhook public_id collision" in (result.stdout + result.stderr)
    finally:
        async with admin_engine.connect() as connection:
            await connection.execute(
                text(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
            )
        await admin_engine.dispose()


async def test_human_work_hardening_repairs_legacy_rows():
    base_url = make_url(get_settings().database_url)
    database_name = f"social_reply_human_work_{uuid.uuid4().hex[:12]}"
    database_url = base_url.set(database=database_name).render_as_string(hide_password=False)
    admin_engine = create_async_engine(
        base_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    async with admin_engine.connect() as connection:
        await connection.execute(text(f'CREATE DATABASE "{database_name}"'))

    try:
        await assert_alembic_succeeds(database_url, "upgrade", "a1c4e8b7f302")
        engine = create_async_engine(database_url)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO platform_accounts ("
                    "id, tenant_id, brand_id, platform, name, capability, "
                    "config_version, automation_default, status) VALUES ("
                    "'00000000-0000-0000-0000-000000000101', 'tenant-a', 'b1', "
                    "'telegram', 'human-work-migration', "
                    '\'{"dm": true, "max_text_length": 4096}\'::jsonb, '
                    "1, 'BOT_ACTIVE', 'active')"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO contacts ("
                    "id, tenant_id, platform, platform_account_id, external_user_id) "
                    "VALUES ("
                    "'00000000-0000-0000-0000-000000000102', 'tenant-a', "
                    "'telegram', '00000000-0000-0000-0000-000000000101', 'u1')"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO conversations ("
                    "id, tenant_id, brand_id, platform, platform_account_id, "
                    "contact_id, conversation_key, channel_type) VALUES "
                    "('00000000-0000-0000-0000-000000000103', 'tenant-a', 'b1', "
                    "'telegram', '00000000-0000-0000-0000-000000000101', "
                    "'00000000-0000-0000-0000-000000000102', 'migration:u1', 'dm'), "
                    "('00000000-0000-0000-0000-000000000104', 'tenant-a', 'b1', "
                    "'telegram', '00000000-0000-0000-0000-000000000101', "
                    "'00000000-0000-0000-0000-000000000102', 'migration:u2', 'dm'), "
                    "('00000000-0000-0000-0000-000000000105', 'tenant-a', 'b1', "
                    "'telegram', '00000000-0000-0000-0000-000000000101', "
                    "'00000000-0000-0000-0000-000000000102', 'migration:u3', 'dm'), "
                    "('00000000-0000-0000-0000-000000000106', 'tenant-a', 'b1', "
                    "'telegram', '00000000-0000-0000-0000-000000000101', "
                    "'00000000-0000-0000-0000-000000000102', 'migration:u4', 'dm'), "
                    "('00000000-0000-0000-0000-000000000107', 'tenant-a', 'b1', "
                    "'telegram', '00000000-0000-0000-0000-000000000101', "
                    "'00000000-0000-0000-0000-000000000102', 'migration:u5', 'dm')"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO automation_states ("
                    "conversation_id, state, state_version, human_agent_id, "
                    "resume_policy, state_changed_reason) VALUES "
                    "('00000000-0000-0000-0000-000000000103', 'HUMAN_ACTIVE', 1, "
                    "'user:alice', 'MANUAL', 'legacy'), "
                    "('00000000-0000-0000-0000-000000000104', 'HUMAN_ACTIVE', 1, "
                    "NULL, 'MANUAL', 'legacy'), "
                    "('00000000-0000-0000-0000-000000000105', 'HUMAN_ACTIVE', 1, "
                    "'user:bob', 'MANUAL', 'legacy'), "
                    "('00000000-0000-0000-0000-000000000106', 'HUMAN_ACTIVE', 1, "
                    "'user:alice', 'MANUAL', 'legacy'), "
                    "('00000000-0000-0000-0000-000000000107', 'HUMAN_ACTIVE', 1, "
                    "'user:bob', 'MANUAL', 'legacy')"
                )
            )
        await engine.dispose()

        await assert_alembic_succeeds(database_url, "upgrade", "f6c2a9d81b40")
        engine = create_async_engine(database_url)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO admin_users ("
                    "id, username, password_hash, tenant_id, must_change_password, status) "
                    "VALUES "
                    "('00000000-0000-0000-0000-000000000201', 'alice', 'test-hash', "
                    "'tenant-a', false, 'active'), "
                    "('00000000-0000-0000-0000-000000000202', 'bob', 'test-hash', "
                    "'tenant-b', false, 'active')"
                )
            )
            await connection.execute(
                text(
                    "UPDATE human_work_items SET tenant_id = 'legacy-wrong', "
                    "assigned_user_id = CASE conversation_id "
                    "WHEN '00000000-0000-0000-0000-000000000103' "
                    "THEN '00000000-0000-0000-0000-000000000201'::uuid "
                    "WHEN '00000000-0000-0000-0000-000000000105' "
                    "THEN '00000000-0000-0000-0000-000000000202'::uuid END "
                    "WHERE conversation_id IN ("
                    "'00000000-0000-0000-0000-000000000103', "
                    "'00000000-0000-0000-0000-000000000105')"
                )
            )
            await connection.execute(
                text(
                    "UPDATE human_work_items SET "
                    "assigned_user_id = '00000000-0000-0000-0000-000000000201' "
                    "WHERE conversation_id IN ("
                    "'00000000-0000-0000-0000-000000000106', "
                    "'00000000-0000-0000-0000-000000000107')"
                )
            )
            await connection.execute(
                text(
                    "UPDATE human_work_items SET claimed_at = NULL WHERE conversation_id = "
                    "'00000000-0000-0000-0000-000000000106'"
                )
            )
        await engine.dispose()

        await assert_alembic_succeeds(database_url, "upgrade", "head")
        engine = create_async_engine(database_url)
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT conversation_id, tenant_id, status, assigned_user_id, "
                        "assigned_actor, claimed_at, version FROM human_work_items "
                        "ORDER BY conversation_id"
                    )
                )
            ).all()
            revision = (
                await connection.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one()
        await engine.dispose()

        assert revision == "e4b7c2d9a610"
        assert str(rows[0].conversation_id) == "00000000-0000-0000-0000-000000000103"
        assert rows[0].tenant_id == "tenant-a"
        assert rows[0].status == "CLAIMED"
        assert str(rows[0].assigned_user_id) == "00000000-0000-0000-0000-000000000201"
        assert rows[0].assigned_actor == "user:alice"
        assert rows[0].claimed_at is not None
        assert rows[0].version == 1
        assert str(rows[1].conversation_id) == "00000000-0000-0000-0000-000000000104"
        assert rows[1].tenant_id == "tenant-a"
        assert rows[1].status == "WAITING"
        assert rows[1].assigned_user_id is None
        assert rows[1].assigned_actor is None
        assert rows[1].claimed_at is None
        assert rows[1].version == 2
        assert str(rows[2].conversation_id) == "00000000-0000-0000-0000-000000000105"
        assert rows[2].tenant_id == "tenant-a"
        assert rows[2].status == "WAITING"
        assert rows[2].assigned_user_id is None
        assert rows[2].assigned_actor is None
        assert rows[2].claimed_at is None
        assert rows[2].version == 2
        assert str(rows[3].conversation_id) == "00000000-0000-0000-0000-000000000106"
        assert rows[3].tenant_id == "tenant-a"
        assert rows[3].status == "WAITING"
        assert rows[3].assigned_user_id is None
        assert rows[3].assigned_actor is None
        assert rows[3].claimed_at is None
        assert rows[3].version == 2
        assert str(rows[4].conversation_id) == "00000000-0000-0000-0000-000000000107"
        assert rows[4].tenant_id == "tenant-a"
        assert rows[4].status == "WAITING"
        assert rows[4].assigned_user_id is None
        assert rows[4].assigned_actor is None
        assert rows[4].claimed_at is None
        assert rows[4].version == 2
    finally:
        async with admin_engine.connect() as connection:
            await connection.execute(
                text(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
            )
        await admin_engine.dispose()


async def test_message_history_migration_backfills_and_round_trips():
    base_url = make_url(get_settings().database_url)
    database_name = f"social_reply_history_{uuid.uuid4().hex[:12]}"
    database_url = base_url.set(database=database_name).render_as_string(hide_password=False)
    admin_engine = create_async_engine(
        base_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    async with admin_engine.connect() as connection:
        await connection.execute(text(f'CREATE DATABASE "{database_name}"'))

    try:
        await assert_alembic_succeeds(database_url, "upgrade", "e7b2c4d9a610")
        engine = create_async_engine(database_url)
        seed_statements = (
            """
            INSERT INTO platform_accounts (
                id, tenant_id, brand_id, platform, name, config,
                capability, config_version, automation_default, status
            ) VALUES (
                '00000000-0000-0000-0000-000000000001',
                'default', 'b1', 'telegram', 'probe', '{}'::jsonb,
                '{}'::jsonb, 1, 'BOT_ACTIVE', 'CONNECTED'
            )
            """,
            """
            INSERT INTO platform_accounts (
                id, tenant_id, brand_id, platform, name, config,
                capability, config_version, automation_default, status
            ) VALUES (
                '00000000-0000-0000-0000-000000000008',
                'tenant-x', 'b1', 'x', 'x-probe',
                '{"x_dm_cursor": "900", "x_dm_bootstrapped": true,
                  "xchat_cursors": {"self-peer": "700", "self:peer": "900"},
                  "xchat_bootstrapped": {"self-peer": false}}'::jsonb,
                '{}'::jsonb, 1, 'BOT_ACTIVE', 'CONNECTED'
            )
            """,
            """
            INSERT INTO contacts (
                id, tenant_id, platform, platform_account_id, external_user_id
            ) VALUES (
                '00000000-0000-0000-0000-000000000002',
                'default', 'telegram',
                '00000000-0000-0000-0000-000000000001', 'u1'
            )
            """,
            """
            INSERT INTO conversations (
                id, tenant_id, brand_id, platform, platform_account_id,
                contact_id, conversation_key, channel_type
            ) VALUES (
                '00000000-0000-0000-0000-000000000003',
                'default', 'b1', 'telegram',
                '00000000-0000-0000-0000-000000000001',
                '00000000-0000-0000-0000-000000000002', 'probe:u1', 'dm'
            )
            """,
            """
            INSERT INTO messages (
                id, conversation_id, direction, sender_type, text,
                reply_target, private, occurred_at, created_at
            ) VALUES
            (
                '00000000-0000-0000-0000-000000000004',
                '00000000-0000-0000-0000-000000000003',
                'inbound', 'contact', 'first', '{}'::jsonb, false,
                '2026-01-01T10:01:00Z', '2026-01-01T10:01:00Z'
            ),
            (
                '00000000-0000-0000-0000-000000000005',
                '00000000-0000-0000-0000-000000000003',
                'outbound', 'agent', 'second', '{}'::jsonb, false,
                '2026-01-01T10:02:00Z', '2026-01-01T10:02:00Z'
            )
            """,
            """
            INSERT INTO outbox_messages (
                id, tenant_id, conversation_id, platform_account_id,
                destination_type, destination_id, message_type, payload,
                idempotency_key, status, attempt_count, sent_at, created_at
            ) VALUES (
                '00000000-0000-0000-0000-000000000006', 'default',
                '00000000-0000-0000-0000-000000000003',
                '00000000-0000-0000-0000-000000000001',
                'telegram_dm', 'probe:u1', 'text',
                '{"text":"bot third"}'::jsonb,
                'probe-outbox', 'SENT', 1,
                '2026-01-01T10:03:00Z', '2026-01-01T10:03:00Z'
            )
            """,
        )
        async with engine.begin() as connection:
            for statement in seed_statements:
                await connection.execute(text(statement))
        await engine.dispose()

        await assert_alembic_succeeds(database_url, "upgrade", "head")
        engine = create_async_engine(database_url)
        async with engine.connect() as connection:
            revision = (
                await connection.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one()
            rows = (
                await connection.execute(
                    text(
                        "SELECT history_seq, direction, sender_type, text, "
                        "source_outbox_id FROM messages ORDER BY history_seq"
                    )
                )
            ).all()
            account_contract = (
                await connection.execute(
                    text(
                        "SELECT status, capability FROM platform_accounts "
                        "WHERE id='00000000-0000-0000-0000-000000000001'"
                    )
                )
            ).one()
            trigger_count = (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM pg_trigger "
                        "WHERE tgname='trg_raw_event_evidence_append_only'"
                    )
                )
            ).scalar_one()
            checkpoints = (
                await connection.execute(
                    text(
                        "SELECT stream, scope_key, cursor, bootstrapped "
                        "FROM platform_checkpoints "
                        "WHERE platform_account_id="
                        "'00000000-0000-0000-0000-000000000008' "
                        "ORDER BY stream, scope_key"
                    )
                )
            ).all()
        assert revision == "e4b7c2d9a610"
        assert trigger_count == 1
        assert account_contract.status == "active"
        assert account_contract.capability == {
            "dm": False,
            "max_text_length": 4096,
        }
        assert [tuple(row) for row in checkpoints] == [
            ("XCHAT_CONVERSATION", "self:peer", "900", True),
            ("XCHAT_DISCOVERY", "", None, False),
            ("X_LEGACY_DM", "", "900", True),
        ]
        assert [(row.history_seq, row.text) for row in rows] == [
            (1, "first"),
            (2, "second"),
            (3, "bot third"),
        ]
        assert rows[-1].sender_type == "bot"
        assert str(rows[-1].source_outbox_id) == "00000000-0000-0000-0000-000000000006"

        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO raw_events (id, source, payload, headers, context, "
                    "processing_status) VALUES ("
                    "'00000000-0000-0000-0000-000000000007', 'test', "
                    "'{\"value\": 1}'::jsonb, '{}'::jsonb, '{}'::jsonb, 'PENDING')"
                )
            )
            await connection.execute(
                text(
                    "UPDATE raw_events SET processing_status='PROCESSED', "
                    "processing_claim_token="
                    "'00000000-0000-0000-0000-000000000008', "
                    "processing_attempt_count=1, processing_error_code='TEST' "
                    "WHERE id='00000000-0000-0000-0000-000000000007'"
                )
            )
        with pytest.raises(DBAPIError, match="raw_event_evidence_is_append_only"):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE raw_events SET payload='{\"value\": 2}'::jsonb "
                        "WHERE id='00000000-0000-0000-0000-000000000007'"
                    )
                )
        await engine.dispose()

        await assert_alembic_succeeds(database_url, "downgrade", "e7b2c4d9a610")
        await assert_alembic_succeeds(database_url, "upgrade", "head")
        engine = create_async_engine(database_url)
        async with engine.connect() as connection:
            counts = (
                await connection.execute(
                    text(
                        "SELECT COUNT(*), COUNT(source_outbox_id), "
                        "COUNT(DISTINCT history_seq) FROM messages"
                    )
                )
            ).one()
        await engine.dispose()
        assert tuple(counts) == (3, 1, 3)
    finally:
        async with admin_engine.connect() as connection:
            await connection.execute(
                text(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
            )
        await admin_engine.dispose()
