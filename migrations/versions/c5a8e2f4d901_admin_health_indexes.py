"""add tenant-scoped admin health indexes

Revision ID: c5a8e2f4d901
Revises: b2d8f5a3c714
"""

from alembic import op


revision = "c5a8e2f4d901"
down_revision = "b2d8f5a3c714"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_raw_events_tenant_status_received",
        "raw_events",
        ["tenant_id", "processing_status", "received_at"],
    )
    op.create_index(
        "ix_outbox_tenant_status_created",
        "outbox_messages",
        ["tenant_id", "status", "created_at"],
    )
    op.create_index(
        "ix_platform_accounts_tenant_status",
        "platform_accounts",
        ["tenant_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_platform_accounts_tenant_status",
        table_name="platform_accounts",
    )
    op.drop_index(
        "ix_outbox_tenant_status_created",
        table_name="outbox_messages",
    )
    op.drop_index(
        "ix_raw_events_tenant_status_received",
        table_name="raw_events",
    )
