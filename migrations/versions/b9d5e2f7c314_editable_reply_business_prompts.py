"""add versioned editable reply business prompts

Revision ID: b9d5e2f7c314
Revises: a7c3e9d1b624
Create Date: 2026-08-25
"""

import hashlib
import uuid

import sqlalchemy as sa
from alembic import op


revision = "b9d5e2f7c314"
down_revision = "a7c3e9d1b624"
branch_labels = None
depends_on = None

_MIGRATION_ACTOR = "migration:b9d5e2f7c314"
_DEFAULT_VOICE_PREFERENCES = {
    "tone": "professional",
    "length": "concise",
    "empathy": "standard",
    "emoji": "never",
}
_TONE_CLAUSES = {
    "professional": "Use a professional, calm, and plain-spoken tone.",
    "warm": "Use a warm, approachable, and reassuring tone.",
    "formal": "Use a formal, respectful, and precise tone.",
}
_LENGTH_CLAUSES = {
    "concise": "Keep replies concise and focused on the customer's immediate question.",
    "balanced": "Use a balanced amount of detail while staying focused on the question.",
}
_EMPATHY_CLAUSES = {
    "standard": "Acknowledge the customer's concern when relevant without overstating emotion.",
    "high": "Show clear empathy for the customer's concern while remaining factual and composed.",
}
_EMOJI_CLAUSES = {
    "never": "Do not use emoji.",
    "sparingly": "Use at most one simple emoji, and only when it naturally fits the locale.",
}


def _normalize_prompt(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


def _compile_voice_preferences(value: object) -> str:
    preferences = value if isinstance(value, dict) else _DEFAULT_VOICE_PREFERENCES
    if set(preferences) != set(_DEFAULT_VOICE_PREFERENCES):
        preferences = _DEFAULT_VOICE_PREFERENCES
    tone = preferences.get("tone")
    length = preferences.get("length")
    empathy = preferences.get("empathy")
    emoji = preferences.get("emoji")
    if (
        tone not in _TONE_CLAUSES
        or length not in _LENGTH_CLAUSES
        or empathy not in _EMPATHY_CLAUSES
        or emoji not in _EMOJI_CLAUSES
    ):
        tone = _DEFAULT_VOICE_PREFERENCES["tone"]
        length = _DEFAULT_VOICE_PREFERENCES["length"]
        empathy = _DEFAULT_VOICE_PREFERENCES["empathy"]
        emoji = _DEFAULT_VOICE_PREFERENCES["emoji"]
    clauses = (
        _TONE_CLAUSES[tone],
        _LENGTH_CLAUSES[length],
        _EMPATHY_CLAUSES[empathy],
        _EMOJI_CLAUSES[emoji],
    )
    return "Brand voice preferences:\n" + "\n".join(f"- {clause}" for clause in clauses)


def upgrade() -> None:
    op.create_table(
        "reply_business_prompt_versions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("brand_id", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("change_note", sa.String(length=240), nullable=True),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "revision > 0",
            name="ck_reply_business_prompt_versions_revision",
        ),
        sa.CheckConstraint(
            "btrim(content) <> '' AND char_length(content) <= 4000",
            name="ck_reply_business_prompt_versions_content",
        ),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_reply_business_prompt_versions_content_hash",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "brand_id",
            "revision",
            name="uq_reply_business_prompt_versions_scope_revision",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "brand_id",
            "id",
            name="uq_reply_business_prompt_versions_scope_id",
        ),
    )
    op.create_table(
        "reply_business_prompts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("brand_id", sa.String(length=64), nullable=False),
        sa.Column("active_version_id", sa.UUID(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("updated_by", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "revision > 0",
            name="ck_reply_business_prompts_revision",
        ),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_reply_business_prompts_content_hash",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "brand_id", "active_version_id"],
            [
                "reply_business_prompt_versions.tenant_id",
                "reply_business_prompt_versions.brand_id",
                "reply_business_prompt_versions.id",
            ],
            name="fk_reply_business_prompts_active_scope_version",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "brand_id",
            name="uq_reply_business_prompts_scope",
        ),
    )
    op.create_index(
        "ix_reply_business_prompts_tenant_id",
        "reply_business_prompts",
        ["tenant_id"],
        unique=False,
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION guard_reply_business_prompt_version_immutable()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'reply_business_prompt_version_is_immutable';
                RETURN OLD;
            END;
            $$ LANGUAGE plpgsql
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER trg_reply_business_prompt_versions_immutable
            BEFORE UPDATE OR DELETE ON reply_business_prompt_versions
            FOR EACH ROW EXECUTE FUNCTION guard_reply_business_prompt_version_immutable()
            """
        )
    )

    bind = op.get_bind()
    bind.execute(sa.text("LOCK TABLE reply_prompts IN SHARE ROW EXCLUSIVE MODE"))
    legacy_prompts = bind.execute(
        sa.text(
            "SELECT tenant_id, brand_id, voice_preferences, revision, updated_by "
            "FROM reply_prompts ORDER BY tenant_id, brand_id"
        )
    ).mappings()
    for legacy_prompt in legacy_prompts:
        content = _normalize_prompt(
            _compile_voice_preferences(legacy_prompt["voice_preferences"])
        )
        if not content or len(content) > 4000:
            raise RuntimeError("legacy reply prompt cannot be represented safely")
        version_id = uuid.uuid4()
        prompt_id = uuid.uuid4()
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        revision_number = max(int(legacy_prompt["revision"] or 1), 1)
        bind.execute(
            sa.text(
                "INSERT INTO reply_business_prompt_versions "
                "(id, tenant_id, brand_id, revision, content, content_hash, change_note, "
                "created_by) VALUES (:id, :tenant_id, :brand_id, :revision, :content, "
                ":content_hash, :change_note, :created_by)"
            ),
            {
                "id": version_id,
                "tenant_id": legacy_prompt["tenant_id"],
                "brand_id": legacy_prompt["brand_id"],
                "revision": revision_number,
                "content": content,
                "content_hash": content_hash,
                "change_note": "Imported current compiled brand voice",
                "created_by": _MIGRATION_ACTOR,
            },
        )
        bind.execute(
            sa.text(
                "INSERT INTO reply_business_prompts "
                "(id, tenant_id, brand_id, active_version_id, revision, content_hash, "
                "updated_by) VALUES (:id, :tenant_id, :brand_id, :active_version_id, "
                ":revision, :content_hash, :updated_by)"
            ),
            {
                "id": prompt_id,
                "tenant_id": legacy_prompt["tenant_id"],
                "brand_id": legacy_prompt["brand_id"],
                "active_version_id": version_id,
                "revision": revision_number,
                "content_hash": content_hash,
                "updated_by": legacy_prompt["updated_by"] or _MIGRATION_ACTOR,
            },
        )

    op.execute(
        sa.text(
            """
            CREATE FUNCTION guard_legacy_reply_prompt_write_after_business_prompt_upgrade()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'legacy_reply_prompt_write_forbidden_after_upgrade';
                RETURN OLD;
            END;
            $$ LANGUAGE plpgsql
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER trg_reply_prompts_frozen_after_business_prompt_upgrade
            BEFORE INSERT OR UPDATE OR DELETE ON reply_prompts
            FOR EACH ROW EXECUTE FUNCTION
                guard_legacy_reply_prompt_write_after_business_prompt_upgrade()
            """
        )
    )

    op.add_column(
        "reply_decisions",
        sa.Column("reply_business_prompt_version_id", sa.UUID(), nullable=True),
    )
    op.add_column(
        "reply_decisions",
        sa.Column("reply_business_prompt_content_hash", sa.String(length=64), nullable=True),
    )
    op.create_foreign_key(
        "fk_reply_decisions_business_prompt_version",
        "reply_decisions",
        "reply_business_prompt_versions",
        ["reply_business_prompt_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_reply_decisions_business_prompt_hash",
        "reply_decisions",
        "reply_business_prompt_content_hash IS NULL OR "
        "reply_business_prompt_content_hash ~ '^[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        "ck_reply_decisions_business_prompt_provenance_pair",
        "reply_decisions",
        "reply_business_prompt_version_id IS NULL OR "
        "reply_business_prompt_content_hash IS NOT NULL",
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "LOCK TABLE reply_decisions, reply_business_prompt_versions, "
            "reply_business_prompts, reply_prompts IN ACCESS EXCLUSIVE MODE"
        )
    )
    decision_provenance_count = bind.execute(
        sa.text(
            "SELECT count(*) FROM reply_decisions "
            "WHERE reply_business_prompt_version_id IS NOT NULL "
            "OR reply_business_prompt_content_hash IS NOT NULL"
        )
    ).scalar_one()
    if decision_provenance_count:
        raise RuntimeError(
            "cannot downgrade while reply decisions retain business prompt provenance"
        )

    edited_version_count = bind.execute(
        sa.text(
            "SELECT count(*) FROM reply_business_prompt_versions "
            "WHERE created_by <> :migration_actor"
        ),
        {"migration_actor": _MIGRATION_ACTOR},
    ).scalar_one()
    if edited_version_count:
        raise RuntimeError(
            "cannot downgrade while editable business prompt history exists"
        )

    op.drop_constraint(
        "ck_reply_decisions_business_prompt_provenance_pair",
        "reply_decisions",
        type_="check",
    )
    op.drop_constraint(
        "ck_reply_decisions_business_prompt_hash",
        "reply_decisions",
        type_="check",
    )
    op.drop_constraint(
        "fk_reply_decisions_business_prompt_version",
        "reply_decisions",
        type_="foreignkey",
    )
    op.drop_column("reply_decisions", "reply_business_prompt_content_hash")
    op.drop_column("reply_decisions", "reply_business_prompt_version_id")
    op.drop_index(
        "ix_reply_business_prompts_tenant_id",
        table_name="reply_business_prompts",
    )
    op.drop_table("reply_business_prompts")
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS trg_reply_prompts_frozen_after_business_prompt_upgrade "
            "ON reply_prompts"
        )
    )
    op.execute(
        sa.text(
            "DROP FUNCTION IF EXISTS "
            "guard_legacy_reply_prompt_write_after_business_prompt_upgrade()"
        )
    )
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS trg_reply_business_prompt_versions_immutable "
            "ON reply_business_prompt_versions"
        )
    )
    op.drop_table("reply_business_prompt_versions")
    op.execute(
        sa.text(
            "DROP FUNCTION IF EXISTS guard_reply_business_prompt_version_immutable()"
        )
    )
