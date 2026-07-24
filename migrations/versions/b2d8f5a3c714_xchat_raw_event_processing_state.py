"""add mutable XChat RawEvent processing state

Revision ID: b2d8f5a3c714
Revises: a1c7e4f2b903
"""

from alembic import op
import sqlalchemy as sa


revision = "b2d8f5a3c714"
down_revision = "a1c7e4f2b903"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("raw_events", sa.Column("processing_claim_token", sa.UUID(), nullable=True))
    op.add_column(
        "raw_events",
        sa.Column("processing_claim_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "raw_events",
        sa.Column(
            "processing_attempt_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.add_column(
        "raw_events",
        sa.Column("processing_next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("raw_events", sa.Column("processing_error_code", sa.Text(), nullable=True))
    op.add_column(
        "raw_events",
        sa.Column("processing_last_dispatched_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_raw_events_processing_attempt_count",
        "raw_events",
        "processing_attempt_count >= 0",
    )
    op.create_index(
        "ix_raw_events_processing_due",
        "raw_events",
        ["processing_status", "processing_next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_raw_events_processing_due", table_name="raw_events")
    op.drop_constraint(
        "ck_raw_events_processing_attempt_count",
        "raw_events",
        type_="check",
    )
    op.drop_column("raw_events", "processing_last_dispatched_at")
    op.drop_column("raw_events", "processing_error_code")
    op.drop_column("raw_events", "processing_next_attempt_at")
    op.drop_column("raw_events", "processing_attempt_count")
    op.drop_column("raw_events", "processing_claim_expires_at")
    op.drop_column("raw_events", "processing_claim_token")
