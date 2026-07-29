"""add polling occurrence metadata to raw events

Revision ID: d6b8f0a2c431
Revises: 92a6e3f1c4d8
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "d6b8f0a2c431"
down_revision = "92a6e3f1c4d8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("raw_events", sa.Column("tenant_id", sa.Text(), nullable=True))
    op.add_column("raw_events", sa.Column("platform_account_id", sa.UUID(), nullable=True))
    op.add_column(
        "raw_events",
        sa.Column("ingress_kind", sa.Text(), server_default="webhook", nullable=False),
    )
    op.add_column("raw_events", sa.Column("event_namespace", sa.Text(), nullable=True))
    op.add_column("raw_events", sa.Column("external_event_id", sa.Text(), nullable=True))
    op.add_column("raw_events", sa.Column("external_conversation_id", sa.Text(), nullable=True))
    op.add_column(
        "raw_events",
        sa.Column(
            "context",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "raw_events",
        sa.Column("schema_version", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column("raw_events", sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(
        "fk_raw_events_platform_account_id",
        "raw_events",
        "platform_accounts",
        ["platform_account_id"],
        ["id"],
    )
    op.create_index(
        "ix_raw_events_status_received",
        "raw_events",
        ["processing_status", "received_at"],
    )
    op.create_index(
        "ix_raw_events_account_received",
        "raw_events",
        ["platform_account_id", "received_at"],
    )

    op.add_column(
        "normalized_events",
        sa.Column("external_conversation_id", sa.Text(), nullable=True),
    )
    op.add_column(
        "normalized_events",
        sa.Column(
            "event_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )

    op.execute(
        sa.text(
            """
            CREATE FUNCTION prevent_raw_event_evidence_update()
            RETURNS trigger AS $$
            BEGIN
              IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
                 OR NEW.platform_account_id IS DISTINCT FROM OLD.platform_account_id
                 OR NEW.source IS DISTINCT FROM OLD.source
                 OR NEW.ingress_kind IS DISTINCT FROM OLD.ingress_kind
                 OR NEW.event_namespace IS DISTINCT FROM OLD.event_namespace
                 OR NEW.external_event_id IS DISTINCT FROM OLD.external_event_id
                 OR NEW.external_conversation_id IS DISTINCT FROM OLD.external_conversation_id
                 OR NEW.payload IS DISTINCT FROM OLD.payload
                 OR NEW.headers IS DISTINCT FROM OLD.headers
                 OR NEW.context IS DISTINCT FROM OLD.context
                 OR NEW.schema_version IS DISTINCT FROM OLD.schema_version
                 OR NEW.occurred_at IS DISTINCT FROM OLD.occurred_at
                 OR NEW.received_at IS DISTINCT FROM OLD.received_at THEN
                RAISE EXCEPTION 'raw_event_evidence_is_append_only';
              END IF;
              RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER trg_raw_event_evidence_append_only
            BEFORE UPDATE ON raw_events
            FOR EACH ROW EXECUTE FUNCTION prevent_raw_event_evidence_update();
            """
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DROP TRIGGER IF EXISTS trg_raw_event_evidence_append_only ON raw_events"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS prevent_raw_event_evidence_update"))
    op.drop_column("normalized_events", "event_metadata")
    op.drop_column("normalized_events", "external_conversation_id")
    op.drop_index("ix_raw_events_account_received", table_name="raw_events")
    op.drop_index("ix_raw_events_status_received", table_name="raw_events")
    op.drop_constraint("fk_raw_events_platform_account_id", "raw_events", type_="foreignkey")
    op.drop_column("raw_events", "occurred_at")
    op.drop_column("raw_events", "schema_version")
    op.drop_column("raw_events", "context")
    op.drop_column("raw_events", "external_conversation_id")
    op.drop_column("raw_events", "external_event_id")
    op.drop_column("raw_events", "event_namespace")
    op.drop_column("raw_events", "ingress_kind")
    op.drop_column("raw_events", "platform_account_id")
    op.drop_column("raw_events", "tenant_id")
