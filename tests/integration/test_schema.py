import uuid

import pytest
from sqlalchemy import insert, text
from sqlalchemy.exc import IntegrityError

from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_engine

pytestmark = pytest.mark.integration

EXPECTED_TABLES = {
    "admin_users",
    "admin_sessions",
    "platform_apps",
    "platform_accounts",
    "contacts",
    "conversations",
    "conversation_mappings",
    "messages",
    "raw_events",
    "normalized_events",
    "automation_states",
    "outbox_messages",
    "audit_logs",
    "provisioning_jobs",
    "decision_jobs",
    "reply_decisions",
    "delivery_attempts",
}


async def test_admin_auth_tables_have_constraints_and_indexes(migrated_db):
    engine = get_engine()
    async with engine.connect() as conn:
        user_constraints = {
            row[0]
            for row in await conn.execute(
                text(
                    "SELECT constraint_name FROM information_schema.table_constraints "
                    "WHERE table_name='admin_users'"
                )
            )
        }
        session_constraints = {
            row[0]
            for row in await conn.execute(
                text(
                    "SELECT constraint_name FROM information_schema.table_constraints "
                    "WHERE table_name='admin_sessions'"
                )
            )
        }
        session_columns = {
            row[0]
            for row in await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='admin_sessions'"
                )
            )
        }
        session_indexes = {
            row[0]
            for row in await conn.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename='admin_sessions'")
            )
        }
    assert any("username" in name for name in user_constraints)
    assert any("tenant_id" in name for name in user_constraints)
    assert "credential_fingerprint" in session_columns
    assert "ck_admin_sessions_single_identity" in session_constraints
    assert "ix_admin_sessions_user_id" in session_indexes
    assert "ix_admin_sessions_expires_at" in session_indexes


async def test_platform_apps_and_account_fk_exist(migrated_db):
    engine = get_engine()
    async with engine.connect() as conn:
        account_cols = {
            row[0]
            for row in await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='platform_accounts'"
                )
            )
        }
    assert "platform_app_id" in account_cols


async def test_provisioning_jobs_have_durable_recovery_index(migrated_db):
    engine = get_engine()
    async with engine.connect() as conn:
        cols = {
            row[0]
            for row in await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='provisioning_jobs'"
                )
            )
        }
        indexes = {
            row[0]
            for row in await conn.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename='provisioning_jobs'")
            )
        }
    assert {
        "tenant_id",
        "platform",
        "idempotency_key",
        "staging_secret",
        "status",
        "current_step",
        "next_attempt_at",
        "result",
        "last_error_code",
    } <= cols
    assert "ix_provisioning_jobs_status_next_attempt" in indexes


async def test_delivery_attempts_and_outbox_index(migrated_db):
    engine = get_engine()
    async with engine.connect() as conn:
        cols = {
            r[0]
            for r in await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='delivery_attempts'"
                )
            )
        }
        idx = {
            r[0]
            for r in await conn.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename='outbox_messages'")
            )
        }
    assert {"id", "outbox_id", "attempt_no", "outcome", "error_code", "created_at"} <= cols
    assert any("conversation" in name and "status" in name for name in idx)


async def test_all_core_tables_exist(migrated_db):
    engine = get_engine()
    async with engine.connect() as conn:
        rows = await conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public'"))
        tables = {r[0] for r in rows}
    assert EXPECTED_TABLES <= tables


async def test_message_history_columns_and_indexes(migrated_db):
    engine = get_engine()
    async with engine.connect() as conn:
        cols = {
            row[0]
            for row in await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='messages'"
                )
            )
        }
        indexes = {
            row[0]
            for row in await conn.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename='messages'")
            )
        }
        constraints = {
            row[0]
            for row in await conn.execute(
                text(
                    "SELECT constraint_name FROM information_schema.table_constraints "
                    "WHERE table_name='messages'"
                )
            )
        }
    assert {"history_seq", "source_outbox_id"} <= cols
    assert "ix_messages_conversation_history" in indexes
    assert "uq_messages_history_seq" in constraints
    assert "uq_messages_source_outbox_id" in constraints


async def test_reply_decisions_columns_and_message_index(migrated_db):
    engine = get_engine()
    async with engine.connect() as conn:
        cols = {
            r[0]
            for r in await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='reply_decisions'"
                )
            )
        }
        idx = {
            r[0]
            for r in await conn.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename='messages'")
            )
        }
    assert {
        "id",
        "conversation_id",
        "message_id",
        "action",
        "risk_level",
        "confidence",
        "reply_text",
        "reply_visibility",
        "reason_codes",
        "source",
        "prompt_version",
        "state_version_at_decision",
        "created_at",
    } <= cols
    assert any("conversation_id" in name for name in idx)


async def test_normalized_events_dedup_constraint(migrated_db, session):
    account_id = uuid.uuid4()
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            tenant_id="default",
            brand_id="b1",
            platform="telegram",
            name="acc",
            chatwoot_inbox_id=101,
        )
    )
    values = dict(
        id=uuid.uuid4(),
        tenant_id="default",
        platform="telegram",
        platform_account_id=account_id,
        external_event_id="cw_msg_1",
        event_type="dm.message.created",
    )
    await session.execute(insert(models.NormalizedEvent).values(**values))
    await session.commit()
    with pytest.raises(IntegrityError):
        await session.execute(
            insert(models.NormalizedEvent).values(**{**values, "id": uuid.uuid4()})
        )
        await session.commit()


async def test_metadata_matches_migrations(migrated_db):
    """漂移护栏：models 改动但忘记生成迁移时在测试期报警"""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    engine = get_engine()
    async with engine.connect() as conn:
        diffs = await conn.run_sync(
            lambda c: compare_metadata(MigrationContext.configure(c), models.Base.metadata)
        )
    assert diffs == []
