"""tenant-scoped knowledge and encrypted-secret readiness

Revision ID: c9e83a4d1f20
Revises: b7d1e4a9c2f3
"""

import sqlalchemy as sa
from alembic import op

revision = "c9e83a4d1f20"
down_revision = "b7d1e4a9c2f3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "knowledge_chunks",
        sa.Column("tenant_id", sa.String(length=64), nullable=False, server_default="default"),
    )
    op.execute(
        """
        UPDATE knowledge_chunks AS chunk
        SET tenant_id = document.tenant_id
        FROM knowledge_documents AS document
        WHERE document.id = chunk.document_id
        """
    )
    op.drop_constraint("knowledge_chunks_content_hash_key", "knowledge_chunks", type_="unique")
    op.create_unique_constraint(
        "uq_knowledge_chunks_tenant_content_hash",
        "knowledge_chunks",
        ["tenant_id", "content_hash"],
    )

    connection = op.get_bind()
    inspector = sa.inspect(connection)
    columns = {
        table: {column["name"] for column in inspector.get_columns(table)}
        for table in ("platform_apps", "platform_accounts", "provisioning_jobs")
    }
    # Released a3 schemas may have dropped these columns. Re-add nullable compatibility
    # columns so one forward migration converges both historical variants.
    compatibility_columns = {
        "platform_apps": {"credential_ref": sa.Text()},
        "platform_accounts": {
            "credential_ref": sa.Text(),
            "webhook_secret_ref": sa.Text(),
        },
        "provisioning_jobs": {"staging_secret_ref": sa.Text()},
    }
    for table, definitions in compatibility_columns.items():
        for name, column_type in definitions.items():
            if name not in columns[table]:
                op.add_column(table, sa.Column(name, column_type, nullable=True))
                columns[table].add(name)

    checks = [
        "credential_bundle IS NULL OR NOT (credential_bundle ? '__encrypted__')",
    ]
    app_where = checks[0]
    account_where = (
        "(COALESCE(config->>'delivery_mode', '') = 'direct' AND "
        "(credential_bundle IS NULL OR NOT (credential_bundle ? '__encrypted__'))) OR "
        "(webhook_secret_bundle IS NOT NULL AND "
        "NOT (webhook_secret_bundle ? '__encrypted__'))"
    )
    staging_where = "staging_secret IS NOT NULL AND NOT (staging_secret ? '__encrypted__')"
    missing = connection.execute(
        sa.text(
            f"""
            SELECT
              (SELECT count(*) FROM platform_apps WHERE {app_where}) +
              (SELECT count(*) FROM platform_accounts WHERE {account_where}) +
              (SELECT count(*) FROM provisioning_jobs WHERE {staging_where})
            """
        )
    ).scalar_one()
    required_webhook_missing = connection.execute(
        sa.text(
            """
            SELECT count(*) FROM platform_accounts
            WHERE status IN ('active', 'CONNECTED')
              AND platform IN ('telegram', 'x')
              AND (webhook_secret_bundle IS NULL
                   OR NOT (webhook_secret_bundle ? '__encrypted__'))
            """
        )
    ).scalar_one()
    if missing or required_webhook_missing:
        raise RuntimeError(
            "legacy secret references remain; run `uv run python scripts/migrate_legacy_secrets.py` "
            "with PLATFORM_SECRET_KEYS configured, then retry the migration"
        )


def downgrade() -> None:
    raise RuntimeError(
        "c9e83a4d1f20 downgrade is intentionally unsupported after tenant-scoped knowledge writes; "
        "restore a pre-upgrade database backup instead"
    )
