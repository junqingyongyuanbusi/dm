import uuid

import pytest
from sqlalchemy import insert, text
from sqlalchemy.exc import IntegrityError

from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_engine

pytestmark = pytest.mark.integration

EXPECTED_TABLES = {
    "platform_accounts", "contacts", "conversations", "conversation_mappings",
    "messages", "raw_events", "normalized_events", "automation_states",
    "outbox_messages", "audit_logs", "reply_decisions",
}


async def test_all_core_tables_exist(migrated_db):
    engine = get_engine()
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname='public'")
        )
        tables = {r[0] for r in rows}
    assert EXPECTED_TABLES <= tables


async def test_reply_decisions_columns_and_message_index(migrated_db):
    engine = get_engine()
    async with engine.connect() as conn:
        cols = {r[0] for r in await conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='reply_decisions'"
        ))}
        idx = {r[0] for r in await conn.execute(text(
            "SELECT indexname FROM pg_indexes WHERE tablename='messages'"
        ))}
    assert {"id", "conversation_id", "message_id", "action", "risk_level",
            "confidence", "reply_text", "reply_visibility", "reason_codes",
            "source", "prompt_version", "state_version_at_decision",
            "created_at"} <= cols
    assert any("conversation_id" in name for name in idx)


async def test_normalized_events_dedup_constraint(migrated_db, session):
    account_id = uuid.uuid4()
    await session.execute(insert(models.PlatformAccount).values(
        id=account_id, tenant_id="default", brand_id="b1", platform="telegram",
        name="acc", chatwoot_inbox_id=101,
    ))
    values = dict(
        id=uuid.uuid4(), tenant_id="default", platform="telegram",
        platform_account_id=account_id, external_event_id="cw_msg_1",
        event_type="dm.message.created",
    )
    await session.execute(insert(models.NormalizedEvent).values(**values))
    await session.commit()
    with pytest.raises(IntegrityError):
        await session.execute(insert(models.NormalizedEvent).values(
            **{**values, "id": uuid.uuid4()}
        ))
        await session.commit()


async def test_metadata_matches_migrations(migrated_db):
    """漂移护栏：models 改动但忘记生成迁移时在测试期报警"""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    engine = get_engine()
    async with engine.connect() as conn:
        diffs = await conn.run_sync(
            lambda c: compare_metadata(
                MigrationContext.configure(c), models.Base.metadata
            )
        )
    assert diffs == []
