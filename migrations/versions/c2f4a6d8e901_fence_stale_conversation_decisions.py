"""fence stale conversation decisions

Revision ID: c2f4a6d8e901
Revises: b8e1d4f7a2c3
"""

import sqlalchemy as sa
from alembic import op


revision = "c2f4a6d8e901"
down_revision = "b8e1d4f7a2c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("decision_generation", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.create_check_constraint(
        "ck_conversations_decision_generation",
        "conversations",
        "decision_generation >= 0",
    )
    op.add_column("messages", sa.Column("decision_generation", sa.BigInteger(), nullable=True))
    op.add_column(
        "decision_jobs",
        sa.Column("decision_generation", sa.BigInteger(), nullable=True),
    )
    op.add_column("decision_jobs", sa.Column("claim_token", sa.UUID(), nullable=True))
    op.add_column("reply_decisions", sa.Column("decision_job_id", sa.UUID(), nullable=True))
    op.add_column(
        "reply_decisions", sa.Column("decision_generation", sa.BigInteger(), nullable=True)
    )
    op.add_column("reply_decisions", sa.Column("decision_claim_token", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_reply_decisions_decision_job_id",
        "reply_decisions",
        "decision_jobs",
        ["decision_job_id"],
        ["id"],
    )
    op.create_index(
        "ix_messages_conversation_decision_generation",
        "messages",
        ["conversation_id", "decision_generation"],
    )
    op.create_index(
        "ix_decision_jobs_conversation_generation",
        "decision_jobs",
        ["conversation_id", "decision_generation"],
    )
    op.create_index(
        "ix_reply_decisions_decision_job_id",
        "reply_decisions",
        ["decision_job_id"],
        unique=True,
        postgresql_where=sa.text("decision_job_id IS NOT NULL"),
    )

    # Serialize the backfill and cancellation with live delivery transactions.
    op.execute(
        sa.text(
            """
            SELECT pg_advisory_xact_lock(
                hashtextextended(
                    'social-reply:conversation-delivery:' || locked.conversation_id,
                    0
                )
            )
            FROM (
                SELECT conversation_id::text AS conversation_id
                FROM messages
                WHERE direction = 'inbound'
                  AND sender_type = 'contact'
                  AND private = false
                UNION
                SELECT conversation_id::text AS conversation_id
                FROM decision_jobs
                UNION
                SELECT decision.conversation_id::text AS conversation_id
                FROM reply_decisions AS decision
                JOIN outbox_messages AS outbox ON outbox.id = decision.outbox_id
                WHERE outbox.origin_kind = 'DECISION'
                  AND outbox.actor_kind = 'BOT'
                  AND outbox.status IN ('PENDING', 'FAILED')
                ORDER BY conversation_id
            ) AS locked
            """
        )
    )

    op.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT message.id,
                       row_number() OVER (
                           PARTITION BY message.conversation_id
                           ORDER BY message.history_seq
                       ) AS generation
                FROM messages AS message
                WHERE message.direction = 'inbound'
                  AND message.sender_type = 'contact'
                  AND message.private = false
            )
            UPDATE messages AS message
            SET decision_generation = ranked.generation
            FROM ranked
            WHERE ranked.id = message.id
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE conversations AS conversation
            SET decision_generation = generations.generation
            FROM (
                SELECT conversation_id, max(decision_generation) AS generation
                FROM messages
                WHERE decision_generation IS NOT NULL
                GROUP BY conversation_id
            ) AS generations
            WHERE generations.conversation_id = conversation.id
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE decision_jobs AS job
            SET decision_generation = message.decision_generation
            FROM messages AS message
            WHERE message.id = job.message_id
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE reply_decisions AS decision
            SET decision_job_id = job.id
            FROM decision_jobs AS job
            WHERE job.message_id = decision.message_id
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE reply_decisions AS decision
            SET decision_generation = coalesce(job.decision_generation, message.decision_generation)
            FROM messages AS message
            LEFT JOIN decision_jobs AS job ON job.message_id = message.id
            WHERE message.id = decision.message_id
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE decision_jobs AS job
            SET status = 'SUPERSEDED',
                next_attempt_at = NULL,
                locked_at = NULL,
                claim_token = NULL,
                completed_at = coalesce(completed_at, now()),
                last_error = 'superseded by migration backfill'
            FROM conversations AS conversation
            WHERE conversation.id = job.conversation_id
              AND job.decision_generation < conversation.decision_generation
              AND job.status IN ('PENDING', 'FAILED', 'PROCESSING', 'DEFERRED_CHATWOOT')
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE outbox_messages AS outbox
            SET status = 'CANCELLED',
                last_error_code = 'STALE_CONVERSATION_INPUT',
                last_error_message = 'superseded by a newer inbound message'
            FROM reply_decisions AS decision, conversations AS conversation
            WHERE decision.outbox_id = outbox.id
              AND conversation.id = decision.conversation_id
              AND decision.decision_generation < conversation.decision_generation
              AND outbox.origin_kind = 'DECISION'
              AND outbox.actor_kind = 'BOT'
              AND outbox.status IN ('PENDING', 'FAILED')
            """
        )
    )

    op.execute(
        sa.text(
            """
            CREATE FUNCTION reserve_message_decision_generation() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.direction = 'inbound'
                   AND NEW.sender_type = 'contact'
                   AND NEW.private = false
                   AND NEW.decision_generation IS NULL THEN
                    PERFORM pg_advisory_xact_lock(
                        hashtextextended(
                            'social-reply:conversation-delivery:' || NEW.conversation_id,
                            0
                        )
                    );

                    UPDATE conversations
                    SET decision_generation = decision_generation + 1
                    WHERE id = NEW.conversation_id
                    RETURNING decision_generation INTO NEW.decision_generation;

                    IF NEW.decision_generation IS NULL THEN
                        RAISE EXCEPTION 'decision_conversation_missing'
                            USING ERRCODE = '23503';
                    END IF;

                    UPDATE decision_jobs
                    SET status = 'SUPERSEDED',
                        next_attempt_at = NULL,
                        locked_at = NULL,
                        claim_token = NULL,
                        completed_at = coalesce(completed_at, now()),
                        last_error = 'superseded by a newer inbound message'
                    WHERE conversation_id = NEW.conversation_id
                      AND decision_generation < NEW.decision_generation
                      AND status IN ('PENDING', 'FAILED', 'PROCESSING', 'DEFERRED_CHATWOOT');

                    UPDATE outbox_messages AS outbox
                    SET status = 'CANCELLED',
                        last_error_code = 'STALE_CONVERSATION_INPUT',
                        last_error_message = 'superseded by a newer inbound message'
                    FROM reply_decisions AS decision
                    WHERE decision.outbox_id = outbox.id
                      AND decision.conversation_id = NEW.conversation_id
                      AND decision.decision_generation < NEW.decision_generation
                      AND outbox.origin_kind = 'DECISION'
                      AND outbox.actor_kind = 'BOT'
                      AND outbox.status IN ('PENDING', 'FAILED');
                END IF;
                RETURN NEW;
            END;
            $$
            """
        )
    )
    op.execute(
        "CREATE TRIGGER trg_reserve_message_decision_generation "
        "BEFORE INSERT ON messages FOR EACH ROW "
        "EXECUTE FUNCTION reserve_message_decision_generation()"
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION attach_decision_job_generation() RETURNS trigger
            LANGUAGE plpgsql AS $$
            DECLARE
                message_conversation_id uuid;
                message_account_id uuid;
                message_generation bigint;
            BEGIN
                PERFORM pg_advisory_xact_lock(
                    hashtextextended(
                        'social-reply:conversation-delivery:' || NEW.conversation_id,
                        0
                    )
                );

                SELECT message.conversation_id,
                       conversation.platform_account_id,
                       message.decision_generation
                INTO message_conversation_id, message_account_id, message_generation
                FROM messages AS message
                JOIN conversations AS conversation ON conversation.id = message.conversation_id
                WHERE message.id = NEW.message_id;

                IF NOT FOUND THEN
                    RAISE EXCEPTION 'decision_job_message_not_found'
                        USING ERRCODE = '23503';
                END IF;
                IF message_conversation_id IS DISTINCT FROM NEW.conversation_id
                   OR message_account_id IS DISTINCT FROM NEW.account_id THEN
                    RAISE EXCEPTION 'decision_job_message_scope_mismatch'
                        USING ERRCODE = '23514';
                END IF;
                IF message_generation IS NULL THEN
                    RAISE EXCEPTION 'decision_job_message_not_reply_eligible'
                        USING ERRCODE = '23514';
                END IF;
                IF NEW.decision_generation IS NULL THEN
                    NEW.decision_generation := message_generation;
                ELSIF NEW.decision_generation IS DISTINCT FROM message_generation THEN
                    RAISE EXCEPTION 'decision_job_generation_mismatch'
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END;
            $$
            """
        )
    )
    op.execute(
        "CREATE TRIGGER trg_attach_decision_job_generation "
        "BEFORE INSERT ON decision_jobs FOR EACH ROW "
        "EXECUTE FUNCTION attach_decision_job_generation()"
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION attach_reply_decision_generation() RETURNS trigger
            LANGUAGE plpgsql AS $$
            DECLARE
                job_conversation_id uuid;
                job_message_id uuid;
                job_generation bigint;
                job_status text;
                job_claim_token uuid;
                current_generation bigint;
            BEGIN
                PERFORM pg_advisory_xact_lock(
                    hashtextextended(
                        'social-reply:conversation-delivery:' || NEW.conversation_id,
                        0
                    )
                );

                SELECT decision_generation
                INTO current_generation
                FROM conversations
                WHERE id = NEW.conversation_id
                FOR UPDATE;

                IF NEW.decision_job_id IS NULL AND NEW.message_id IS NOT NULL THEN
                    SELECT id
                    INTO NEW.decision_job_id
                    FROM decision_jobs
                    WHERE message_id = NEW.message_id;
                END IF;

                IF NEW.decision_job_id IS NOT NULL THEN
                    SELECT conversation_id, message_id, decision_generation, status, claim_token
                    INTO job_conversation_id, job_message_id, job_generation,
                         job_status, job_claim_token
                    FROM decision_jobs
                    WHERE id = NEW.decision_job_id
                    FOR UPDATE;

                    IF NOT FOUND THEN
                        RAISE EXCEPTION 'decision_job_not_found'
                            USING ERRCODE = '23503';
                    END IF;
                    IF job_conversation_id IS DISTINCT FROM NEW.conversation_id
                       OR job_message_id IS DISTINCT FROM NEW.message_id THEN
                        RAISE EXCEPTION 'decision_job_scope_mismatch'
                            USING ERRCODE = '23514';
                    END IF;
                    IF job_status IS DISTINCT FROM 'PROCESSING' THEN
                        RAISE EXCEPTION 'decision_job_not_processing'
                            USING ERRCODE = '40001';
                    END IF;
                    IF NEW.decision_generation IS NULL THEN
                        NEW.decision_generation := job_generation;
                    ELSIF NEW.decision_generation IS DISTINCT FROM job_generation THEN
                        RAISE EXCEPTION 'decision_job_generation_mismatch'
                            USING ERRCODE = '40001';
                    END IF;
                    IF job_generation IS DISTINCT FROM current_generation THEN
                        RAISE EXCEPTION 'stale_decision_generation'
                            USING ERRCODE = '40001';
                    END IF;
                    IF job_claim_token IS NOT NULL
                       AND NEW.decision_claim_token IS DISTINCT FROM job_claim_token THEN
                        RAISE EXCEPTION 'decision_job_claim_mismatch'
                            USING ERRCODE = '40001';
                    END IF;
                END IF;
                RETURN NEW;
            END;
            $$
            """
        )
    )
    op.execute(
        "CREATE TRIGGER trg_attach_reply_decision_generation "
        "BEFORE INSERT ON reply_decisions FOR EACH ROW "
        "EXECUTE FUNCTION attach_reply_decision_generation()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_attach_reply_decision_generation ON reply_decisions")
    op.execute("DROP FUNCTION IF EXISTS attach_reply_decision_generation()")
    op.execute("DROP TRIGGER IF EXISTS trg_attach_decision_job_generation ON decision_jobs")
    op.execute("DROP FUNCTION IF EXISTS attach_decision_job_generation()")
    op.execute("DROP TRIGGER IF EXISTS trg_reserve_message_decision_generation ON messages")
    op.execute("DROP FUNCTION IF EXISTS reserve_message_decision_generation()")
    op.drop_index("ix_reply_decisions_decision_job_id", table_name="reply_decisions")
    op.drop_index("ix_decision_jobs_conversation_generation", table_name="decision_jobs")
    op.drop_index("ix_messages_conversation_decision_generation", table_name="messages")
    op.drop_constraint("fk_reply_decisions_decision_job_id", "reply_decisions", type_="foreignkey")
    op.drop_column("reply_decisions", "decision_claim_token")
    op.drop_column("reply_decisions", "decision_generation")
    op.drop_column("reply_decisions", "decision_job_id")
    op.drop_column("decision_jobs", "claim_token")
    op.drop_column("decision_jobs", "decision_generation")
    op.drop_column("messages", "decision_generation")
    op.drop_constraint(
        "ck_conversations_decision_generation",
        "conversations",
        type_="check",
    )
    op.drop_column("conversations", "decision_generation")
