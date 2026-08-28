"""add user roles and account ownership

Revision ID: c8f1a4d7e203
Revises: b9d5e2f7c314
Create Date: 2026-08-27
"""

import sqlalchemy as sa
from alembic import op


revision = "c8f1a4d7e203"
down_revision = "b9d5e2f7c314"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "admin_users",
        sa.Column("role", sa.String(length=16), nullable=True),
    )
    # Existing database users previously had full Tenant administration access.
    op.execute("UPDATE admin_users SET role = 'ADMIN' WHERE role IS NULL")
    op.alter_column("admin_users", "role", nullable=False, server_default="USER")
    op.drop_constraint("admin_users_tenant_id_key", "admin_users", type_="unique")
    op.create_check_constraint(
        "ck_admin_users_role",
        "admin_users",
        "role IN ('ADMIN', 'USER')",
    )
    op.create_check_constraint(
        "ck_admin_users_status",
        "admin_users",
        "status IN ('active', 'disabled')",
    )
    op.create_index(
        "ix_admin_users_tenant_role_status",
        "admin_users",
        ["tenant_id", "role", "status"],
    )

    op.add_column(
        "platform_accounts",
        sa.Column("owner_user_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_platform_accounts_tenant_owner_user",
        "platform_accounts",
        "admin_users",
        ["tenant_id", "owner_user_id"],
        ["tenant_id", "id"],
    )
    op.create_index(
        "ix_platform_accounts_tenant_owner_status",
        "platform_accounts",
        ["tenant_id", "owner_user_id", "status"],
    )

    op.add_column(
        "provisioning_jobs",
        sa.Column("owner_user_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_provisioning_jobs_tenant_owner_user",
        "provisioning_jobs",
        "admin_users",
        ["tenant_id", "owner_user_id"],
        ["tenant_id", "id"],
    )
    op.create_index(
        "ix_provisioning_jobs_tenant_owner_created",
        "provisioning_jobs",
        ["tenant_id", "owner_user_id", "created_at"],
    )


def downgrade() -> None:
    duplicate_tenants = op.get_bind().execute(
        sa.text(
            "SELECT tenant_id FROM admin_users GROUP BY tenant_id HAVING count(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicate_tenants is not None:
        raise RuntimeError("cannot downgrade while a Tenant has multiple users")

    op.drop_index(
        "ix_provisioning_jobs_tenant_owner_created",
        table_name="provisioning_jobs",
    )
    op.drop_constraint(
        "fk_provisioning_jobs_tenant_owner_user",
        "provisioning_jobs",
        type_="foreignkey",
    )
    op.drop_column("provisioning_jobs", "owner_user_id")

    op.drop_index(
        "ix_platform_accounts_tenant_owner_status",
        table_name="platform_accounts",
    )
    op.drop_constraint(
        "fk_platform_accounts_tenant_owner_user",
        "platform_accounts",
        type_="foreignkey",
    )
    op.drop_column("platform_accounts", "owner_user_id")

    op.drop_index("ix_admin_users_tenant_role_status", table_name="admin_users")
    op.drop_constraint("ck_admin_users_status", "admin_users", type_="check")
    op.drop_constraint("ck_admin_users_role", "admin_users", type_="check")
    op.create_unique_constraint(
        "admin_users_tenant_id_key",
        "admin_users",
        ["tenant_id"],
    )
    op.drop_column("admin_users", "role")
