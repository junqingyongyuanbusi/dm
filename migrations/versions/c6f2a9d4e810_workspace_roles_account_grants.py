"""Five workspace roles and explicit tenant-bound account access.

Revision ID: c6f2a9d4e810
Revises: b9e5f3a7d102
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "c6f2a9d4e810"
down_revision = "b9e5f3a7d102"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_admin_users_role", "admin_users", type_="check")
    op.create_check_constraint(
        "ck_admin_users_role",
        "admin_users",
        "role IN ('USER', 'WORKSPACE_ADMIN', 'MANAGER', 'OPERATOR', 'AGENT', 'VIEWER')",
    )
    for column_name in ("operator_reply_enabled", "operator_takeover_enabled"):
        op.add_column(
            "admin_users",
            sa.Column(
                column_name,
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )
    op.create_table(
        "account_access_grants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("platform_account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("tenant_id", "platform_account_id", "user_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "platform_account_id"],
            ["platform_accounts.tenant_id", "platform_accounts.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["admin_users.tenant_id", "admin_users.id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_account_access_grants_tenant_user_active",
        "account_access_grants",
        ["tenant_id", "user_id", "active"],
    )
    # Snapshot only existing support membership. Future members never inherit this flag.
    op.execute("""
        INSERT INTO account_access_grants (id, tenant_id, platform_account_id, user_id, active)
        SELECT gen_random_uuid(), account.tenant_id, account.id, member.id, true
        FROM platform_accounts AS account
        JOIN admin_users AS member ON member.tenant_id = account.tenant_id
        WHERE account.shared_with_support IS TRUE AND member.role = 'USER'
    """)
    op.execute("UPDATE admin_users SET role = 'AGENT' WHERE role = 'USER'")
    op.alter_column("admin_users", "role", server_default="AGENT")


def downgrade() -> None:
    # Returning to USER restores connect permission and global sharing: never do it implicitly.
    raise RuntimeError(
        "Workspace permission downgrade requires an audited authority/data migration; "
        "automatic downgrade would restore revoked channel and shared-account permissions"
    )
