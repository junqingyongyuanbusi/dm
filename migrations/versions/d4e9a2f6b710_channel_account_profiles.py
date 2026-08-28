"""add channel account profile metadata

Revision ID: d4e9a2f6b710
Revises: c8f1a4d7e203
Create Date: 2026-08-26
"""

import sqlalchemy as sa
from alembic import op


revision = "d4e9a2f6b710"
down_revision = "c8f1a4d7e203"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "platform_accounts",
        sa.Column("provider_username", sa.Text(), nullable=True),
    )
    op.add_column(
        "platform_accounts",
        sa.Column("avatar_url", sa.Text(), nullable=True),
    )
    op.add_column(
        "platform_accounts",
        sa.Column(
            "profile_updated_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("platform_accounts", "profile_updated_at")
    op.drop_column("platform_accounts", "avatar_url")
    op.drop_column("platform_accounts", "provider_username")
