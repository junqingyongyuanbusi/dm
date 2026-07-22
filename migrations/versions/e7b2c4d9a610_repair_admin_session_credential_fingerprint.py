"""repair admin session credential fingerprint

Revision ID: e7b2c4d9a610
Revises: da4e19c7b203
"""

import sqlalchemy as sa
from alembic import op

revision = "e7b2c4d9a610"
down_revision = "da4e19c7b203"
branch_labels = None
depends_on = None


def _admin_session_columns() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {column["name"] for column in inspector.get_columns("admin_sessions")}


def upgrade() -> None:
    if "credential_fingerprint" not in _admin_session_columns():
        op.add_column(
            "admin_sessions",
            sa.Column("credential_fingerprint", sa.String(length=64), nullable=True),
        )


def downgrade() -> None:
    if "credential_fingerprint" in _admin_session_columns():
        op.drop_column("admin_sessions", "credential_fingerprint")
