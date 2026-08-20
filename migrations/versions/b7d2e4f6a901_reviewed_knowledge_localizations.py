"""add reviewed knowledge localization artifacts

Revision ID: b7d2e4f6a901
Revises: a6f1c3d8e205
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "b7d2e4f6a901"
down_revision = "a6f1c3d8e205"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_knowledge_documents_tenant_id_id",
        "knowledge_documents",
        ["tenant_id", "id"],
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1
            FROM knowledge_chunks c
            JOIN knowledge_documents d ON d.id = c.document_id
            WHERE c.tenant_id <> d.tenant_id
          ) THEN
            RAISE EXCEPTION 'knowledge chunk tenant/document mismatch';
          END IF;
        END $$;
        """
    )
    op.drop_constraint("knowledge_chunks_document_id_fkey", "knowledge_chunks", type_="foreignkey")
    op.create_foreign_key(
        "fk_knowledge_chunks_tenant_document",
        "knowledge_chunks",
        "knowledge_documents",
        ["tenant_id", "document_id"],
        ["tenant_id", "id"],
        ondelete="CASCADE",
    )

    op.create_table(
        "knowledge_localizations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("release_id", sa.String(length=64), nullable=False),
        sa.Column("locale", sa.String(length=35), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_hash", sa.String(length=64), nullable=False),
        sa.Column("source_content_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "protected_values",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "official_contact_authorized",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "auto_reply_allowed",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'draft'"),
            nullable=False,
        ),
        sa.Column("source_file", sa.String(length=256), nullable=True),
        sa.Column("import_batch_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reviewed_by", sa.Text(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.Text(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoke_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'published', 'revoked')",
            name="ck_knowledge_localizations_status",
        ),
        sa.CheckConstraint(
            "btrim(release_id) <> ''",
            name="ck_knowledge_localizations_release_id",
        ),
        sa.CheckConstraint(
            "status <> 'published' OR (reviewed_by IS NOT NULL AND btrim(reviewed_by) <> '' "
            "AND reviewed_at IS NOT NULL)",
            name="ck_knowledge_localizations_published_review",
        ),
        sa.CheckConstraint(
            "status <> 'revoked' OR (revoked_by IS NOT NULL AND btrim(revoked_by) <> '' "
            "AND revoked_at IS NOT NULL)",
            name="ck_knowledge_localizations_revoked_review",
        ),
        sa.CheckConstraint(
            "text_hash ~ '^[0-9a-f]{64}$' AND source_content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_knowledge_localizations_hashes",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["knowledge_documents.tenant_id", "knowledge_documents.id"],
            name="fk_knowledge_localizations_tenant_document",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_knowledge_localizations_tenant_id_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "id",
            "release_id",
            name="uq_knowledge_localizations_tenant_id_id_release",
        ),
    )
    op.create_index(
        "ix_knowledge_localizations_import_batch_id",
        "knowledge_localizations",
        ["import_batch_id"],
        unique=False,
    )
    op.create_index(
        "ix_knowledge_localizations_lookup",
        "knowledge_localizations",
        ["tenant_id", "document_id", "release_id", "status", "locale"],
        unique=False,
    )
    op.create_index(
        "uq_knowledge_localizations_active_locale",
        "knowledge_localizations",
        ["tenant_id", "document_id", "release_id", "locale"],
        unique=True,
        postgresql_where=sa.text("status = 'published'"),
    )

    op.add_column(
        "reply_decisions",
        sa.Column(
            "resolved_locale",
            sa.String(length=35),
            server_default=sa.text("'und'"),
            nullable=False,
        ),
    )
    op.add_column(
        "reply_decisions",
        sa.Column("knowledge_localization_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "reply_decisions",
        sa.Column("knowledge_localization_release_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "reply_decisions",
        sa.Column("knowledge_localization_text_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "reply_decisions",
        sa.Column("knowledge_localization_source_hash", sa.String(length=64), nullable=True),
    )
    op.create_check_constraint(
        "ck_reply_decisions_localization_provenance",
        "reply_decisions",
        "(knowledge_localization_id IS NULL AND knowledge_localization_release_id IS NULL "
        "AND knowledge_localization_text_hash IS NULL "
        "AND knowledge_localization_source_hash IS NULL) OR "
        "(knowledge_localization_id IS NOT NULL "
        "AND knowledge_localization_release_id IS NOT NULL "
        "AND btrim(knowledge_localization_release_id) <> '' AND resolved_locale <> 'und' "
        "AND knowledge_localization_text_hash ~ '^[0-9a-f]{64}$' "
        "AND knowledge_localization_source_hash ~ '^[0-9a-f]{64}$')",
    )
    op.create_foreign_key(
        "fk_reply_decisions_tenant_localization",
        "reply_decisions",
        "knowledge_localizations",
        ["tenant_id", "knowledge_localization_id", "knowledge_localization_release_id"],
        ["tenant_id", "id", "release_id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_reply_decisions_tenant_localization",
        "reply_decisions",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_reply_decisions_localization_provenance",
        "reply_decisions",
        type_="check",
    )
    op.drop_column("reply_decisions", "knowledge_localization_source_hash")
    op.drop_column("reply_decisions", "knowledge_localization_text_hash")
    op.drop_column("reply_decisions", "knowledge_localization_release_id")
    op.drop_column("reply_decisions", "knowledge_localization_id")
    op.drop_column("reply_decisions", "resolved_locale")

    op.drop_index(
        "uq_knowledge_localizations_active_locale",
        table_name="knowledge_localizations",
        postgresql_where=sa.text("status = 'published'"),
    )
    op.drop_index("ix_knowledge_localizations_lookup", table_name="knowledge_localizations")
    op.drop_index(
        "ix_knowledge_localizations_import_batch_id",
        table_name="knowledge_localizations",
    )
    op.drop_table("knowledge_localizations")
    op.drop_constraint(
        "fk_knowledge_chunks_tenant_document", "knowledge_chunks", type_="foreignkey"
    )
    op.create_foreign_key(
        "knowledge_chunks_document_id_fkey",
        "knowledge_chunks",
        "knowledge_documents",
        ["document_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_constraint(
        "uq_knowledge_documents_tenant_id_id",
        "knowledge_documents",
        type_="unique",
    )
