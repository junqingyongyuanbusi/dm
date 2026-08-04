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
    "platform_checkpoints",
    "sync_runs",
    "sync_gaps",
    "automation_states",
    "human_work_items",
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


async def test_poll_raw_event_journal_columns_and_indexes_exist(migrated_db):
    engine = get_engine()
    async with engine.connect() as conn:
        columns = {
            row[0]
            for row in await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='raw_events'"
                )
            )
        }
        normalized_columns = {
            row[0]
            for row in await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='normalized_events'"
                )
            )
        }
        indexes = {
            row[0]
            for row in await conn.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename='raw_events'")
            )
        }
    assert {
        "tenant_id",
        "platform_account_id",
        "ingress_kind",
        "event_namespace",
        "external_event_id",
        "external_conversation_id",
        "context",
        "schema_version",
        "occurred_at",
        "processing_claim_token",
        "processing_claim_expires_at",
        "processing_attempt_count",
        "processing_next_attempt_at",
        "processing_error_code",
        "processing_last_dispatched_at",
    } <= columns
    assert {"external_conversation_id", "event_metadata"} <= normalized_columns
    assert {
        "ix_raw_events_status_received",
        "ix_raw_events_account_received",
        "ix_raw_events_processing_due",
        "ix_raw_events_tenant_status_received",
        "uq_raw_events_feishu_webhook_external_event",
    } <= indexes


async def test_platform_sync_tables_have_constraints_and_indexes(migrated_db):
    engine = get_engine()
    async with engine.connect() as conn:
        checkpoint_constraints = {
            row[0]
            for row in await conn.execute(
                text(
                    "SELECT constraint_name FROM information_schema.table_constraints "
                    "WHERE table_name='platform_checkpoints'"
                )
            )
        }
        gap_indexes = {
            row[0]
            for row in await conn.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename='sync_gaps'")
            )
        }
    assert {
        "ck_platform_checkpoints_stream",
        "ck_platform_checkpoints_scope",
        "ck_platform_checkpoints_revision",
        "ck_platform_checkpoints_claim",
        "uq_platform_checkpoints_account_stream_scope",
    } <= checkpoint_constraints
    assert {
        "ix_sync_gaps_retry",
        "uq_sync_gaps_active_checkpoint",
    } <= gap_indexes


async def test_admin_health_indexes_exist(migrated_db):
    engine = get_engine()
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT tablename, indexname FROM pg_indexes "
                "WHERE indexname IN ("
                "'ix_raw_events_tenant_status_received',"
                "'ix_outbox_tenant_status_created',"
                "'ix_platform_accounts_tenant_status'"
                ")"
            )
        )
    assert set(rows) == {
        ("raw_events", "ix_raw_events_tenant_status_received"),
        ("outbox_messages", "ix_outbox_tenant_status_created"),
        ("platform_accounts", "ix_platform_accounts_tenant_status"),
    }


async def test_platform_account_contract_constraints_exist(migrated_db):
    engine = get_engine()
    async with engine.connect() as conn:
        constraints = {
            row[0]
            for row in await conn.execute(
                text(
                    "SELECT constraint_name FROM information_schema.table_constraints "
                    "WHERE table_name='platform_accounts'"
                )
            )
        }
    assert {
        "ck_platform_accounts_platform",
        "ck_platform_accounts_status",
        "ck_platform_accounts_capability_object",
    } <= constraints


@pytest.mark.parametrize(
    "overrides",
    [
        {"platform": "unknown"},
        {"status": "CONNECTED"},
        {"capability": "not-an-object"},
    ],
)
async def test_platform_account_contract_rejects_invalid_rows(session, overrides):
    values = {
        "id": uuid.uuid4(),
        "tenant_id": "default",
        "brand_id": "b1",
        "platform": "telegram",
        "name": "invalid",
        "status": "active",
        "capability": {"dm": True, "max_text_length": 4096},
        **overrides,
    }
    with pytest.raises(IntegrityError):
        await session.execute(insert(models.PlatformAccount).values(**values))
        await session.commit()
    await session.rollback()


async def test_platform_account_contract_accepts_feishu(session):
    account_id = uuid.uuid4()
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            tenant_id="default",
            brand_id="b1",
            platform="feishu",
            name="feishu-contract",
            status="active",
            capability={"dm": True, "mentions": True, "max_text_length": 4000},
        )
    )
    await session.commit()
    assert (await session.get(models.PlatformAccount, account_id)).platform == "feishu"


async def test_platform_account_model_defaults_to_canonical_active_status(session):
    account = models.PlatformAccount(
        id=uuid.uuid4(),
        tenant_id="default",
        brand_id="b1",
        platform="telegram",
        name="default-status",
        capability={"dm": True, "max_text_length": 4096},
    )
    session.add(account)
    await session.commit()
    assert account.status == "active"


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
        app_indexes = {
            row[0]
            for row in await conn.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename='platform_apps'")
            )
        }
    assert "platform_app_id" in account_cols
    assert "uq_platform_apps_meta_route_public_id" in app_indexes


async def test_meta_webhook_route_id_is_unique_across_app_families(session):
    public_id = f"shared_{uuid.uuid4().hex}"
    await session.execute(
        insert(models.PlatformApp).values(
            tenant_id="tenant-a",
            platform_family="meta",
            name="Facebook App",
            public_id=public_id,
            config={},
            status="active",
        )
    )
    await session.commit()
    with pytest.raises(IntegrityError):
        await session.execute(
            insert(models.PlatformApp).values(
                tenant_id="tenant-b",
                platform_family="instagram",
                name="Instagram App",
                public_id=public_id,
                config={},
                status="active",
            )
        )
        await session.commit()
    await session.rollback()


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
                    "SELECT column_name FROM information_schema.columns WHERE table_name='messages'"
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


async def test_human_work_items_enforce_tenant_and_claim_assignment(session, migrated_db):
    engine = get_engine()
    async with engine.connect() as conn:
        work_constraints = {
            row[0]
            for row in await conn.execute(
                text(
                    "SELECT constraint_name FROM information_schema.table_constraints "
                    "WHERE table_name='human_work_items'"
                )
            )
        }
        conversation_constraints = {
            row[0]
            for row in await conn.execute(
                text(
                    "SELECT constraint_name FROM information_schema.table_constraints "
                    "WHERE table_name='conversations'"
                )
            )
        }
    assert {
        "ck_human_work_items_claimed_assignment",
        "fk_human_work_items_tenant_conversation",
    } <= work_constraints
    assert "uq_conversations_tenant_id_id" in conversation_constraints

    account_id, contact_id, conversation_id = (uuid.uuid4() for _ in range(3))
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            tenant_id="tenant-a",
            brand_id="b1",
            platform="telegram",
            name="human-work-schema",
        )
    )
    await session.execute(
        insert(models.Contact).values(
            id=contact_id,
            tenant_id="tenant-a",
            platform="telegram",
            platform_account_id=account_id,
            external_user_id="human-work-schema",
        )
    )
    await session.execute(
        insert(models.Conversation).values(
            id=conversation_id,
            tenant_id="tenant-a",
            brand_id="b1",
            platform="telegram",
            platform_account_id=account_id,
            contact_id=contact_id,
            conversation_key=f"schema:{conversation_id}",
        )
    )
    await session.commit()

    with pytest.raises(IntegrityError):
        await session.execute(
            insert(models.HumanWorkItem).values(
                tenant_id="tenant-a",
                conversation_id=conversation_id,
                status="CLAIMED",
                reason_code="TEST",
            )
        )
        await session.commit()
    await session.rollback()

    with pytest.raises(IntegrityError):
        await session.execute(
            insert(models.HumanWorkItem).values(
                tenant_id="tenant-b",
                conversation_id=conversation_id,
                status="WAITING",
                reason_code="TEST",
            )
        )
        await session.commit()
    await session.rollback()


async def test_prompt_and_knowledge_governance_schema_matches_contract(migrated_db):
    engine = get_engine()
    async with engine.connect() as conn:
        columns = {
            (row.table_name, row.column_name): row
            for row in await conn.execute(
                text(
                    "SELECT table_name, column_name, is_nullable, column_default, data_type "
                    "FROM information_schema.columns "
                    "WHERE (table_name='reply_prompts' AND column_name='voice_preferences') "
                    "OR (table_name='knowledge_documents' "
                    "AND column_name IN ('status', 'is_official_contact'))"
                )
            )
        }
        constraints = {
            row[0]
            for row in await conn.execute(
                text(
                    "SELECT constraint_name FROM information_schema.table_constraints "
                    "WHERE table_name='knowledge_documents'"
                )
            )
        }
    voice = columns[("reply_prompts", "voice_preferences")]
    knowledge_status = columns[("knowledge_documents", "status")]
    official = columns[("knowledge_documents", "is_official_contact")]
    assert voice.is_nullable == "NO"
    assert voice.data_type == "jsonb"
    assert all(value in voice.column_default for value in ("professional", "concise", "never"))
    assert knowledge_status.is_nullable == "NO"
    assert "draft" in knowledge_status.column_default
    assert official.is_nullable == "NO"
    assert official.column_default == "false"
    assert "ck_knowledge_documents_status" in constraints


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
