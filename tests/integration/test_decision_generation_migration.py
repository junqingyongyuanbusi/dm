import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from tests.integration.migration_support import assert_alembic_succeeds

from social_reply.shared.config import get_settings

pytestmark = pytest.mark.integration
_BASE_REVISION = "b8e1d4f7a2c3"
_FENCING_REVISION = "c2f4a6d8e901"
_ACCOUNT_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
_CONTACT_ID = uuid.UUID("10000000-0000-0000-0000-000000000002")
_CONVERSATION_ID = uuid.UUID("10000000-0000-0000-0000-000000000003")


async def _create_database(prefix: str):
    base_url = make_url(get_settings().database_url)
    database_name = f"{prefix}_{uuid.uuid4().hex[:12]}"
    database_url = base_url.set(database=database_name).render_as_string(hide_password=False)
    admin_engine = create_async_engine(
        base_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    async with admin_engine.connect() as connection:
        await connection.execute(text(f'CREATE DATABASE "{database_name}"'))
    return database_name, database_url, admin_engine


async def _drop_database(database_name: str, admin_engine) -> None:
    async with admin_engine.connect() as connection:
        await connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)'))
    await admin_engine.dispose()


async def _seed_scope(connection) -> None:
    await connection.execute(
        text(
            """
            INSERT INTO platform_accounts (
                id, tenant_id, brand_id, platform, name, config, capability,
                config_version, automation_default, status
            ) VALUES (
                :account_id, 'default', 'b1', 'telegram', 'migration-account',
                '{}'::jsonb, '{"dm": true, "max_text_length": 4096}'::jsonb,
                1, 'BOT_ACTIVE', 'active'
            )
            """
        ),
        {"account_id": _ACCOUNT_ID},
    )
    await connection.execute(
        text(
            """
            INSERT INTO contacts (
                id, tenant_id, platform, platform_account_id, external_user_id
            ) VALUES (:contact_id, 'default', 'telegram', :account_id, 'user-1')
            """
        ),
        {"contact_id": _CONTACT_ID, "account_id": _ACCOUNT_ID},
    )
    await connection.execute(
        text(
            """
            INSERT INTO conversations (
                id, tenant_id, brand_id, platform, platform_account_id,
                contact_id, conversation_key, channel_type
            ) VALUES (
                :conversation_id, 'default', 'b1', 'telegram', :account_id,
                :contact_id, 'migration:generation', 'dm'
            )
            """
        ),
        {
            "conversation_id": _CONVERSATION_ID,
            "account_id": _ACCOUNT_ID,
            "contact_id": _CONTACT_ID,
        },
    )


async def _insert_message(connection, message_id: uuid.UUID, label: str) -> None:
    await connection.execute(
        text(
            """
            INSERT INTO messages (
                id, conversation_id, direction, sender_type, text,
                reply_target, attachments, private
            ) VALUES (
                :message_id, :conversation_id, 'inbound', 'contact', :label,
                '{}'::jsonb, '[]'::jsonb, false
            )
            """
        ),
        {
            "message_id": message_id,
            "conversation_id": _CONVERSATION_ID,
            "label": label,
        },
    )


async def _insert_legacy_job(
    connection,
    job_id: uuid.UUID,
    message_id: uuid.UUID,
    *,
    status: str = "PENDING",
    created_at: datetime | None = None,
) -> None:
    values = {
        "job_id": job_id,
        "message_id": message_id,
        "conversation_id": _CONVERSATION_ID,
        "account_id": _ACCOUNT_ID,
        "status": status,
        "created_at": created_at,
    }
    await connection.execute(
        text(
            """
            INSERT INTO decision_jobs (
                id, conversation_id, message_id, account_id, snapshot, status,
                attempt_count, created_at
            ) VALUES (
                :job_id, :conversation_id, :message_id, :account_id,
                '{}'::jsonb, :status, 0, COALESCE(CAST(:created_at AS timestamptz), now())
            )
            """
        ),
        values,
    )


async def _insert_outbox(
    connection,
    outbox_id: uuid.UUID,
    *,
    origin: str,
    actor: str,
    status: str,
    suffix: str,
) -> None:
    await connection.execute(
        text(
            """
            INSERT INTO outbox_messages (
                id, tenant_id, conversation_id, platform_account_id,
                destination_type, destination_id, message_type, payload,
                origin_kind, actor_kind, idempotency_key, status, attempt_count
            ) VALUES (
                :outbox_id, 'default', :conversation_id, :account_id,
                'telegram_dm', 'user-1', 'text', '{"text":"reply"}'::jsonb,
                :origin, :actor, :idempotency_key, :status, 0
            )
            """
        ),
        {
            "outbox_id": outbox_id,
            "conversation_id": _CONVERSATION_ID,
            "account_id": _ACCOUNT_ID,
            "origin": origin,
            "actor": actor,
            "idempotency_key": f"migration-{suffix}",
            "status": status,
        },
    )


async def _insert_legacy_decision(
    connection,
    decision_id: uuid.UUID,
    message_id: uuid.UUID,
    outbox_id: uuid.UUID,
) -> None:
    await connection.execute(
        text(
            """
            INSERT INTO reply_decisions (
                id, tenant_id, conversation_id, message_id, action,
                risk_level, confidence, reply_text, reply_visibility,
                reason_codes, source, outbox_id
            ) VALUES (
                :decision_id, 'default', :conversation_id, :message_id,
                'auto_reply', 'low', 0.8, 'reply', 'public', '[]'::jsonb,
                'llm', :outbox_id
            )
            """
        ),
        {
            "decision_id": decision_id,
            "conversation_id": _CONVERSATION_ID,
            "message_id": message_id,
            "outbox_id": outbox_id,
        },
    )


async def test_generation_migration_backfill_triggers_and_downgrade():
    database_name, database_url, admin_engine = await _create_database(
        "social_reply_generation_migration"
    )
    try:
        await assert_alembic_succeeds(database_url, "upgrade", _BASE_REVISION)
        engine = create_async_engine(database_url)
        orphan_message_id = uuid.uuid4()
        private_message_id = uuid.uuid4()
        message_ids = [uuid.uuid4() for _ in range(6)]
        job_ids = [uuid.uuid4() for _ in range(6)]
        outbox_ids = [uuid.uuid4() for _ in range(6)]
        cases = [
            ("DECISION", "BOT", "PENDING", "CANCELLED"),
            ("DECISION", "BOT", "FAILED", "CANCELLED"),
            ("MANUAL_REPLY", "ADMIN_HUMAN", "PENDING", "PENDING"),
            ("DRAFT_APPROVAL", "ADMIN_HUMAN", "PENDING", "PENDING"),
            ("DECISION", "BOT", "SENT", "SENT"),
            ("DECISION", "BOT", "PENDING", "PENDING"),
        ]
        async with engine.begin() as connection:
            await _seed_scope(connection)
            await _insert_message(connection, orphan_message_id, "orphan-without-job")
            await connection.execute(
                text(
                    """
                    INSERT INTO messages (
                        id, conversation_id, direction, sender_type, text,
                        reply_target, attachments, private
                    ) VALUES (
                        :message_id, :conversation_id, 'inbound', 'contact', 'private',
                        '{}'::jsonb, '[]'::jsonb, true
                    )
                    """
                ),
                {
                    "message_id": private_message_id,
                    "conversation_id": _CONVERSATION_ID,
                },
            )
            for index, message_id in enumerate(message_ids, start=1):
                await _insert_message(connection, message_id, f"m{index}")
            for index in reversed(range(6)):
                await _insert_legacy_job(
                    connection,
                    job_ids[index],
                    message_ids[index],
                    created_at=datetime(2026, 1, 1, 0, 0, 6 - index, tzinfo=UTC),
                )
            for index, ((origin, actor, status, _expected), message_id, outbox_id) in enumerate(
                zip(cases, message_ids, outbox_ids, strict=True)
            ):
                await _insert_outbox(
                    connection,
                    outbox_id,
                    origin=origin,
                    actor=actor,
                    status=status,
                    suffix=str(index),
                )
                await _insert_legacy_decision(
                    connection,
                    uuid.uuid4(),
                    message_id,
                    outbox_id,
                )

        await engine.dispose()
        await assert_alembic_succeeds(database_url, "upgrade", _FENCING_REVISION)
        engine = create_async_engine(database_url)
        async with engine.connect() as connection:
            generations = (
                await connection.execute(
                    text(
                        """
                        SELECT message_id, decision_generation, status
                        FROM decision_jobs
                        ORDER BY decision_generation
                        """
                    )
                )
            ).all()
            outbox_statuses = dict(
                (
                    await connection.execute(
                        text("SELECT id, status FROM outbox_messages WHERE id = ANY(:ids)"),
                        {"ids": outbox_ids},
                    )
                ).all()
            )
            message_generations = dict(
                (
                    await connection.execute(
                        text(
                            """
                            SELECT id, decision_generation
                            FROM messages
                            WHERE id = ANY(:ids)
                            """
                        ),
                        {"ids": [orphan_message_id, private_message_id, *message_ids]},
                    )
                ).all()
            )
            conversation_generation = (
                await connection.execute(
                    text("SELECT decision_generation FROM conversations WHERE id=:id"),
                    {"id": _CONVERSATION_ID},
                )
            ).scalar_one()
            schema = (
                await connection.execute(
                    text(
                        """
                        SELECT
                            (SELECT is_nullable FROM information_schema.columns
                             WHERE table_schema='public' AND table_name='messages'
                               AND column_name='decision_generation') AS message_nullable,
                            (SELECT is_nullable FROM information_schema.columns
                             WHERE table_schema='public' AND table_name='decision_jobs'
                               AND column_name='decision_generation') AS job_nullable,
                            EXISTS (
                                SELECT 1 FROM pg_indexes
                                WHERE schemaname='public'
                                  AND indexname='ix_messages_conversation_decision_generation'
                                  AND indexdef LIKE '%(conversation_id, decision_generation)%'
                            ) AS message_index,
                            EXISTS (
                                SELECT 1 FROM pg_constraint
                                WHERE conname='ck_conversations_decision_generation'
                                  AND pg_get_constraintdef(oid) =
                                      'CHECK ((decision_generation >= 0))'
                            ) AS conversation_check
                        """
                    )
                )
            ).one()
        assert [row.message_id for row in generations] == message_ids
        assert [row.decision_generation for row in generations] == [2, 3, 4, 5, 6, 7]
        assert [row.status for row in generations] == [
            "SUPERSEDED",
            "SUPERSEDED",
            "SUPERSEDED",
            "SUPERSEDED",
            "SUPERSEDED",
            "PENDING",
        ]
        assert message_generations[orphan_message_id] == 1
        assert message_generations[private_message_id] is None
        assert [message_generations[message_id] for message_id in message_ids] == [
            2,
            3,
            4,
            5,
            6,
            7,
        ]
        assert conversation_generation == 7
        assert tuple(schema) == ("YES", "YES", True, True)
        assert [outbox_statuses[outbox_id] for outbox_id in outbox_ids] == [
            expected for *_case, expected in cases
        ]
        with pytest.raises(DBAPIError, match="ck_conversations_decision_generation"):
            async with engine.begin() as connection:
                await connection.execute(
                    text("UPDATE conversations SET decision_generation=-1 WHERE id=:id"),
                    {"id": _CONVERSATION_ID},
                )

        explicit_message_id = uuid.uuid4()
        agent_message_id = uuid.uuid4()
        outgoing_message_id = uuid.uuid4()
        private_legacy_message_id = uuid.uuid4()
        legacy_message_id = uuid.uuid4()
        legacy_job_id = uuid.uuid4()
        accepted_outbox_id = uuid.uuid4()
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO messages (
                        id, conversation_id, direction, sender_type, text,
                        reply_target, attachments, private, decision_generation
                    ) VALUES (
                        :explicit_id, :conversation_id, 'inbound', 'contact', 'explicit',
                        '{}'::jsonb, '[]'::jsonb, false, 7
                    ), (
                        :agent_id, :conversation_id, 'inbound', 'agent', 'agent',
                        '{}'::jsonb, '[]'::jsonb, false, NULL
                    ), (
                        :outgoing_id, :conversation_id, 'outbound', 'contact', 'outgoing',
                        '{}'::jsonb, '[]'::jsonb, false, NULL
                    ), (
                        :private_id, :conversation_id, 'inbound', 'contact', 'private',
                        '{}'::jsonb, '[]'::jsonb, true, NULL
                    )
                    """
                ),
                {
                    "explicit_id": explicit_message_id,
                    "agent_id": agent_message_id,
                    "outgoing_id": outgoing_message_id,
                    "private_id": private_legacy_message_id,
                    "conversation_id": _CONVERSATION_ID,
                },
            )
            unchanged_generation = (
                await connection.execute(
                    text("SELECT decision_generation FROM conversations WHERE id=:id"),
                    {"id": _CONVERSATION_ID},
                )
            ).scalar_one()
            assert unchanged_generation == 7
            await _insert_message(connection, legacy_message_id, "legacy-current")
            await _insert_legacy_job(
                connection, legacy_job_id, legacy_message_id, status="PROCESSING"
            )
            await _insert_outbox(
                connection,
                accepted_outbox_id,
                origin="DECISION",
                actor="BOT",
                status="PENDING",
                suffix="accepted",
            )
            await _insert_legacy_decision(
                connection,
                uuid.uuid4(),
                legacy_message_id,
                accepted_outbox_id,
            )
        async with engine.connect() as connection:
            explicit_and_outgoing = dict(
                (
                    await connection.execute(
                        text(
                            """
                            SELECT id, decision_generation
                            FROM messages
                            WHERE id = ANY(:ids)
                            """
                        ),
                        {
                            "ids": [
                                explicit_message_id,
                                agent_message_id,
                                outgoing_message_id,
                                private_legacy_message_id,
                            ]
                        },
                    )
                ).all()
            )
            legacy_row = (
                await connection.execute(
                    text(
                        """
                        SELECT message.history_seq, job.decision_generation,
                               decision.decision_job_id, decision.decision_generation
                        FROM messages AS message
                        JOIN decision_jobs AS job ON job.message_id = message.id
                        JOIN reply_decisions AS decision ON decision.message_id = message.id
                        WHERE message.id=:message_id
                        """
                    ),
                    {"message_id": legacy_message_id},
                )
            ).one()
        assert explicit_and_outgoing == {
            explicit_message_id: 7,
            agent_message_id: None,
            outgoing_message_id: None,
            private_legacy_message_id: None,
        }
        assert legacy_row.history_seq > 8
        assert legacy_row.decision_generation == 8
        assert legacy_row.decision_job_id == legacy_job_id
        assert legacy_row[3] == 8

        mismatched_conversation_id = uuid.uuid4()
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO conversations (
                        id, tenant_id, brand_id, platform, platform_account_id,
                        contact_id, conversation_key, channel_type
                    ) VALUES (
                        :id, 'default', 'b1', 'telegram', :account_id,
                        :contact_id, 'migration:generation:mismatch', 'dm'
                    )
                    """
                ),
                {
                    "id": mismatched_conversation_id,
                    "account_id": _ACCOUNT_ID,
                    "contact_id": _CONTACT_ID,
                },
            )
        with pytest.raises(DBAPIError, match="decision_job_message_scope_mismatch"):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        INSERT INTO decision_jobs (
                            id, conversation_id, message_id, account_id, snapshot,
                            status, attempt_count
                        ) VALUES (
                            :id, :conversation_id, :message_id, :account_id,
                            '{}'::jsonb, 'PENDING', 0
                        )
                        """
                    ),
                    {
                        "id": uuid.uuid4(),
                        "conversation_id": mismatched_conversation_id,
                        "message_id": legacy_message_id,
                        "account_id": _ACCOUNT_ID,
                    },
                )

        newer_message_id = uuid.uuid4()
        newer_job_id = uuid.uuid4()
        async with engine.begin() as connection:
            await _insert_message(connection, newer_message_id, "legacy-newer")
            await _insert_legacy_job(
                connection, newer_job_id, newer_message_id, status="PROCESSING"
            )
        async with engine.connect() as connection:
            accepted_outbox = (
                await connection.execute(
                    text("SELECT status, last_error_code FROM outbox_messages WHERE id=:id"),
                    {"id": accepted_outbox_id},
                )
            ).one()
        assert tuple(accepted_outbox) == ("CANCELLED", "STALE_CONVERSATION_INPUT")

        rejected_outbox_id = uuid.uuid4()
        with pytest.raises(DBAPIError, match="decision_job_not_processing"):
            async with engine.begin() as connection:
                await _insert_outbox(
                    connection,
                    rejected_outbox_id,
                    origin="DECISION",
                    actor="BOT",
                    status="PENDING",
                    suffix="stale-rollback",
                )
                await _insert_legacy_decision(
                    connection,
                    uuid.uuid4(),
                    legacy_message_id,
                    rejected_outbox_id,
                )
        async with engine.connect() as connection:
            assert (
                await connection.execute(
                    text("SELECT count(*) FROM outbox_messages WHERE id=:id"),
                    {"id": rejected_outbox_id},
                )
            ).scalar_one() == 0

        mismatch_outbox_id = uuid.uuid4()
        with pytest.raises(DBAPIError, match="decision_job_generation_mismatch"):
            async with engine.begin() as connection:
                await _insert_outbox(
                    connection,
                    mismatch_outbox_id,
                    origin="DECISION",
                    actor="BOT",
                    status="PENDING",
                    suffix="mismatch-rollback",
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO reply_decisions (
                            id, tenant_id, conversation_id, message_id, action,
                            risk_level, confidence, reply_text, reply_visibility,
                            reason_codes, source, outbox_id,
                            decision_job_id, decision_generation
                        ) VALUES (
                            :id, 'default', :conversation_id, :message_id,
                            'auto_reply', 'low', 0.8, 'reply', 'public',
                            '[]'::jsonb, 'llm', :outbox_id, :job_id, 7
                        )
                        """
                    ),
                    {
                        "id": uuid.uuid4(),
                        "conversation_id": _CONVERSATION_ID,
                        "message_id": newer_message_id,
                        "outbox_id": mismatch_outbox_id,
                        "job_id": newer_job_id,
                    },
                )
        async with engine.connect() as connection:
            assert (
                await connection.execute(
                    text("SELECT count(*) FROM outbox_messages WHERE id=:id"),
                    {"id": mismatch_outbox_id},
                )
            ).scalar_one() == 0

        claimed_message_id = uuid.uuid4()
        claimed_job_id = uuid.uuid4()
        claimed_token = uuid.uuid4()
        claim_outbox_id = uuid.uuid4()
        async with engine.begin() as connection:
            await _insert_message(connection, claimed_message_id, "claimed")
            await _insert_legacy_job(
                connection, claimed_job_id, claimed_message_id, status="PROCESSING"
            )
            await connection.execute(
                text("UPDATE decision_jobs SET claim_token=:token WHERE id=:id"),
                {"token": claimed_token, "id": claimed_job_id},
            )
        with pytest.raises(DBAPIError, match="decision_job_claim_mismatch"):
            async with engine.begin() as connection:
                await _insert_outbox(
                    connection,
                    claim_outbox_id,
                    origin="DECISION",
                    actor="BOT",
                    status="PENDING",
                    suffix="claim-rollback",
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO reply_decisions (
                            id, tenant_id, conversation_id, message_id, action,
                            risk_level, confidence, reply_text, reply_visibility,
                            reason_codes, source, outbox_id, decision_job_id,
                            decision_claim_token
                        ) VALUES (
                            :id, 'default', :conversation_id, :message_id,
                            'auto_reply', 'low', 0.8, 'reply', 'public',
                            '[]'::jsonb, 'llm', :outbox_id, :job_id, :claim_token
                        )
                        """
                    ),
                    {
                        "id": uuid.uuid4(),
                        "conversation_id": _CONVERSATION_ID,
                        "message_id": claimed_message_id,
                        "outbox_id": claim_outbox_id,
                        "job_id": claimed_job_id,
                        "claim_token": uuid.uuid4(),
                    },
                )
        async with engine.connect() as connection:
            assert (
                await connection.execute(
                    text("SELECT count(*) FROM outbox_messages WHERE id=:id"),
                    {"id": claim_outbox_id},
                )
            ).scalar_one() == 0

        direct_message_id = uuid.uuid4()
        direct_outbox_id = uuid.uuid4()
        async with engine.begin() as connection:
            await _insert_message(connection, direct_message_id, "direct characterization")
            await _insert_outbox(
                connection,
                direct_outbox_id,
                origin="DECISION",
                actor="BOT",
                status="PENDING",
                suffix="direct-no-job",
            )
            await _insert_legacy_decision(
                connection,
                uuid.uuid4(),
                direct_message_id,
                direct_outbox_id,
            )
        async with engine.connect() as connection:
            direct_job_id = (
                await connection.execute(
                    text("SELECT decision_job_id FROM reply_decisions WHERE outbox_id=:id"),
                    {"id": direct_outbox_id},
                )
            ).scalar_one()
        assert direct_job_id is None

        await engine.dispose()
        await assert_alembic_succeeds(database_url, "downgrade", _BASE_REVISION)
        engine = create_async_engine(database_url)
        async with engine.connect() as connection:
            trigger_count = (
                await connection.execute(
                    text(
                        """
                        SELECT count(*) FROM pg_trigger
                        WHERE tgname IN (
                            'trg_reserve_message_decision_generation',
                            'trg_attach_decision_job_generation',
                            'trg_attach_reply_decision_generation'
                        ) AND NOT tgisinternal
                        """
                    )
                )
            ).scalar_one()
            function_count = (
                await connection.execute(
                    text(
                        """
                        SELECT count(*) FROM pg_proc
                        WHERE proname IN (
                            'reserve_message_decision_generation',
                            'attach_decision_job_generation',
                            'attach_reply_decision_generation'
                        )
                        """
                    )
                )
            ).scalar_one()
            remaining_columns = {
                row.column_name
                for row in (
                    await connection.execute(
                        text(
                            """
                            SELECT column_name FROM information_schema.columns
                            WHERE table_schema='public'
                              AND column_name IN (
                                  'decision_generation', 'claim_token', 'decision_job_id',
                                  'decision_claim_token'
                              )
                              AND table_name IN (
                                  'conversations', 'messages', 'decision_jobs', 'reply_decisions'
                              )
                            """
                        )
                    )
                ).all()
            }
            remaining_indexes = (
                await connection.execute(
                    text(
                        """
                        SELECT count(*) FROM pg_indexes
                        WHERE schemaname='public'
                          AND indexname IN (
                            'ix_messages_conversation_decision_generation',
                            'ix_decision_jobs_conversation_generation',
                            'ix_reply_decisions_decision_job_id'
                          )
                        """
                    )
                )
            ).scalar_one()
            remaining_check = (
                await connection.execute(
                    text(
                        """
                        SELECT count(*) FROM pg_constraint
                        WHERE conname='ck_conversations_decision_generation'
                        """
                    )
                )
            ).scalar_one()
        await engine.dispose()
        assert trigger_count == 0
        assert function_count == 0
        assert remaining_columns == set()
        assert remaining_indexes == 0
        assert remaining_check == 0
    finally:
        await _drop_database(database_name, admin_engine)


async def test_reply_decision_trigger_rejects_stale_overlapping_transaction():
    database_name, database_url, admin_engine = await _create_database(
        "social_reply_decision_commit_race"
    )
    try:
        await assert_alembic_succeeds(database_url, "upgrade", _FENCING_REVISION)
        engine = create_async_engine(database_url)
        old_message_id = uuid.uuid4()
        new_message_id = uuid.uuid4()
        old_job_id = uuid.uuid4()
        new_job_id = uuid.uuid4()
        old_claim_token = uuid.uuid4()
        outbox_id = uuid.uuid4()
        async with engine.begin() as connection:
            await _seed_scope(connection)
            await _insert_message(connection, old_message_id, "old")
            await connection.execute(
                text(
                    """
                    INSERT INTO decision_jobs (
                        id, conversation_id, message_id, account_id, snapshot,
                        status, attempt_count, claim_token
                    ) VALUES (
                        :job_id, :conversation_id, :message_id, :account_id,
                        '{}'::jsonb, 'PROCESSING', 1, :claim_token
                    )
                    """
                ),
                {
                    "job_id": old_job_id,
                    "conversation_id": _CONVERSATION_ID,
                    "message_id": old_message_id,
                    "account_id": _ACCOUNT_ID,
                    "claim_token": old_claim_token,
                },
            )
            await _insert_message(connection, new_message_id, "new")

        old_connection = await engine.connect()
        newer_connection = await engine.connect()
        try:
            await old_connection.begin()
            await _insert_outbox(
                old_connection,
                outbox_id,
                origin="DECISION",
                actor="BOT",
                status="PENDING",
                suffix="overlap-stale",
            )

            await newer_connection.begin()
            await _insert_current_job(
                newer_connection,
                job_id=new_job_id,
                message_id=new_message_id,
            )

            stale_insert = asyncio.create_task(
                old_connection.execute(
                    text(
                        """
                        INSERT INTO reply_decisions (
                            id, tenant_id, conversation_id, message_id, action,
                            risk_level, confidence, reply_text, reply_visibility,
                            reason_codes, source, outbox_id, decision_job_id,
                            decision_generation, decision_claim_token
                        ) VALUES (
                            :id, 'default', :conversation_id, :message_id,
                            'auto_reply', 'low', 0.8, 'stale', 'public',
                            '[]'::jsonb, 'llm', :outbox_id, :job_id, 1, :claim_token
                        )
                        """
                    ),
                    {
                        "id": uuid.uuid4(),
                        "conversation_id": _CONVERSATION_ID,
                        "message_id": old_message_id,
                        "outbox_id": outbox_id,
                        "job_id": old_job_id,
                        "claim_token": old_claim_token,
                    },
                )
            )
            await asyncio.sleep(0.1)
            assert not stale_insert.done()
            await newer_connection.commit()
            with pytest.raises(DBAPIError, match="decision_job_not_processing"):
                await stale_insert
            await old_connection.rollback()
        finally:
            if old_connection.in_transaction():
                await old_connection.rollback()
            if newer_connection.in_transaction():
                await newer_connection.rollback()
            await old_connection.close()
            await newer_connection.close()

        async with engine.connect() as connection:
            stale_counts = (
                await connection.execute(
                    text(
                        """
                        SELECT
                            (SELECT count(*) FROM reply_decisions WHERE decision_job_id=:job_id),
                            (SELECT count(*) FROM outbox_messages WHERE id=:outbox_id)
                        """
                    ),
                    {"job_id": old_job_id, "outbox_id": outbox_id},
                )
            ).one()
            old_status = (
                await connection.execute(
                    text("SELECT status FROM decision_jobs WHERE id=:id"),
                    {"id": old_job_id},
                )
            ).scalar_one()
            new_generation = (
                await connection.execute(
                    text("SELECT decision_generation FROM decision_jobs WHERE id=:id"),
                    {"id": new_job_id},
                )
            ).scalar_one()
        await engine.dispose()
        assert tuple(stale_counts) == (0, 0)
        assert old_status == "SUPERSEDED"
        assert new_generation == 2
    finally:
        await _drop_database(database_name, admin_engine)


async def _insert_current_job(connection, *, job_id: uuid.UUID, message_id: uuid.UUID) -> None:
    await connection.execute(
        text(
            """
            INSERT INTO decision_jobs (
                id, conversation_id, message_id, account_id, snapshot,
                status, attempt_count
            ) VALUES (
                :job_id, :conversation_id, :message_id, :account_id,
                '{}'::jsonb, 'PENDING', 0
            )
            """
        ),
        {
            "job_id": job_id,
            "conversation_id": _CONVERSATION_ID,
            "message_id": message_id,
            "account_id": _ACCOUNT_ID,
        },
    )


async def test_migration_cancellation_waits_for_delivery_advisory_lock():
    database_name, database_url, admin_engine = await _create_database(
        "social_reply_generation_race"
    )
    try:
        await assert_alembic_succeeds(database_url, "upgrade", _BASE_REVISION)
        engine = create_async_engine(database_url)
        old_message_id, new_message_id = uuid.uuid4(), uuid.uuid4()
        old_job_id, new_job_id = uuid.uuid4(), uuid.uuid4()
        outbox_id = uuid.uuid4()
        async with engine.begin() as connection:
            await _seed_scope(connection)
            await _insert_message(connection, old_message_id, "old")
            await _insert_message(connection, new_message_id, "new")
            await _insert_legacy_job(connection, old_job_id, old_message_id)
            await _insert_legacy_job(connection, new_job_id, new_message_id)
            await _insert_outbox(
                connection,
                outbox_id,
                origin="DECISION",
                actor="BOT",
                status="PENDING",
                suffix="race",
            )
            await _insert_legacy_decision(connection, uuid.uuid4(), old_message_id, outbox_id)

        lock_connection = await engine.connect()
        key = f"social-reply:conversation-delivery:{_CONVERSATION_ID}"
        await lock_connection.execute(
            text("SELECT pg_advisory_lock(hashtextextended(:key, 0))"),
            {"key": key},
        )
        await lock_connection.commit()
        migration = asyncio.create_task(
            assert_alembic_succeeds(database_url, "upgrade", _FENCING_REVISION)
        )
        await asyncio.sleep(0.25)
        assert not migration.done()

        await lock_connection.execute(
            text("UPDATE outbox_messages SET status='SENT' WHERE id=:id"),
            {"id": outbox_id},
        )
        await lock_connection.commit()
        await lock_connection.execute(
            text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))"),
            {"key": key},
        )
        await lock_connection.commit()
        await lock_connection.close()
        await asyncio.wait_for(migration, timeout=10)

        async with engine.connect() as connection:
            status = (
                await connection.execute(
                    text("SELECT status FROM outbox_messages WHERE id=:id"),
                    {"id": outbox_id},
                )
            ).scalar_one()
        await engine.dispose()
        assert status == "SENT"
    finally:
        await _drop_database(database_name, admin_engine)
