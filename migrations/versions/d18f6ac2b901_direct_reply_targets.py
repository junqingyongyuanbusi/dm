"""persist direct platform reply targets

Revision ID: d18f6ac2b901
Revises: c4d8e19f6a30
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "d18f6ac2b901"
down_revision = "c4d8e19f6a30"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column(
            "reply_target",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("messages", "reply_target")
