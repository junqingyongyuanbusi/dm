"""add first-class agent control plane

Revision ID: a8f4d2c6e901
Revises: f3a7c9e1b5d2
Create Date: 2026-09-02
"""

import hashlib
import json
import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "a8f4d2c6e901"
down_revision = "f3a7c9e1b5d2"
branch_labels = None
depends_on = None

_MIGRATION_ACTOR = "migration:a8f4d2c6e901"


def _agent_name(brand_id: str) -> str:
    if brand_id == "default":
        return "Default Agent"
    normalized = brand_id.replace("_", " ").replace("-", " ").strip() or brand_id
    return f"{normalized.title()} Agent"[:128]


def _snapshot_configuration(
    *,
    tenant_id: str,
    brand_id: str,
    business_prompt_version_id: uuid.UUID | None,
) -> dict[str, object]:
    return {
        "business_prompt_version_id": (
            str(business_prompt_version_id) if business_prompt_version_id else None
        ),
        "runtime_scope": {"brand_id": brand_id, "tenant_id": tenant_id},
        "schema_version": 1,
        "source": "legacy_scope_backfill",
    }


def _configuration_hash(configuration: dict[str, object]) -> str:
    payload = json.dumps(
        configuration,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def upgrade() -> None:
    op.create_table(
        "agents",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("legacy_brand_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'active'"),
            nullable=False,
        ),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("status IN ('active', 'archived')", name="ck_agents_status"),
        sa.CheckConstraint(
            "btrim(slug) <> '' AND char_length(slug) <= 64",
            name="ck_agents_slug",
        ),
        sa.CheckConstraint(
            "btrim(legacy_brand_id) <> '' AND char_length(legacy_brand_id) <= 64",
            name="ck_agents_legacy_brand_id",
        ),
        sa.CheckConstraint(
            "btrim(name) <> '' AND char_length(name) <= 128",
            name="ck_agents_name",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_agents_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "id",
            "legacy_brand_id",
            name="uq_agents_tenant_id_id_legacy_brand",
        ),
        sa.UniqueConstraint("tenant_id", "slug", name="uq_agents_tenant_slug"),
        sa.UniqueConstraint(
            "tenant_id",
            "legacy_brand_id",
            name="uq_agents_tenant_legacy_brand",
        ),
    )
    op.create_index("ix_agents_tenant_status", "agents", ["tenant_id", "status"])

    op.create_table(
        "agent_versions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("legacy_brand_id", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("business_prompt_version_id", sa.UUID(), nullable=True),
        sa.Column(
            "configuration",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("change_note", sa.String(length=240), nullable=True),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("revision > 0", name="ck_agent_versions_revision"),
        sa.CheckConstraint(
            "jsonb_typeof(configuration) = 'object'",
            name="ck_agent_versions_configuration_object",
        ),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_agent_versions_content_hash",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "agent_id", "legacy_brand_id"],
            ["agents.tenant_id", "agents.id", "agents.legacy_brand_id"],
            name="fk_agent_versions_tenant_agent_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "legacy_brand_id", "business_prompt_version_id"],
            [
                "reply_business_prompt_versions.tenant_id",
                "reply_business_prompt_versions.brand_id",
                "reply_business_prompt_versions.id",
            ],
            name="fk_agent_versions_tenant_prompt_version",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "agent_id",
            "revision",
            name="uq_agent_versions_tenant_agent_revision",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "agent_id",
            "id",
            name="uq_agent_versions_tenant_agent_id",
        ),
    )
    op.create_index(
        "ix_agent_versions_tenant_agent_created",
        "agent_versions",
        ["tenant_id", "agent_id", "created_at"],
    )

    op.create_table(
        "agent_deployments",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("agent_version_id", sa.UUID(), nullable=False),
        sa.Column(
            "environment",
            sa.String(length=16),
            server_default=sa.text("'production'"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("deployed_by", sa.Text(), nullable=False),
        sa.Column(
            "deployed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "environment IN ('production')",
            name="ck_agent_deployments_environment",
        ),
        sa.CheckConstraint("revision > 0", name="ck_agent_deployments_revision"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "agent_id"],
            ["agents.tenant_id", "agents.id"],
            name="fk_agent_deployments_tenant_agent",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "agent_id", "agent_version_id"],
            ["agent_versions.tenant_id", "agent_versions.agent_id", "agent_versions.id"],
            name="fk_agent_deployments_tenant_agent_version",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "agent_id",
            "environment",
            "revision",
            name="uq_agent_deployments_scope_revision",
        ),
    )
    op.create_index(
        "ix_agent_deployments_tenant_agent_deployed",
        "agent_deployments",
        ["tenant_id", "agent_id", "deployed_at"],
    )

    op.execute("SET LOCAL lock_timeout = '30s'")
    op.execute(
        "LOCK TABLE admin_users, platform_accounts, knowledge_documents, "
        "reply_business_prompts, reply_business_prompt_versions IN SHARE MODE"
    )
    bind = op.get_bind()
    scopes = (
        bind.execute(
            sa.text(
                "WITH scopes AS ("
                " SELECT tenant_id, brand_id FROM platform_accounts"
                " UNION SELECT tenant_id, brand_id FROM reply_business_prompts"
                " UNION SELECT tenant_id, brand_id FROM knowledge_documents"
                " UNION SELECT tenant_id, 'default' AS brand_id FROM admin_users"
                ") "
                "SELECT scopes.tenant_id, scopes.brand_id, prompt.active_version_id, "
                "EXISTS (SELECT 1 FROM platform_accounts AS account "
                "WHERE account.tenant_id = scopes.tenant_id "
                "AND account.brand_id = scopes.brand_id) AS has_channel "
                "FROM scopes LEFT JOIN reply_business_prompts AS prompt "
                "ON prompt.tenant_id = scopes.tenant_id "
                "AND prompt.brand_id = scopes.brand_id "
                "ORDER BY scopes.tenant_id, scopes.brand_id"
            )
        )
        .mappings()
        .all()
    )
    for scope in scopes:
        tenant_id = str(scope["tenant_id"])
        brand_id = str(scope["brand_id"])
        if not tenant_id.strip() or len(tenant_id) > 64:
            raise RuntimeError("legacy agent tenant scope cannot be represented safely")
        if not brand_id.strip() or len(brand_id) > 64:
            raise RuntimeError("legacy agent brand scope cannot be represented safely")
        prompt_version_id = scope["active_version_id"]
        agent_id = uuid.uuid4()
        agent_version_id = uuid.uuid4()
        configuration = _snapshot_configuration(
            tenant_id=tenant_id,
            brand_id=brand_id,
            business_prompt_version_id=prompt_version_id,
        )
        bind.execute(
            sa.text(
                "INSERT INTO agents "
                "(id, tenant_id, slug, legacy_brand_id, name, status, created_by) "
                "VALUES (:id, :tenant_id, :slug, :legacy_brand_id, :name, 'active', :actor)"
            ),
            {
                "actor": _MIGRATION_ACTOR,
                "id": agent_id,
                "legacy_brand_id": brand_id,
                "name": _agent_name(brand_id),
                "slug": brand_id,
                "tenant_id": tenant_id,
            },
        )
        bind.execute(
            sa.text(
                "INSERT INTO agent_versions "
                "(id, tenant_id, agent_id, legacy_brand_id, revision, "
                "business_prompt_version_id, configuration, content_hash, change_note, "
                "created_by) VALUES (:id, :tenant_id, :agent_id, :legacy_brand_id, 1, "
                ":business_prompt_version_id, CAST(:configuration AS jsonb), :content_hash, "
                ":change_note, :actor)"
            ),
            {
                "actor": _MIGRATION_ACTOR,
                "agent_id": agent_id,
                "business_prompt_version_id": prompt_version_id,
                "change_note": "Imported legacy tenant and brand scope",
                "configuration": json.dumps(configuration, ensure_ascii=False),
                "content_hash": _configuration_hash(configuration),
                "id": agent_version_id,
                "legacy_brand_id": brand_id,
                "tenant_id": tenant_id,
            },
        )
        if scope["has_channel"]:
            bind.execute(
                sa.text(
                    "INSERT INTO agent_deployments "
                    "(id, tenant_id, agent_id, agent_version_id, environment, revision, "
                    "deployed_by) VALUES (:id, :tenant_id, :agent_id, :agent_version_id, "
                    "'production', 1, :actor)"
                ),
                {
                    "actor": _MIGRATION_ACTOR,
                    "agent_id": agent_id,
                    "agent_version_id": agent_version_id,
                    "id": uuid.uuid4(),
                    "tenant_id": tenant_id,
                },
            )

    op.execute(
        sa.text(
            """
            CREATE FUNCTION guard_agent_control_plane_append_only()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'agent_control_plane_record_is_append_only';
                RETURN OLD;
            END;
            $$ LANGUAGE plpgsql
            """
        )
    )
    op.execute(
        "CREATE TRIGGER trg_agent_versions_append_only "
        "BEFORE UPDATE OR DELETE ON agent_versions "
        "FOR EACH ROW EXECUTE FUNCTION guard_agent_control_plane_append_only()"
    )
    op.execute(
        "CREATE TRIGGER trg_agent_deployments_append_only "
        "BEFORE UPDATE OR DELETE ON agent_deployments "
        "FOR EACH ROW EXECUTE FUNCTION guard_agent_control_plane_append_only()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_agent_deployments_append_only ON agent_deployments")
    op.execute("DROP TRIGGER IF EXISTS trg_agent_versions_append_only ON agent_versions")
    op.execute("DROP FUNCTION IF EXISTS guard_agent_control_plane_append_only()")
    op.drop_index(
        "ix_agent_deployments_tenant_agent_deployed",
        table_name="agent_deployments",
    )
    op.drop_table("agent_deployments")
    op.drop_index("ix_agent_versions_tenant_agent_created", table_name="agent_versions")
    op.drop_table("agent_versions")
    op.drop_index("ix_agents_tenant_status", table_name="agents")
    op.drop_table("agents")
