"""durable decision jobs and decision idempotency

Revision ID: 4a6f2c19d0b1
Revises: e2534a2e7e50
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "4a6f2c19d0b1"
down_revision = "e2534a2e7e50"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "decision_jobs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("raw_event_id", sa.UUID(), nullable=True),
        sa.Column("conversation_id", sa.UUID(), nullable=False),
        sa.Column("message_id", sa.UUID(), nullable=False),
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["account_id"], ["platform_accounts.id"]),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"]),
        sa.ForeignKeyConstraint(["raw_event_id"], ["raw_events.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("message_id"),
    )
    op.create_index(
        "ix_decision_jobs_status_next_attempt",
        "decision_jobs",
        ["status", "next_attempt_at"],
        unique=False,
    )
    op.create_unique_constraint("uq_reply_decisions_message_id", "reply_decisions", ["message_id"])


def downgrade() -> None:
    op.drop_constraint("uq_reply_decisions_message_id", "reply_decisions", type_="unique")
    op.drop_index("ix_decision_jobs_status_next_attempt", table_name="decision_jobs")
    op.drop_table("decision_jobs")
