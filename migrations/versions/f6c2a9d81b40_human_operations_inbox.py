"""human operations inbox and outbox provenance

Revision ID: f6c2a9d81b40
Revises: a1c4e8b7f302
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "f6c2a9d81b40"
down_revision = "a1c4e8b7f302"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column(
            "attachments",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )

    op.create_table(
        "human_work_items",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("conversation_id", sa.UUID(), nullable=False),
        sa.Column("status", sa.Text(), server_default="WAITING", nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=False),
        sa.Column("priority", sa.Integer(), server_default="0", nullable=False),
        sa.Column("assigned_user_id", sa.UUID(), nullable=True),
        sa.Column("assigned_actor", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.CheckConstraint(
            "status IN ('WAITING', 'CLAIMED', 'RESOLVED', 'CANCELLED')",
            name="ck_human_work_items_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_human_work_items_version"),
        sa.ForeignKeyConstraint(["assigned_user_id"], ["admin_users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_human_work_items_open_conversation",
        "human_work_items",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('WAITING', 'CLAIMED')"),
    )
    op.create_index(
        "ix_human_work_items_tenant_status_created",
        "human_work_items",
        ["tenant_id", "status", "created_at"],
    )
    op.create_index(
        "ix_human_work_items_assigned_status",
        "human_work_items",
        ["assigned_user_id", "status"],
    )
    op.execute(
        sa.text(
            "INSERT INTO human_work_items "
            "(id, tenant_id, conversation_id, status, reason_code, assigned_actor, "
            "created_at, claimed_at, version) "
            "SELECT gen_random_uuid(), c.tenant_id, s.conversation_id, "
            "CASE WHEN s.state = 'HUMAN_ACTIVE' THEN 'CLAIMED' ELSE 'WAITING' END, "
            "COALESCE(NULLIF(s.state_changed_reason, ''), 'LEGACY_HANDOFF'), "
            "s.human_agent_id, s.updated_at, "
            "CASE WHEN s.state = 'HUMAN_ACTIVE' THEN s.updated_at ELSE NULL END, 1 "
            "FROM automation_states s "
            "JOIN conversations c ON c.id = s.conversation_id "
            "WHERE s.state IN ('HANDOFF_PENDING', 'HUMAN_ACTIVE')"
        )
    )

    op.add_column("outbox_messages", sa.Column("reply_to_message_id", sa.UUID()))
    op.add_column(
        "outbox_messages",
        sa.Column("origin_kind", sa.Text(), server_default="DECISION", nullable=False),
    )
    op.add_column(
        "outbox_messages",
        sa.Column("actor_kind", sa.Text(), server_default="BOT", nullable=False),
    )
    op.add_column("outbox_messages", sa.Column("actor_id", sa.Text()))
    op.create_foreign_key(
        "fk_outbox_messages_reply_to_message_id",
        "outbox_messages",
        "messages",
        ["reply_to_message_id"],
        ["id"],
    )
    op.create_check_constraint(
        "ck_outbox_origin_kind",
        "outbox_messages",
        "origin_kind IN ('DECISION', 'DRAFT_APPROVAL', 'MANUAL_REPLY', 'SYSTEM_NOTICE')",
    )
    op.create_check_constraint(
        "ck_outbox_actor_kind",
        "outbox_messages",
        "actor_kind IN ('BOT', 'ADMIN_HUMAN', 'SYSTEM')",
    )
    op.execute(
        sa.text(
            "UPDATE outbox_messages o SET reply_to_message_id = d.message_id "
            "FROM reply_decisions d WHERE d.outbox_id = o.id"
        )
    )
    op.execute(
        sa.text(
            "UPDATE outbox_messages SET origin_kind = 'DRAFT_APPROVAL', "
            "actor_kind = 'ADMIN_HUMAN', actor_id = payload->>'approved_by' "
            "WHERE payload->>'approval' = 'admin'"
        )
    )

    op.add_column("reply_decisions", sa.Column("original_reply_text", sa.Text()))
    op.add_column("reply_decisions", sa.Column("final_reply_text", sa.Text()))
    op.add_column("reply_decisions", sa.Column("review_action", sa.Text()))
    op.add_column("reply_decisions", sa.Column("reviewed_by", sa.Text()))
    op.add_column("reply_decisions", sa.Column("reviewed_at", sa.DateTime(timezone=True)))
    op.add_column("reply_decisions", sa.Column("review_reason", sa.Text()))
    op.execute(
        sa.text(
            "UPDATE reply_decisions SET original_reply_text = reply_text "
            "WHERE reply_text IS NOT NULL"
        )
    )
    op.execute(
        sa.text(
            "UPDATE reply_decisions SET review_action = CASE "
            "WHEN reason_codes @> '[\"ADMIN_DISCARDED\"]'::jsonb THEN 'REJECTED' "
            "WHEN outbox_id IS NOT NULL THEN 'ACCEPTED' ELSE 'PENDING' END "
            "WHERE action = 'draft'"
        )
    )


def downgrade() -> None:
    op.drop_column("reply_decisions", "review_reason")
    op.drop_column("reply_decisions", "reviewed_at")
    op.drop_column("reply_decisions", "reviewed_by")
    op.drop_column("reply_decisions", "review_action")
    op.drop_column("reply_decisions", "final_reply_text")
    op.drop_column("reply_decisions", "original_reply_text")

    op.drop_constraint("ck_outbox_actor_kind", "outbox_messages", type_="check")
    op.drop_constraint("ck_outbox_origin_kind", "outbox_messages", type_="check")
    op.drop_constraint(
        "fk_outbox_messages_reply_to_message_id", "outbox_messages", type_="foreignkey"
    )
    op.drop_column("outbox_messages", "actor_id")
    op.drop_column("outbox_messages", "actor_kind")
    op.drop_column("outbox_messages", "origin_kind")
    op.drop_column("outbox_messages", "reply_to_message_id")

    op.drop_index("ix_human_work_items_assigned_status", table_name="human_work_items")
    op.drop_index("ix_human_work_items_tenant_status_created", table_name="human_work_items")
    op.drop_index("uq_human_work_items_open_conversation", table_name="human_work_items")
    op.drop_table("human_work_items")
    op.drop_column("messages", "attachments")
