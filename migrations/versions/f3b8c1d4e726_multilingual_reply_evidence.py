"""multilingual reply evidence

Revision ID: f3b8c1d4e726
Revises: e9a1c4f7b620
Create Date: 2026-08-14
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision = "f3b8c1d4e726"
down_revision = "e9a1c4f7b620"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "reply_decisions",
        sa.Column("request_language", sa.String(length=35), server_default="und", nullable=False),
    )
    op.add_column(
        "reply_decisions",
        sa.Column("reply_language", sa.String(length=35), server_default="und", nullable=False),
    )
    op.add_column(
        "reply_decisions",
        sa.Column("knowledge_content_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "reply_decisions",
        sa.Column("knowledge_similarity", sa.Float(), nullable=True),
    )
    op.add_column(
        "reply_decisions",
        sa.Column("knowledge_similarity_margin", sa.Float(), nullable=True),
    )
    op.add_column(
        "reply_decisions",
        sa.Column("multilingual_shadow", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column(
        "reply_decisions",
        sa.Column("multilingual_contract_version", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "reply_decisions",
        sa.Column(
            "multilingual_shadow_evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
    )
    op.add_column(
        "reply_decisions",
        sa.Column("request_language_confidence", sa.Float(), nullable=True),
    )
    op.add_column(
        "reply_decisions",
        sa.Column("request_language_source", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "reply_decisions",
        sa.Column("knowledge_top2_content_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "reply_decisions",
        sa.Column("knowledge_top2_similarity", sa.Float(), nullable=True),
    )
    op.add_column(
        "reply_decisions",
        sa.Column("knowledge_match_status", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "reply_decisions",
        sa.Column("knowledge_gate_version", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "reply_decisions",
        sa.Column("knowledge_min_similarity_threshold", sa.Float(), nullable=True),
    )
    op.add_column(
        "reply_decisions",
        sa.Column("knowledge_min_margin_threshold", sa.Float(), nullable=True),
    )
    op.add_column(
        "reply_decisions",
        sa.Column("grounding_verified", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "reply_decisions",
        sa.Column("grounding_verifier_version", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "reply_decisions",
        sa.Column("grounding_latency_ms", sa.Float(), nullable=True),
    )
    op.add_column(
        "knowledge_documents",
        sa.Column("source_language", sa.String(length=35), server_default="und", nullable=False),
    )
    op.add_column(
        "knowledge_documents",
        sa.Column("detected_language", sa.String(length=35), server_default="und", nullable=False),
    )
    op.add_column(
        "knowledge_documents",
        sa.Column(
            "language_detection_status",
            sa.String(length=16),
            server_default="unknown",
            nullable=False,
        ),
    )
    op.add_column(
        "knowledge_documents",
        sa.Column("language_verified", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column(
        "knowledge_documents",
        sa.Column("import_batch_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index(
        "ix_knowledge_documents_import_batch_id",
        "knowledge_documents",
        ["import_batch_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_knowledge_documents_import_batch_id", table_name="knowledge_documents")
    op.drop_column("knowledge_documents", "import_batch_id")
    op.drop_column("knowledge_documents", "language_verified")
    op.drop_column("knowledge_documents", "language_detection_status")
    op.drop_column("knowledge_documents", "detected_language")
    op.drop_column("knowledge_documents", "source_language")
    op.drop_column("reply_decisions", "grounding_latency_ms")
    op.drop_column("reply_decisions", "grounding_verifier_version")
    op.drop_column("reply_decisions", "grounding_verified")
    op.drop_column("reply_decisions", "knowledge_min_margin_threshold")
    op.drop_column("reply_decisions", "knowledge_min_similarity_threshold")
    op.drop_column("reply_decisions", "knowledge_gate_version")
    op.drop_column("reply_decisions", "knowledge_match_status")
    op.drop_column("reply_decisions", "knowledge_top2_similarity")
    op.drop_column("reply_decisions", "knowledge_top2_content_hash")
    op.drop_column("reply_decisions", "request_language_source")
    op.drop_column("reply_decisions", "request_language_confidence")
    op.drop_column("reply_decisions", "multilingual_shadow_evidence")
    op.drop_column("reply_decisions", "multilingual_contract_version")
    op.drop_column("reply_decisions", "multilingual_shadow")
    op.drop_column("reply_decisions", "knowledge_similarity_margin")
    op.drop_column("reply_decisions", "knowledge_similarity")
    op.drop_column("reply_decisions", "knowledge_content_hash")
    op.drop_column("reply_decisions", "reply_language")
    op.drop_column("reply_decisions", "request_language")
