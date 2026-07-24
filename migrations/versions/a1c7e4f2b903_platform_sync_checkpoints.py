"""add durable platform sync checkpoints

Revision ID: a1c7e4f2b903
Revises: d6b8f0a2c431
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "a1c7e4f2b903"
down_revision = "d6b8f0a2c431"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "platform_checkpoints",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("platform_account_id", sa.UUID(), nullable=False),
        sa.Column("stream", sa.Text(), nullable=False),
        sa.Column("scope_key", sa.Text(), server_default="", nullable=False),
        sa.Column("cursor", sa.Text(), nullable=True),
        sa.Column("bootstrapped", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("revision", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_token", sa.UUID(), nullable=True),
        sa.Column("claimed_by", sa.Text(), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "stream IN ('X_LEGACY_DM', 'XCHAT_DISCOVERY', 'XCHAT_CONVERSATION')",
            name="ck_platform_checkpoints_stream",
        ),
        sa.CheckConstraint(
            "(stream = 'XCHAT_CONVERSATION' AND scope_key <> '') OR "
            "(stream <> 'XCHAT_CONVERSATION' AND scope_key = '')",
            name="ck_platform_checkpoints_scope",
        ),
        sa.CheckConstraint("revision >= 0", name="ck_platform_checkpoints_revision"),
        sa.CheckConstraint(
            "(claim_token IS NULL AND claimed_by IS NULL AND claim_expires_at IS NULL) OR "
            "(claim_token IS NOT NULL AND claimed_by IS NOT NULL "
            "AND claim_expires_at IS NOT NULL)",
            name="ck_platform_checkpoints_claim",
        ),
        sa.ForeignKeyConstraint(
            ["platform_account_id"], ["platform_accounts.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "platform_account_id",
            "stream",
            "scope_key",
            name="uq_platform_checkpoints_account_stream_scope",
        ),
    )
    op.create_index(
        "ix_platform_checkpoints_due",
        "platform_checkpoints",
        ["stream", "next_attempt_at", "claim_expires_at"],
    )
    op.create_index(
        "ix_platform_checkpoints_account",
        "platform_checkpoints",
        ["platform_account_id", "stream"],
    )

    op.create_table(
        "sync_runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("checkpoint_id", sa.UUID(), nullable=False),
        sa.Column("claim_token", sa.UUID(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="RUNNING", nullable=False),
        sa.Column("cursor_before", sa.Text(), nullable=True),
        sa.Column("cursor_after", sa.Text(), nullable=True),
        sa.Column("resume_token", sa.Text(), nullable=True),
        sa.Column("page_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("occurrence_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("mode IN ('POLL', 'BACKFILL')", name="ck_sync_runs_mode"),
        sa.CheckConstraint(
            "status IN ('RUNNING', 'SUCCEEDED', 'GAPPED', 'FAILED', 'LEASE_LOST')",
            name="ck_sync_runs_status",
        ),
        sa.CheckConstraint(
            "page_count >= 0 AND occurrence_count >= 0",
            name="ck_sync_runs_counts",
        ),
        sa.ForeignKeyConstraint(["checkpoint_id"], ["platform_checkpoints.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_sync_runs_checkpoint_started",
        "sync_runs",
        ["checkpoint_id", "started_at"],
    )

    op.create_table(
        "sync_gaps",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("checkpoint_id", sa.UUID(), nullable=False),
        sa.Column("sync_run_id", sa.UUID(), nullable=False),
        sa.Column("gap_type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="OPEN", nullable=False),
        sa.Column("cursor_before", sa.Text(), nullable=True),
        sa.Column("candidate_cursor", sa.Text(), nullable=True),
        sa.Column("resume_token", sa.Text(), nullable=True),
        sa.Column(
            "detail",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "gap_type IN ('PAGE_CAP', 'PAGINATION_ERROR', 'DECRYPT_ERROR')",
            name="ck_sync_gaps_type",
        ),
        sa.CheckConstraint(
            "status IN ('OPEN', 'RETRYING', 'RESOLVED')",
            name="ck_sync_gaps_status",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_sync_gaps_attempt_count"),
        sa.ForeignKeyConstraint(["checkpoint_id"], ["platform_checkpoints.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["sync_run_id"], ["sync_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_sync_gaps_retry", "sync_gaps", ["status", "next_attempt_at"])
    op.create_index(
        "uq_sync_gaps_active_checkpoint",
        "sync_gaps",
        ["checkpoint_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('OPEN', 'RETRYING')"),
    )

    op.execute(
        sa.text(
            """
            DO $$
            DECLARE bad_id uuid;
            BEGIN
              SELECT id INTO bad_id
              FROM platform_accounts
              WHERE platform = 'x'
                AND (
                  (config ? 'xchat_cursors'
                   AND jsonb_typeof(config->'xchat_cursors') IS DISTINCT FROM 'object')
                  OR (config ? 'xchat_bootstrapped'
                      AND jsonb_typeof(config->'xchat_bootstrapped') IS DISTINCT FROM 'object')
                )
              LIMIT 1;
              IF bad_id IS NOT NULL THEN
                RAISE EXCEPTION 'invalid xchat checkpoint config for account %', bad_id;
              END IF;

              SELECT p.id INTO bad_id
              FROM platform_accounts p
              CROSS JOIN LATERAL jsonb_each(COALESCE(p.config->'xchat_cursors', '{}'::jsonb)) e
              WHERE p.platform = 'x'
                AND (
                  e.key = ''
                  OR jsonb_typeof(e.value) <> 'string'
                  OR (e.value #>> '{}') !~ '^[0-9]+$'
                )
              LIMIT 1;
              IF bad_id IS NOT NULL THEN
                RAISE EXCEPTION 'invalid xchat cursor config for account %', bad_id;
              END IF;

              SELECT p.id INTO bad_id
              FROM platform_accounts p
              CROSS JOIN LATERAL jsonb_each(COALESCE(p.config->'xchat_bootstrapped', '{}'::jsonb)) e
              WHERE p.platform = 'x'
                AND (e.key = '' OR jsonb_typeof(e.value) <> 'boolean')
              LIMIT 1;
              IF bad_id IS NOT NULL THEN
                RAISE EXCEPTION 'invalid xchat bootstrap config for account %', bad_id;
              END IF;
            END $$;
            """
        )
    )

    op.execute(
        sa.text(
            """
            INSERT INTO platform_checkpoints (
              id, tenant_id, platform_account_id, stream, scope_key, cursor, bootstrapped
            )
            SELECT
              gen_random_uuid(), tenant_id, id, 'X_LEGACY_DM', '',
              NULLIF(config->>'x_dm_cursor', ''),
              CASE
                WHEN lower(COALESCE(config->>'x_dm_bootstrapped', '')) IN ('true', '1', 'yes', 'on')
                  THEN true
                ELSE NULLIF(config->>'x_dm_cursor', '') IS NOT NULL
              END
            FROM platform_accounts
            WHERE platform = 'x'
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO platform_checkpoints (
              id, tenant_id, platform_account_id, stream, scope_key, bootstrapped
            )
            SELECT gen_random_uuid(), tenant_id, id, 'XCHAT_DISCOVERY', '', false
            FROM platform_accounts
            WHERE platform = 'x'
            """
        )
    )
    op.execute(
        sa.text(
            """
            WITH checkpoint_values AS (
              SELECT
                p.id,
                p.tenant_id,
                replace(item.key, '-', ':') AS scope_key,
                item.value #>> '{}' AS cursor,
                true AS bootstrapped
              FROM platform_accounts p
              CROSS JOIN LATERAL jsonb_each(
                COALESCE(p.config->'xchat_cursors', '{}'::jsonb)
              ) item
              WHERE p.platform = 'x'
              UNION ALL
              SELECT
                p.id,
                p.tenant_id,
                replace(item.key, '-', ':') AS scope_key,
                NULL AS cursor,
                (item.value #>> '{}')::boolean AS bootstrapped
              FROM platform_accounts p
              CROSS JOIN LATERAL jsonb_each(
                COALESCE(p.config->'xchat_bootstrapped', '{}'::jsonb)
              ) item
              WHERE p.platform = 'x'
            ), merged AS (
              SELECT
                id,
                tenant_id,
                scope_key,
                max(cursor::numeric)::text AS cursor,
                bool_or(bootstrapped) AS bootstrapped
              FROM checkpoint_values
              GROUP BY id, tenant_id, scope_key
            )
            INSERT INTO platform_checkpoints (
              id, tenant_id, platform_account_id, stream, scope_key, cursor, bootstrapped
            )
            SELECT
              gen_random_uuid(), tenant_id, id, 'XCHAT_CONVERSATION', scope_key,
              cursor, bootstrapped
            FROM merged
            """
        )
    )


def downgrade() -> None:
    op.drop_index("uq_sync_gaps_active_checkpoint", table_name="sync_gaps")
    op.drop_index("ix_sync_gaps_retry", table_name="sync_gaps")
    op.drop_table("sync_gaps")
    op.drop_index("ix_sync_runs_checkpoint_started", table_name="sync_runs")
    op.drop_table("sync_runs")
    op.drop_index("ix_platform_checkpoints_account", table_name="platform_checkpoints")
    op.drop_index("ix_platform_checkpoints_due", table_name="platform_checkpoints")
    op.drop_table("platform_checkpoints")
