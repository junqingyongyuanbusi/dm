"""deduplicate Feishu webhook events at raw ingress

Revision ID: f8a1c3d5e702
Revises: e4b7c2d9a610
"""

import sqlalchemy as sa
from alembic import op


revision = "f8a1c3d5e702"
down_revision = "e4b7c2d9a610"
branch_labels = None
depends_on = None

_INDEX_NAME = "uq_raw_events_feishu_webhook_external_event"
_PREDICATE = "source = 'feishu' AND ingress_kind = 'webhook' AND external_event_id IS NOT NULL"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            _INDEX_NAME,
            "raw_events",
            ["platform_account_id", "external_event_id"],
            unique=True,
            postgresql_where=sa.text(_PREDICATE),
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            _INDEX_NAME,
            table_name="raw_events",
            postgresql_concurrently=True,
        )
