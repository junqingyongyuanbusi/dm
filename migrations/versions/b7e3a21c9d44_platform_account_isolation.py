"""platform account runtime isolation

Revision ID: b7e3a21c9d44
Revises: 8c91d5a74f20
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "b7e3a21c9d44"
down_revision = "8c91d5a74f20"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "platform_apps",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("platform_family", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("external_app_id", sa.Text(), nullable=True),
        sa.Column("public_id", sa.Text(), nullable=False),
        sa.Column("credential_ref", sa.Text(), nullable=False),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("config_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "platform_family",
            "external_app_id",
            name="uq_platform_apps_tenant_family_external_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "platform_family",
            "public_id",
            name="uq_platform_apps_tenant_family_public_id",
        ),
    )
    op.add_column("platform_accounts", sa.Column("external_account_id", sa.Text()))
    op.add_column("platform_accounts", sa.Column("public_id", sa.Text()))
    op.add_column("platform_accounts", sa.Column("platform_app_id", sa.UUID()))
    op.add_column("platform_accounts", sa.Column("credential_ref", sa.Text()))
    op.add_column("platform_accounts", sa.Column("webhook_secret_ref", sa.Text()))
    op.add_column(
        "platform_accounts",
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "platform_accounts",
        sa.Column(
            "capability",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "platform_accounts",
        sa.Column("config_version", sa.Integer(), server_default="1", nullable=False),
    )
    op.create_unique_constraint(
        "uq_platform_accounts_tenant_platform_public_id",
        "platform_accounts",
        ["tenant_id", "platform", "public_id"],
    )
    op.create_unique_constraint(
        "uq_platform_accounts_tenant_platform_external_id",
        "platform_accounts",
        ["tenant_id", "platform", "external_account_id"],
    )
    op.create_foreign_key(
        "fk_platform_accounts_platform_app_id",
        "platform_accounts",
        "platform_apps",
        ["platform_app_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_platform_accounts_platform_app_id",
        "platform_accounts",
        type_="foreignkey",
    )
    op.drop_constraint(
        "uq_platform_accounts_tenant_platform_external_id",
        "platform_accounts",
        type_="unique",
    )
    op.drop_constraint(
        "uq_platform_accounts_tenant_platform_public_id",
        "platform_accounts",
        type_="unique",
    )
    op.drop_column("platform_accounts", "config_version")
    op.drop_column("platform_accounts", "capability")
    op.drop_column("platform_accounts", "config")
    op.drop_column("platform_accounts", "webhook_secret_ref")
    op.drop_column("platform_accounts", "credential_ref")
    op.drop_column("platform_accounts", "platform_app_id")
    op.drop_column("platform_accounts", "public_id")
    op.drop_column("platform_accounts", "external_account_id")
    op.drop_table("platform_apps")
