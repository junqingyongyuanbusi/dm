"""knowledge tenant scope

Revision ID: c4d8e19f6a30
Revises: b7e3a21c9d44
"""

from alembic import op
import sqlalchemy as sa


revision = "c4d8e19f6a30"
down_revision = "b7e3a21c9d44"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "knowledge_documents",
        sa.Column("tenant_id", sa.String(length=64), server_default="default", nullable=False),
    )
    op.create_index(
        "ix_knowledge_documents_tenant_id", "knowledge_documents", ["tenant_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_knowledge_documents_tenant_id", table_name="knowledge_documents")
    op.drop_column("knowledge_documents", "tenant_id")
