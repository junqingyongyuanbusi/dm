"""admin control plane and globally routable webhook ids

Revision ID: f4a82d7c6e10
Revises: d18f6ac2b901
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "f4a82d7c6e10"
down_revision = "d18f6ac2b901"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return bool(
        op.get_bind()
        .execute(sa.text("SELECT to_regclass(:name) IS NOT NULL"), {"name": f"public.{name}"})
        .scalar()
    )


def _has_column(table: str, column: str) -> bool:
    return bool(
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name=:table AND column_name=:column)"
            ),
            {"table": table, "column": column},
        )
        .scalar()
    )


def _has_constraint(table: str, name: str) -> bool:
    return bool(
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM pg_constraint c "
                "JOIN pg_class t ON t.oid=c.conrelid WHERE t.relname=:table AND c.conname=:name)"
            ),
            {"table": table, "name": name},
        )
        .scalar()
    )


def upgrade() -> None:
    if not _has_table("platform_apps"):
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
        )
    if _has_constraint("platform_apps", "uq_platform_apps_tenant_family_public_id"):
        op.drop_constraint(
            "uq_platform_apps_tenant_family_public_id", "platform_apps", type_="unique"
        )
    if not _has_constraint("platform_apps", "uq_platform_apps_family_public_id"):
        op.create_unique_constraint(
            "uq_platform_apps_family_public_id",
            "platform_apps",
            ["platform_family", "public_id"],
        )

    if not _has_column("platform_accounts", "platform_app_id"):
        op.add_column("platform_accounts", sa.Column("platform_app_id", sa.UUID()))
    if not _has_constraint("platform_accounts", "fk_platform_accounts_platform_app_id"):
        op.create_foreign_key(
            "fk_platform_accounts_platform_app_id",
            "platform_accounts",
            "platform_apps",
            ["platform_app_id"],
            ["id"],
        )

    if not _has_column("messages", "reply_target"):
        op.add_column(
            "messages",
            sa.Column(
                "reply_target",
                postgresql.JSONB(astext_type=sa.Text()),
                server_default=sa.text("'{}'::jsonb"),
                nullable=False,
            ),
        )

    duplicate_app_ids = op.get_bind().execute(
        sa.text(
            "SELECT platform_family, public_id FROM platform_apps "
            "WHERE public_id IS NOT NULL GROUP BY platform_family, public_id HAVING count(*) > 1"
        )
    ).first()
    duplicate_account_ids = op.get_bind().execute(
        sa.text(
            "SELECT platform, public_id FROM platform_accounts "
            "WHERE public_id IS NOT NULL GROUP BY platform, public_id HAVING count(*) > 1"
        )
    ).first()
    if duplicate_app_ids or duplicate_account_ids:
        raise RuntimeError(
            "duplicate webhook public_id values must be renamed before f4a82d7c6e10"
        )

    # Backfill safe capability defaults for direct accounts created before capability enforcement.
    op.execute(
        sa.text(
            "UPDATE platform_accounts SET capability = CASE platform "
            "WHEN 'telegram' THEN '{\"dm\": true, \"max_text_length\": 4096}'::jsonb "
            "WHEN 'facebook' THEN '{\"dm\": true, \"comments\": true, \"max_text_length\": 2000}'::jsonb "
            "WHEN 'instagram' THEN '{\"dm\": true, \"comments\": true, \"max_text_length\": 1000}'::jsonb "
            "WHEN 'whatsapp' THEN '{\"dm\": true, \"session_messages\": true, \"max_text_length\": 4096}'::jsonb "
            "WHEN 'x' THEN '{\"dm\": true, \"mentions\": true, \"max_text_length\": 280}'::jsonb "
            "ELSE capability END "
            "WHERE COALESCE(capability, '{}'::jsonb) = '{}'::jsonb "
            "AND COALESCE(config->>'delivery_mode', '') = 'direct'"
        )
    )

    if _has_constraint("platform_accounts", "uq_platform_accounts_tenant_platform_public_id"):
        op.drop_constraint(
            "uq_platform_accounts_tenant_platform_public_id",
            "platform_accounts",
            type_="unique",
        )
    if not _has_constraint("platform_accounts", "uq_platform_accounts_platform_public_id"):
        op.create_unique_constraint(
            "uq_platform_accounts_platform_public_id",
            "platform_accounts",
            ["platform", "public_id"],
        )

    op.create_table(
        "provisioning_jobs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("brand_id", sa.Text(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column(
            "request",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("staging_secret_ref", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("current_step", sa.Text(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.Text(), nullable=True),
        sa.Column("account_id", sa.UUID(), nullable=True),
        sa.Column("platform_app_id", sa.UUID(), nullable=True),
        sa.Column(
            "result",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("last_error_code", sa.Text(), nullable=True),
        sa.Column("last_error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["account_id"], ["platform_accounts.id"]),
        sa.ForeignKeyConstraint(["platform_app_id"], ["platform_apps.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_provisioning_jobs_tenant_idempotency"
        ),
    )
    op.create_index(
        "ix_provisioning_jobs_status_next_attempt",
        "provisioning_jobs",
        ["status", "next_attempt_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_provisioning_jobs_status_next_attempt", table_name="provisioning_jobs")
    op.drop_table("provisioning_jobs")
    op.drop_constraint(
        "uq_platform_accounts_platform_public_id", "platform_accounts", type_="unique"
    )
