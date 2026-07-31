"""harden human work tenant scope

Revision ID: b8e1d4f7a2c3
Revises: f6c2a9d81b40
"""

from alembic import op
import sqlalchemy as sa


revision = "b8e1d4f7a2c3"
down_revision = "f6c2a9d81b40"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE human_work_items w SET tenant_id = c.tenant_id "
            "FROM conversations c "
            "WHERE c.id = w.conversation_id AND w.tenant_id <> c.tenant_id"
        )
    )
    op.execute(
        sa.text(
            "UPDATE human_work_items SET status = 'WAITING', claimed_at = NULL, "
            "version = version + 1 "
            "WHERE status = 'CLAIMED' "
            "AND (assigned_actor IS NULL OR claimed_at IS NULL)"
        )
    )
    op.create_unique_constraint(
        "uq_conversations_tenant_id_id",
        "conversations",
        ["tenant_id", "id"],
    )
    op.drop_constraint(
        "human_work_items_conversation_id_fkey",
        "human_work_items",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_human_work_items_tenant_conversation",
        "human_work_items",
        "conversations",
        ["tenant_id", "conversation_id"],
        ["tenant_id", "id"],
        ondelete="CASCADE",
    )
    op.create_check_constraint(
        "ck_human_work_items_claimed_assignment",
        "human_work_items",
        "status <> 'CLAIMED' OR (assigned_actor IS NOT NULL AND claimed_at IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_human_work_items_claimed_assignment",
        "human_work_items",
        type_="check",
    )
    op.drop_constraint(
        "fk_human_work_items_tenant_conversation",
        "human_work_items",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "human_work_items_conversation_id_fkey",
        "human_work_items",
        "conversations",
        ["conversation_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_constraint(
        "uq_conversations_tenant_id_id",
        "conversations",
        type_="unique",
    )
