"""add structured voice preferences and draft-first knowledge approval

Revision ID: d3f6a1b8c904
Revises: a9d4e6f2b713

Downgrade removes the additive columns and constraint but cannot reconstruct arbitrary legacy
persona text neutralized by upgrade. Restore a pre-upgrade database backup if that text is needed.
"""

import json

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "d3f6a1b8c904"
down_revision = "a9d4e6f2b713"
branch_labels = None
depends_on = None


_CANONICAL_VOICE = {
    "tone": "professional",
    "length": "concise",
    "empathy": "standard",
    "emoji": "never",
}
_CANONICAL_VOICE_JSON = json.dumps(_CANONICAL_VOICE, sort_keys=True, separators=(",", ":"))
_COMPILED_DEFAULT_PERSONA = (
    "Brand voice preferences:\n"
    "- Use a professional, calm, and plain-spoken tone.\n"
    "- Keep replies concise and focused on the customer's immediate question.\n"
    "- Acknowledge the customer's concern when relevant without overstating emotion.\n"
    "- Do not use emoji."
)
_MIGRATION_ACTOR = "migration:d3f6a1b8c904"


def upgrade() -> None:
    op.execute(
        sa.text(
            "DO $$ BEGIN "
            "IF EXISTS (SELECT 1 FROM knowledge_documents "
            "WHERE status NOT IN ('draft', 'published')) THEN "
            "RAISE EXCEPTION 'unknown knowledge_documents.status values'; "
            "END IF; END $$"
        )
    )

    op.add_column(
        "reply_prompts",
        sa.Column(
            "voice_preferences",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text(f"'{_CANONICAL_VOICE_JSON}'::jsonb"),
            nullable=True,
        ),
    )
    op.execute(
        sa.text(
            "UPDATE reply_prompts "
            "SET voice_preferences = CAST(:voice AS jsonb), "
            "persona = :persona, revision = revision + 1, updated_by = :actor"
        ).bindparams(
            voice=_CANONICAL_VOICE_JSON,
            persona=_COMPILED_DEFAULT_PERSONA,
            actor=_MIGRATION_ACTOR,
        )
    )
    op.alter_column("reply_prompts", "voice_preferences", nullable=False)

    op.alter_column(
        "knowledge_documents",
        "status",
        existing_type=sa.String(length=16),
        server_default=sa.text("'draft'"),
        existing_nullable=False,
    )
    op.create_check_constraint(
        "ck_knowledge_documents_status",
        "knowledge_documents",
        "status IN ('draft', 'published')",
    )
    op.add_column(
        "knowledge_documents",
        sa.Column(
            "is_official_contact",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("knowledge_documents", "is_official_contact")
    op.drop_constraint("ck_knowledge_documents_status", "knowledge_documents", type_="check")
    op.alter_column(
        "knowledge_documents",
        "status",
        existing_type=sa.String(length=16),
        server_default=sa.text("'published'"),
        existing_nullable=False,
    )
    op.drop_column("reply_prompts", "voice_preferences")
