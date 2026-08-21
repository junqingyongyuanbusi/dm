"""bind multilingual knowledge evidence to selected document and chunk

Revision ID: c3e7a9f1b204
Revises: b7d2e4f6a901
Create Date: 2026-08-20
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c3e7a9f1b204"
down_revision = "b7d2e4f6a901"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "reply_decisions",
        sa.Column("knowledge_document_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "reply_decisions",
        sa.Column("knowledge_chunk_id", postgresql.UUID(as_uuid=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("reply_decisions", "knowledge_chunk_id")
    op.drop_column("reply_decisions", "knowledge_document_id")
