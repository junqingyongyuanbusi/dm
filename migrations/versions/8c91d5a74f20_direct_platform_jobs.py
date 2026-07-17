"""allow direct platform decision jobs

Revision ID: 8c91d5a74f20
Revises: 4a6f2c19d0b1
"""

from alembic import op


revision = "8c91d5a74f20"
down_revision = "4a6f2c19d0b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("decision_jobs", "raw_event_id", nullable=True)


def downgrade() -> None:
    op.execute("DELETE FROM decision_jobs WHERE raw_event_id IS NULL")
    op.alter_column("decision_jobs", "raw_event_id", nullable=False)
