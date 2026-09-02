"""remove database ADMIN role

Revision ID: f3a7c9e1b5d2
Revises: d4e9a2f6b710
Create Date: 2026-09-01
"""

from alembic import op


revision = "f3a7c9e1b5d2"
down_revision = "d4e9a2f6b710"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Bound the startup migration wait and prevent legacy writers from adding a
    # new ADMIN row between the data conversion and constraint replacement.
    op.execute("SET LOCAL lock_timeout = '30s'")
    op.execute("LOCK TABLE admin_users IN ACCESS EXCLUSIVE MODE")
    op.execute("UPDATE admin_users SET role = 'USER' WHERE role = 'ADMIN'")
    op.drop_constraint("ck_admin_users_role", "admin_users", type_="check")
    op.create_check_constraint(
        "ck_admin_users_role",
        "admin_users",
        "role = 'USER'",
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '30s'")
    op.execute("LOCK TABLE admin_users IN ACCESS EXCLUSIVE MODE")
    op.drop_constraint("ck_admin_users_role", "admin_users", type_="check")
    op.create_check_constraint(
        "ck_admin_users_role",
        "admin_users",
        "role IN ('ADMIN', 'USER')",
    )
