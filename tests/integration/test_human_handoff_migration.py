import asyncio

import pytest
from migrations.versions.a9d4e6f2b713_repair_human_handoff_lifecycle import (
    _LOCK_AFFECTED_CONVERSATIONS,
    _LOCK_RESOLVED_PLATFORM_ACCOUNTS,
    _REPAIR_RESOLVED_HANDOFFS,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from tests.integration.migration_support import assert_alembic_succeeds, temporary_database

pytestmark = pytest.mark.integration


async def test_human_handoff_repair_downgrade_and_reupgrade():
    async with temporary_database("social_reply_handoff") as (_database_name, database_url):
        await assert_alembic_succeeds(database_url, "upgrade", "f8a1c3d5e702")
        engine = create_async_engine(database_url)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO platform_accounts "
                    "(id, tenant_id, brand_id, platform, name, public_id, config, capability, "
                    "config_version, automation_default, status) VALUES "
                    "('10000000-0000-0000-0000-000000000001', 'tenant-a', 'brand-a', "
                    "'telegram', 'Telegram', 'handoff-telegram', '{}'::jsonb, "
                    '\'{"dm": true, "max_text_length": 1000}\'::jsonb, 1, '
                    "'BOT_ACTIVE', 'active'), "
                    "('10000000-0000-0000-0000-000000000002', 'tenant-a', 'brand-a', "
                    "'facebook', 'Facebook', 'handoff-facebook', '{}'::jsonb, "
                    '\'{"dm": true, "max_text_length": 1000}\'::jsonb, 1, '
                    "'BOT_ACTIVE', 'active')"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO contacts "
                    "(id, tenant_id, platform, platform_account_id, external_user_id) "
                    "SELECT ('20000000-0000-0000-0000-' || lpad(n::text, 12, '0'))::uuid, "
                    "'tenant-a', CASE WHEN n = 6 THEN 'facebook' ELSE 'telegram' END, "
                    "CASE WHEN n = 6 THEN '10000000-0000-0000-0000-000000000002'::uuid "
                    "ELSE '10000000-0000-0000-0000-000000000001'::uuid END, "
                    "'user-' || n FROM generate_series(1, 10) AS n"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO conversations "
                    "(id, tenant_id, brand_id, platform, platform_account_id, contact_id, "
                    "conversation_key, channel_type, decision_generation) "
                    "SELECT ('30000000-0000-0000-0000-' || lpad(n::text, 12, '0'))::uuid, "
                    "'tenant-a', 'brand-a', CASE WHEN n = 6 THEN 'facebook' ELSE 'telegram' END, "
                    "CASE WHEN n = 6 THEN '10000000-0000-0000-0000-000000000002'::uuid "
                    "ELSE '10000000-0000-0000-0000-000000000001'::uuid END, "
                    "('20000000-0000-0000-0000-' || lpad(n::text, 12, '0'))::uuid, "
                    "'handoff-' || n, 'dm', 0 FROM generate_series(1, 10) AS n"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO automation_states "
                    "(conversation_id, state, state_version, human_agent_id, resume_policy) VALUES "
                    "('30000000-0000-0000-0000-000000000001', 'HANDOFF_PENDING', "
                    "1, NULL, 'MANUAL'), "
                    "('30000000-0000-0000-0000-000000000002', 'BOT_ACTIVE', 1, NULL, 'MANUAL'), "
                    "('30000000-0000-0000-0000-000000000003', 'HANDOFF_PENDING', "
                    "1, 'legacy', 'MANUAL'), "
                    "('30000000-0000-0000-0000-000000000004', 'HUMAN_ACTIVE', "
                    "1, 'legacy', 'MANUAL'), "
                    "('30000000-0000-0000-0000-000000000005', 'CLOSED', 1, 'legacy', 'MANUAL'), "
                    "('30000000-0000-0000-0000-000000000006', 'HANDOFF_PENDING', "
                    "1, 'legacy', 'MANUAL'), "
                    "('30000000-0000-0000-0000-000000000007', 'BOT_ACTIVE', 1, NULL, 'MANUAL'), "
                    "('30000000-0000-0000-0000-000000000008', 'BOT_DRAFT_ONLY', "
                    "1, NULL, 'MANUAL'), "
                    "('30000000-0000-0000-0000-000000000009', 'HUMAN_ACTIVE', "
                    "4, 'user:legacy-owner', 'MANUAL'), "
                    "('30000000-0000-0000-0000-000000000010', 'HUMAN_ACTIVE', "
                    "5, 'user:erin', 'MANUAL')"
                )
            )
            await connection.execute(
                text(
                    "UPDATE automation_states SET state_changed_reason = CASE "
                    "WHEN conversation_id = "
                    "'30000000-0000-0000-0000-000000000009'::uuid THEN 'legacy_mismatch' "
                    "ELSE 'correct_attribution' END "
                    "WHERE conversation_id IN ("
                    "'30000000-0000-0000-0000-000000000009'::uuid, "
                    "'30000000-0000-0000-0000-000000000010'::uuid)"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO human_work_items "
                    "(id, tenant_id, conversation_id, status, reason_code, assigned_actor, "
                    "claimed_at, resolved_at, version) VALUES "
                    "('40000000-0000-0000-0000-000000000001', 'tenant-a', "
                    "'30000000-0000-0000-0000-000000000001', 'CLAIMED', 'TEST', "
                    "'user:alice', now(), NULL, 2), "
                    "('40000000-0000-0000-0000-000000000002', 'tenant-a', "
                    "'30000000-0000-0000-0000-000000000002', 'WAITING', 'TEST', "
                    "NULL, NULL, NULL, 1), "
                    "('40000000-0000-0000-0000-000000000003', 'tenant-a', "
                    "'30000000-0000-0000-0000-000000000003', 'RESOLVED', 'TEST', "
                    "NULL, NULL, now(), 3), "
                    "('40000000-0000-0000-0000-000000000004', 'tenant-a', "
                    "'30000000-0000-0000-0000-000000000004', 'RESOLVED', 'TEST', "
                    "NULL, NULL, now(), 3), "
                    "('40000000-0000-0000-0000-000000000005', 'tenant-a', "
                    "'30000000-0000-0000-0000-000000000005', 'WAITING', 'TEST', "
                    "NULL, NULL, NULL, 1), "
                    "('40000000-0000-0000-0000-000000000006', 'tenant-a', "
                    "'30000000-0000-0000-0000-000000000006', 'RESOLVED', 'TEST', "
                    "NULL, NULL, now(), 3), "
                    "('40000000-0000-0000-0000-000000000007', 'tenant-a', "
                    "'30000000-0000-0000-0000-000000000007', 'CLAIMED', 'TEST', "
                    "'user:bob', now(), NULL, 2), "
                    "('40000000-0000-0000-0000-000000000008', 'tenant-a', "
                    "'30000000-0000-0000-0000-000000000008', 'CLAIMED', 'TEST', "
                    "'user:carol', now(), NULL, 2), "
                    "('40000000-0000-0000-0000-000000000009', 'tenant-a', "
                    "'30000000-0000-0000-0000-000000000009', 'CLAIMED', 'TEST', "
                    "'user:dave', now(), NULL, 2), "
                    "('40000000-0000-0000-0000-000000000010', 'tenant-a', "
                    "'30000000-0000-0000-0000-000000000010', 'CLAIMED', 'TEST', "
                    "'user:erin', now(), NULL, 2)"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO outbox_messages "
                    "(id, tenant_id, conversation_id, platform_account_id, destination_type, "
                    "destination_id, message_type, payload, origin_kind, actor_kind, "
                    "idempotency_key, status, attempt_count) VALUES "
                    "('50000000-0000-0000-0000-000000000001', 'tenant-a', "
                    "'30000000-0000-0000-0000-000000000001', "
                    "'10000000-0000-0000-0000-000000000001', 'telegram_dm', '1', 'text', "
                    "'{}'::jsonb, 'DECISION', 'BOT', 'handoff-migration-1', 'PENDING', 0), "
                    "('50000000-0000-0000-0000-000000000002', 'tenant-a', "
                    "'30000000-0000-0000-0000-000000000002', "
                    "'10000000-0000-0000-0000-000000000001', 'telegram_dm', '2', 'text', "
                    "'{}'::jsonb, 'DECISION', 'BOT', 'handoff-migration-2', 'FAILED', 0), "
                    "('50000000-0000-0000-0000-000000000003', 'tenant-a', "
                    "'30000000-0000-0000-0000-000000000001', "
                    "'10000000-0000-0000-0000-000000000001', 'telegram_dm', '1', 'text', "
                    "'{}'::jsonb, 'DRAFT_APPROVAL', 'ADMIN_HUMAN', "
                    "'handoff-migration-3', 'PENDING', 0)"
                )
            )
        await engine.dispose()

        await assert_alembic_succeeds(database_url, "upgrade", "head")
        engine = create_async_engine(database_url)
        async with engine.connect() as connection:
            states = (
                await connection.execute(
                    text(
                        "SELECT right(conversation_id::text, 1), state, state_version, "
                        "human_agent_id, state_changed_reason FROM automation_states "
                        "ORDER BY conversation_id"
                    )
                )
            ).all()
            outboxes = (
                await connection.execute(
                    text("SELECT status, last_error_code FROM outbox_messages ORDER BY id")
                )
            ).all()
        assert states == [
            ("1", "HUMAN_ACTIVE", 2, "user:alice", "migration_claimed_work_human_active"),
            ("2", "HANDOFF_PENDING", 2, None, "migration_waiting_work_handoff_pending"),
            ("3", "BOT_ACTIVE", 2, None, "migration_resolved_work_account_policy"),
            ("4", "HUMAN_ACTIVE", 1, "legacy", None),
            ("5", "CLOSED", 1, "legacy", None),
            ("6", "BOT_DRAFT_ONLY", 2, None, "migration_resolved_work_account_policy"),
            ("7", "HUMAN_ACTIVE", 2, "user:bob", "migration_claimed_work_human_active"),
            ("8", "HUMAN_ACTIVE", 2, "user:carol", "migration_claimed_work_human_active"),
            ("9", "HUMAN_ACTIVE", 5, "user:dave", "migration_claimed_work_human_active"),
            ("0", "HUMAN_ACTIVE", 5, "user:erin", "correct_attribution"),
        ]
        assert outboxes == [
            ("CANCELLED", "TAKEOVER"),
            ("CANCELLED", "TAKEOVER"),
            ("PENDING", None),
        ]
        await engine.dispose()

        await assert_alembic_succeeds(database_url, "downgrade", "f8a1c3d5e702")
        await assert_alembic_succeeds(database_url, "upgrade", "head")
        engine = create_async_engine(database_url)
        async with engine.connect() as connection:
            versions = (
                (
                    await connection.execute(
                        text("SELECT state_version FROM automation_states ORDER BY conversation_id")
                    )
                )
                .scalars()
                .all()
            )
        assert versions == [2, 2, 2, 1, 1, 2, 2, 2, 5, 5]
        await engine.dispose()


async def test_human_handoff_repair_waits_for_committed_account_policy():
    async with temporary_database("social_reply_handoff_policy") as (
        _database_name,
        database_url,
    ):
        await assert_alembic_succeeds(database_url, "upgrade", "f8a1c3d5e702")
        engine = create_async_engine(database_url)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO platform_accounts "
                    "(id, tenant_id, brand_id, platform, name, public_id, config, capability, "
                    "config_version, automation_default, status) VALUES "
                    "('10000000-0000-0000-0000-000000000001', 'tenant-a', 'brand-a', "
                    "'telegram', 'Telegram', 'handoff-policy', '{}'::jsonb, "
                    '\'{"dm": true, "max_text_length": 1000}\'::jsonb, 1, '
                    "'BOT_ACTIVE', 'active')"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO contacts "
                    "(id, tenant_id, platform, platform_account_id, external_user_id) VALUES "
                    "('20000000-0000-0000-0000-000000000001', 'tenant-a', 'telegram', "
                    "'10000000-0000-0000-0000-000000000001', 'user-1')"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO conversations "
                    "(id, tenant_id, brand_id, platform, platform_account_id, contact_id, "
                    "conversation_key, channel_type, decision_generation) VALUES "
                    "('30000000-0000-0000-0000-000000000001', 'tenant-a', 'brand-a', "
                    "'telegram', '10000000-0000-0000-0000-000000000001', "
                    "'20000000-0000-0000-0000-000000000001', 'handoff-policy', 'dm', 0)"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO automation_states "
                    "(conversation_id, state, state_version, human_agent_id, resume_policy) "
                    "VALUES ('30000000-0000-0000-0000-000000000001', "
                    "'HANDOFF_PENDING', 1, 'legacy', 'MANUAL')"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO human_work_items "
                    "(id, tenant_id, conversation_id, status, reason_code, resolved_at, version) "
                    "VALUES ('40000000-0000-0000-0000-000000000001', 'tenant-a', "
                    "'30000000-0000-0000-0000-000000000001', 'RESOLVED', "
                    "'TEST', now(), 3)"
                )
            )

        async def run_resolved_handoff_repair(lock_started):
            async with engine.begin() as connection:
                await connection.execute(_LOCK_AFFECTED_CONVERSATIONS)
                lock_started.set()
                await connection.execute(_LOCK_RESOLVED_PLATFORM_ACCOUNTS)
                await connection.execute(_REPAIR_RESOLVED_HANDOFFS)

        policy_connection = await engine.connect()
        policy_transaction = await policy_connection.begin()
        repair_task = None
        try:
            await policy_connection.execute(
                text(
                    "UPDATE platform_accounts SET automation_default = 'BOT_DRAFT_ONLY' "
                    "WHERE id = '10000000-0000-0000-0000-000000000001'"
                )
            )
            lock_started = asyncio.Event()
            repair_task = asyncio.create_task(run_resolved_handoff_repair(lock_started))
            await asyncio.wait_for(lock_started.wait(), timeout=1)
            await asyncio.sleep(0.1)
            assert not repair_task.done()

            await policy_transaction.commit()
            await asyncio.wait_for(repair_task, timeout=10)
        finally:
            if policy_transaction.is_active:
                await policy_transaction.rollback()
            if repair_task is not None and not repair_task.done():
                repair_task.cancel()
            await policy_connection.close()

        async with engine.connect() as connection:
            repaired_state = (
                await connection.execute(
                    text(
                        "SELECT state, state_version, human_agent_id, state_changed_reason "
                        "FROM automation_states WHERE conversation_id = "
                        "'30000000-0000-0000-0000-000000000001'"
                    )
                )
            ).one()
        assert repaired_state == (
            "BOT_DRAFT_ONLY",
            2,
            None,
            "migration_resolved_work_account_policy",
        )
        await engine.dispose()
