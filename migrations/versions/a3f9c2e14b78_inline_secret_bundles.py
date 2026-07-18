"""add shared encrypted secret envelope columns without dropping legacy references

Revision ID: a3f9c2e14b78
Revises: f4a82d7c6e10
Create Date: 2026-07-17
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "a3f9c2e14b78"
down_revision = "f4a82d7c6e10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("platform_apps", sa.Column("credential_bundle", JSONB(), nullable=True))
    op.add_column("platform_accounts", sa.Column("credential_bundle", JSONB(), nullable=True))
    op.add_column("platform_accounts", sa.Column("webhook_secret_bundle", JSONB(), nullable=True))
    op.add_column("provisioning_jobs", sa.Column("staging_secret", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("provisioning_jobs", "staging_secret")
    op.drop_column("platform_accounts", "webhook_secret_bundle")
    op.drop_column("platform_accounts", "credential_bundle")
    op.drop_column("platform_apps", "credential_bundle")
