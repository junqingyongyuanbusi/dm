"""message history context

Revision ID: f3a6c1d8e250
Revises: e7b2c4d9a610
"""

import sqlalchemy as sa
from alembic import op

revision = "f3a6c1d8e250"
down_revision = "e7b2c4d9a610"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Keep ingestion and delivery from changing the two timelines while they are
    # merged and assigned one durable order.
    op.execute("LOCK TABLE messages IN ACCESS EXCLUSIVE MODE")
    op.execute("LOCK TABLE outbox_messages IN SHARE ROW EXCLUSIVE MODE")

    op.add_column(
        "messages",
        sa.Column("source_outbox_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_messages_source_outbox_id",
        "messages",
        "outbox_messages",
        ["source_outbox_id"],
        ["id"],
    )
    op.create_unique_constraint(
        "uq_messages_source_outbox_id", "messages", ["source_outbox_id"]
    )

    # Older bot echoes were intentionally ignored. Materialize only commands
    # confirmed SENT, and avoid rows already represented by a platform echo.
    op.execute(
        """
        INSERT INTO messages (
            id,
            conversation_id,
            direction,
            sender_type,
            text,
            chatwoot_message_id,
            platform_message_id,
            source_outbox_id,
            reply_target,
            private,
            occurred_at,
            created_at
        )
        SELECT
            gen_random_uuid(),
            o.conversation_id,
            'outbound',
            CASE WHEN o.payload ->> 'approval' = 'admin' THEN 'agent' ELSE 'bot' END,
            o.payload ->> 'text',
            o.chatwoot_message_id,
            o.platform_message_id,
            o.id,
            CASE
                WHEN jsonb_typeof(o.payload -> 'target') = 'object'
                    THEN o.payload -> 'target'
                ELSE '{}'::jsonb
            END,
            false,
            COALESCE(o.sent_at, o.created_at),
            COALESCE(o.sent_at, o.created_at)
        FROM outbox_messages AS o
        WHERE o.status = 'SENT'
          AND o.message_type = 'text'
          AND NULLIF(BTRIM(o.payload ->> 'text'), '') IS NOT NULL
          AND NOT EXISTS (
              SELECT 1
              FROM messages AS m
              WHERE m.conversation_id = o.conversation_id
                AND (
                    m.source_outbox_id = o.id
                    OR (
                        o.chatwoot_message_id IS NOT NULL
                        AND m.chatwoot_message_id = o.chatwoot_message_id
                    )
                    OR (
                        o.platform_message_id IS NOT NULL
                        AND m.platform_message_id = o.platform_message_id
                    )
                )
          )
        """
    )

    op.execute("CREATE SEQUENCE messages_history_seq_seq")
    op.add_column(
        "messages",
        sa.Column("history_seq", sa.BigInteger(), nullable=True),
    )
    op.execute(
        """
        WITH ordered AS (
            SELECT
                id,
                ROW_NUMBER() OVER (
                    ORDER BY COALESCE(occurred_at, created_at), created_at, id
                ) AS seq
            FROM messages
        )
        UPDATE messages AS m
        SET history_seq = ordered.seq
        FROM ordered
        WHERE m.id = ordered.id
        """
    )
    op.execute(
        """
        SELECT setval(
            'messages_history_seq_seq',
            GREATEST(COALESCE((SELECT MAX(history_seq) FROM messages), 0) + 1, 1),
            false
        )
        """
    )
    op.execute(
        "ALTER SEQUENCE messages_history_seq_seq OWNED BY messages.history_seq"
    )
    op.alter_column(
        "messages",
        "history_seq",
        existing_type=sa.BigInteger(),
        nullable=False,
        server_default=sa.text("nextval('messages_history_seq_seq'::regclass)"),
    )
    op.create_unique_constraint(
        "uq_messages_history_seq", "messages", ["history_seq"]
    )
    op.create_index(
        "ix_messages_conversation_history",
        "messages",
        ["conversation_id", "history_seq"],
    )


def downgrade() -> None:
    op.drop_index("ix_messages_conversation_history", table_name="messages")
    op.drop_constraint("uq_messages_history_seq", "messages", type_="unique")
    op.drop_column("messages", "history_seq")
    op.execute("DROP SEQUENCE IF EXISTS messages_history_seq_seq")

    op.execute("DELETE FROM messages WHERE source_outbox_id IS NOT NULL")
    op.drop_constraint(
        "uq_messages_source_outbox_id", "messages", type_="unique"
    )
    op.drop_constraint(
        "fk_messages_source_outbox_id", "messages", type_="foreignkey"
    )
    op.drop_column("messages", "source_outbox_id")
