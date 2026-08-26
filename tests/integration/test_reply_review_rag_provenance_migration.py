import hashlib
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

from social_reply.domain.knowledge.policy import knowledge_revision_hash

pytestmark = pytest.mark.integration

_BASE_REVISION = "f2d9c4b8e631"
_HEAD_REVISION = "a7c3e9d1b624"
_ACCOUNT_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
_CONTACT_ID = uuid.UUID("10000000-0000-0000-0000-000000000002")
_CONVERSATION_ID = uuid.UUID("10000000-0000-0000-0000-000000000003")
_MESSAGE_IDS = tuple(
    uuid.UUID(f"20000000-0000-0000-0000-{number:012d}") for number in range(1, 5)
)
_OUTBOX_IDS = tuple(
    uuid.UUID(f"30000000-0000-0000-0000-{number:012d}") for number in range(1, 5)
)
_DECISION_IDS = tuple(
    uuid.UUID(f"40000000-0000-0000-0000-{number:012d}") for number in range(1, 5)
)
_DOCUMENT_ROWS = (
    (
        "50000000-0000-0000-0000-000000000001",
        "Does WikiFX support this?",
        "Use Meta with OpenAI.",
    ),
    (
        "50000000-0000-0000-0000-000000000002",
        "Can I use Telegram?",
        "Use the approved channel.",
    ),
    (
        "50000000-0000-0000-0000-000000000003",
        "lower case",
        "wikifx is lower case",
    ),
    (
        "50000000-0000-0000-0000-000000000004",
        "duplicate mention",
        "Telegram then Telegram.",
    ),
)


def _knowledge_content(question: str, reply: str) -> str:
    return f"问：{question}\n答：{reply}"


async def _seed_legacy_rows(connection) -> None:
    await connection.execute(
        text(
            """
            INSERT INTO platform_accounts (
                id, tenant_id, brand_id, platform, name, config, capability,
                config_version, automation_default, status
            ) VALUES (
                :account_id, 'default', 'brand-a', 'telegram', 'migration-account',
                '{}'::jsonb, '{"dm": true, "max_text_length": 4096}'::jsonb,
                1, 'BOT_DRAFT_ONLY', 'active'
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
            ) VALUES (:contact_id, 'default', 'telegram', :account_id, 'migration-user')
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
                :conversation_id, 'default', 'brand-a', 'telegram', :account_id,
                :contact_id, 'migration:reply-review-rag', 'dm'
            )
            """
        ),
        {
            "conversation_id": _CONVERSATION_ID,
            "account_id": _ACCOUNT_ID,
            "contact_id": _CONTACT_ID,
        },
    )
    await connection.execute(
        text(
            """
            INSERT INTO messages (
                id, conversation_id, direction, sender_type, text,
                reply_target, attachments, private
            )
            SELECT message_id, :conversation_id, 'inbound', 'contact', label,
                   '{}'::jsonb, '[]'::jsonb, false
            FROM unnest(
                CAST(:message_ids AS uuid[]),
                CAST(:labels AS text[])
            ) AS rows(message_id, label)
            """
        ),
        {
            "conversation_id": _CONVERSATION_ID,
            "message_ids": list(_MESSAGE_IDS),
            "labels": ["approval", "bot", "untrusted-approval", "manual"],
        },
    )
    await connection.execute(
        text(
            """
            INSERT INTO outbox_messages (
                id, tenant_id, conversation_id, platform_account_id,
                destination_type, destination_id, message_type, payload,
                origin_kind, actor_kind, idempotency_key, status, attempt_count
            ) VALUES
                (:approval_id, 'default', :conversation_id, :account_id,
                 'telegram_dm', 'user-1', 'text', '{"text":"approved"}'::jsonb,
                 'DRAFT_APPROVAL', 'ADMIN_HUMAN', 'review-rag-approval', 'PENDING', 0),
                (:bot_id, 'default', :conversation_id, :account_id,
                 'telegram_dm', 'user-1', 'text', '{"text":"bot"}'::jsonb,
                 'DECISION', 'BOT', 'review-rag-bot', 'PENDING', 0),
                (:untrusted_id, 'default', :conversation_id, :account_id,
                 'telegram_dm', 'user-1', 'text', '{"text":"untrusted"}'::jsonb,
                 'DRAFT_APPROVAL', 'BOT', 'review-rag-untrusted', 'PENDING', 0),
                (:manual_id, 'default', :conversation_id, :account_id,
                 'telegram_dm', 'user-1', 'text', '{"text":"manual"}'::jsonb,
                 'MANUAL_REPLY', 'ADMIN_HUMAN', 'review-rag-manual', 'PENDING', 0)
            """
        ),
        {
            "approval_id": _OUTBOX_IDS[0],
            "bot_id": _OUTBOX_IDS[1],
            "untrusted_id": _OUTBOX_IDS[2],
            "manual_id": _OUTBOX_IDS[3],
            "conversation_id": _CONVERSATION_ID,
            "account_id": _ACCOUNT_ID,
        },
    )
    await connection.execute(
        text(
            """
            INSERT INTO reply_decisions (
                id, tenant_id, conversation_id, message_id, action,
                risk_level, confidence, reply_text, reply_visibility,
                reason_codes, source, outbox_id
            )
            SELECT decision_id, 'default', :conversation_id, message_id, action,
                   'low', 0.8, 'reply', 'public', '[]'::jsonb, 'llm', outbox_id
            FROM unnest(
                CAST(:decision_ids AS uuid[]),
                CAST(:message_ids AS uuid[]),
                CAST(:actions AS text[]),
                CAST(:outbox_ids AS uuid[])
            ) AS rows(decision_id, message_id, action, outbox_id)
            """
        ),
        {
            "decision_ids": list(_DECISION_IDS),
            "message_ids": list(_MESSAGE_IDS),
            "actions": ["draft", "auto_reply", "draft", "draft"],
            "outbox_ids": list(_OUTBOX_IDS),
            "conversation_id": _CONVERSATION_ID,
        },
    )
    await connection.execute(
        text(
            """
            INSERT INTO knowledge_documents (
                id, tenant_id, brand_id, question, reply, status
            ) VALUES
                ('50000000-0000-0000-0000-000000000001', 'default', 'brand-a',
                 'Does WikiFX support this?', 'Use Meta with OpenAI.', 'published'),
                ('50000000-0000-0000-0000-000000000002', 'default', 'brand-a',
                 'Can I use Telegram?', 'Use the approved channel.', 'published'),
                ('50000000-0000-0000-0000-000000000003', 'default', 'brand-a',
                 'lower case', 'wikifx is lower case', 'published'),
                ('50000000-0000-0000-0000-000000000004', 'default', 'brand-a',
                 'duplicate mention', 'Telegram then Telegram.', 'published')
            """
        )
    )
    for index, (document_id, question, reply) in enumerate(_DOCUMENT_ROWS, start=1):
        content = _knowledge_content(question, reply)
        await connection.execute(
            text(
                """
                INSERT INTO knowledge_chunks (
                    id, tenant_id, document_id, content, embed_text, content_hash,
                    embedding, embedding_version
                ) VALUES (
                    CAST(:chunk_id AS uuid), 'default', CAST(:document_id AS uuid),
                    :content, :question, :content_hash,
                    ('[1,' || repeat('0,', 1534) || '0]')::vector,
                    'migration-test-v1'
                )
                """
            ),
            {
                "chunk_id": f"60000000-0000-0000-0000-{index:012d}",
                "document_id": document_id,
                "content": content,
                "question": question,
                "content_hash": hashlib.sha256(content.encode()).hexdigest(),
            },
        )


async def test_reply_review_rag_provenance_upgrade_constraints_and_downgrade() -> None:
    async with temporary_database("social_reply_review_rag") as database_url:
        await assert_alembic_succeeds(database_url, "upgrade", _BASE_REVISION)
        engine = create_async_engine(database_url)
        async with engine.begin() as connection:
            await _seed_legacy_rows(connection)
        await engine.dispose()

        await assert_alembic_succeeds(database_url, "upgrade", _HEAD_REVISION)
        engine = create_async_engine(database_url)
        async with engine.connect() as connection:
            decisions = (
                await connection.execute(
                    text(
                        "SELECT id, outbox_id, review_outbox_id "
                        "FROM reply_decisions ORDER BY id"
                    )
                )
            ).all()
            protected_values = (
                await connection.execute(
                    text("SELECT protected_values FROM knowledge_documents ORDER BY id")
                )
            ).scalars().all()
            content_hashes = (
                await connection.execute(
                    text(
                        "SELECT chunk.content_hash "
                        "FROM knowledge_chunks AS chunk "
                        "ORDER BY chunk.document_id"
                    )
                )
            ).scalars().all()
            constraints = {
                row[0]
                for row in await connection.execute(
                    text(
                        "SELECT conname FROM pg_constraint "
                        "WHERE conrelid IN ("
                        "'reply_decisions'::regclass, 'knowledge_documents'::regclass)"
                    )
                )
            }

        assert decisions == [
            (_DECISION_IDS[0], None, _OUTBOX_IDS[0]),
            (_DECISION_IDS[1], _OUTBOX_IDS[1], None),
            (_DECISION_IDS[2], _OUTBOX_IDS[2], None),
            (_DECISION_IDS[3], _OUTBOX_IDS[3], None),
        ]
        assert protected_values == [
            ["Meta", "OpenAI"],
            [],
            [],
            ["Telegram"],
        ]
        assert content_hashes == [
            knowledge_revision_hash(
                _knowledge_content(question, reply),
                protected_values[index],
            )
            for index, (_document_id, question, reply) in enumerate(_DOCUMENT_ROWS)
        ]
        assert {
            "ck_reply_decisions_rag_evidence_object",
            "ck_knowledge_documents_protected_values_array",
            "fk_reply_decisions_review_outbox_id",
            "uq_reply_decisions_review_outbox_id",
        } <= constraints

        for invalid_json in ("[]", '"text"', "null"):
            with pytest.raises(IntegrityError):
                async with engine.begin() as connection:
                    await connection.execute(
                        text(
                            "UPDATE reply_decisions SET rag_evidence=CAST(:value AS jsonb) "
                            "WHERE id=:id"
                        ),
                        {"value": invalid_json, "id": _DECISION_IDS[0]},
                    )

        for invalid_json in ("{}", '"text"', "null"):
            with pytest.raises(IntegrityError):
                async with engine.begin() as connection:
                    await connection.execute(
                        text(
                            "UPDATE knowledge_documents "
                            "SET protected_values=CAST(:value AS jsonb) "
                            "WHERE id='50000000-0000-0000-0000-000000000001'"
                        ),
                        {"value": invalid_json},
                    )

        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE reply_decisions SET rag_evidence='{}'::jsonb WHERE id=:id"),
                {"id": _DECISION_IDS[0]},
            )
            await connection.execute(
                text(
                    "UPDATE knowledge_documents SET protected_values='[\"Acme\"]'::jsonb "
                    "WHERE id='50000000-0000-0000-0000-000000000001'"
                )
            )

        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    text("UPDATE reply_decisions SET review_outbox_id=:outbox_id WHERE id=:id"),
                    {"outbox_id": _OUTBOX_IDS[0], "id": _DECISION_IDS[1]},
                )

        # The predecessor cannot represent both the private-note and approval links. A
        # downgrade must stop before dropping schema or provenance in that state.
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE reply_decisions SET review_outbox_id=outbox_id WHERE id=:id"),
                {"id": _DECISION_IDS[1]},
            )
        await engine.dispose()

        failed = await run_alembic(database_url, "downgrade", _BASE_REVISION)
        assert failed.returncode != 0
        assert (
            "cannot downgrade reply review provenance with dual outbox links"
            in failed.stdout + failed.stderr
        )

        engine = create_async_engine(database_url)
        async with engine.begin() as connection:
            assert (
                await connection.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one() == _HEAD_REVISION
            await connection.execute(
                text("UPDATE reply_decisions SET review_outbox_id=NULL WHERE id=:id"),
                {"id": _DECISION_IDS[1]},
            )
        await engine.dispose()

        failed = await run_alembic(database_url, "downgrade", _BASE_REVISION)
        assert failed.returncode != 0
        assert (
            "cannot downgrade knowledge policy while protected values are present"
            in failed.stdout + failed.stderr
        )

        engine = create_async_engine(database_url)
        async with engine.begin() as connection:
            assert (
                await connection.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one() == _HEAD_REVISION
            await connection.execute(
                text("UPDATE knowledge_documents SET protected_values='[]'::jsonb")
            )
        await engine.dispose()

        await assert_alembic_succeeds(database_url, "downgrade", _BASE_REVISION)
        engine = create_async_engine(database_url)
        async with engine.connect() as connection:
            columns = {
                row[0]
                for row in await connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name IN ('reply_decisions', 'knowledge_documents')"
                    )
                )
            }
            downgraded_links = (
                await connection.execute(
                    text("SELECT id, outbox_id FROM reply_decisions ORDER BY id")
                )
            ).all()
            approval_outbox = (
                await connection.execute(
                    text(
                        "SELECT origin_kind, actor_kind FROM outbox_messages "
                        "WHERE id=:outbox_id"
                    ),
                    {"outbox_id": _OUTBOX_IDS[0]},
                )
            ).one()
            downgraded_hashes = (
                await connection.execute(
                    text("SELECT content_hash FROM knowledge_chunks ORDER BY document_id")
                )
            ).scalars().all()
        await engine.dispose()

        assert {
            "review_outbox_id",
            "decision_release_sha",
            "retrieval_policy_version",
            "selector_version",
            "rag_evidence",
            "protected_values",
        }.isdisjoint(columns)
        assert downgraded_links == list(zip(_DECISION_IDS, _OUTBOX_IDS, strict=True))
        assert approval_outbox == ("DRAFT_APPROVAL", "ADMIN_HUMAN")
        assert downgraded_hashes == [
            hashlib.sha256(_knowledge_content(question, reply).encode()).hexdigest()
            for _document_id, question, reply in _DOCUMENT_ROWS
        ]

        await assert_alembic_succeeds(database_url, "upgrade", _HEAD_REVISION)
        engine = create_async_engine(database_url)
        async with engine.connect() as connection:
            reupgraded_link = (
                await connection.execute(
                    text(
                        "SELECT outbox_id, review_outbox_id FROM reply_decisions "
                        "WHERE id=:id"
                    ),
                    {"id": _DECISION_IDS[0]},
                )
            ).one()
        await engine.dispose()
        assert reupgraded_link == (None, _OUTBOX_IDS[0])
