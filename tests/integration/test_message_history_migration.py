import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from social_reply.shared.config import get_settings

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[2]


async def _alembic(database_url: str, *args: str) -> None:
    env = {**os.environ, "DATABASE_URL": database_url, "TESTING": "true"}
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", *args],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def test_message_history_migration_backfills_and_round_trips():
    base_url = make_url(get_settings().database_url)
    database_name = f"social_reply_history_{uuid.uuid4().hex[:12]}"
    database_url = base_url.set(database=database_name).render_as_string(
        hide_password=False
    )
    admin_engine = create_async_engine(
        base_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    async with admin_engine.connect() as connection:
        await connection.execute(text(f'CREATE DATABASE "{database_name}"'))

    try:
        await _alembic(database_url, "upgrade", "e7b2c4d9a610")
        engine = create_async_engine(database_url)
        seed_statements = (
            """
            INSERT INTO platform_accounts (
                id, tenant_id, brand_id, platform, name, config,
                capability, config_version, automation_default, status
            ) VALUES (
                '00000000-0000-0000-0000-000000000001',
                'default', 'b1', 'telegram', 'probe', '{}'::jsonb,
                '{}'::jsonb, 1, 'BOT_ACTIVE', 'active'
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

        await _alembic(database_url, "upgrade", "head")
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
        await engine.dispose()
        assert revision == "f3a6c1d8e250"
        assert [(row.history_seq, row.text) for row in rows] == [
            (1, "first"),
            (2, "second"),
            (3, "bot third"),
        ]
        assert rows[-1].sender_type == "bot"
        assert str(rows[-1].source_outbox_id) == "00000000-0000-0000-0000-000000000006"

        await _alembic(database_url, "downgrade", "e7b2c4d9a610")
        await _alembic(database_url, "upgrade", "head")
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
