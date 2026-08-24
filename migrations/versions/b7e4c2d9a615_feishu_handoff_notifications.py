"""add durable Feishu handoff notifications

Revision ID: b7e4c2d9a615
Revises: d3f6a1b8c904
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "b7e4c2d9a615"
down_revision = "d3f6a1b8c904"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_admin_users_tenant_id_id",
        "admin_users",
        ["tenant_id", "id"],
    )
    op.create_unique_constraint(
        "uq_platform_accounts_tenant_id_id",
        "platform_accounts",
        ["tenant_id", "id"],
    )
    op.create_unique_constraint(
        "uq_human_work_items_tenant_id_id",
        "human_work_items",
        ["tenant_id", "id"],
    )
    op.add_column("human_work_items", sa.Column("resolved_actor", sa.Text(), nullable=True))
    op.add_column(
        "human_work_items",
        sa.Column("resolution_evidence", sa.Text(), nullable=True),
    )
    op.add_column(
        "human_work_items",
        sa.Column(
            "resolution_outbox_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "ck_human_work_items_resolution_evidence",
        "human_work_items",
        "resolution_evidence IS NULL OR resolution_evidence IN "
        "('REPLY_CORE_CONFIRMED', 'FEISHU_OPERATOR_ATTESTED', "
        "'ADMIN_OPERATOR_ATTESTED', 'SUPERVISOR_OVERRIDE')",
    )
    op.create_foreign_key(
        "fk_human_work_items_resolution_outbox_id",
        "human_work_items",
        "outbox_messages",
        ["resolution_outbox_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "tenant_feishu_handoff_configs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column(
            "feishu_platform_account_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("destination_chat_id", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("config_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("card_locale", sa.Text(), server_default=sa.text("'zh_cn'"), nullable=False),
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
        sa.CheckConstraint(
            "config_version >= 1",
            name="ck_feishu_handoff_configs_version",
        ),
        sa.CheckConstraint(
            "length(btrim(destination_chat_id)) > 0",
            name="ck_feishu_handoff_configs_chat_id",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "feishu_platform_account_id"],
            ["platform_accounts.tenant_id", "platform_accounts.id"],
            name="fk_feishu_handoff_configs_tenant_account",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_tenant_feishu_handoff_configs_tenant_id_id",
        ),
    )

    op.create_table(
        "feishu_handoff_operators",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column(
            "feishu_platform_account_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("operator_open_id", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("admin_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("can_claim", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("can_resolve", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'ACTIVE'"), nullable=False),
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
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'DISABLED')",
            name="ck_feishu_handoff_operators_status",
        ),
        sa.CheckConstraint(
            "length(btrim(operator_open_id)) > 0",
            name="ck_feishu_handoff_operators_open_id",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "feishu_platform_account_id"],
            ["platform_accounts.tenant_id", "platform_accounts.id"],
            name="fk_feishu_handoff_operators_tenant_account",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "admin_user_id"],
            ["admin_users.tenant_id", "admin_users.id"],
            name="fk_feishu_handoff_operators_tenant_admin_user",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "feishu_platform_account_id",
            "operator_open_id",
            name="uq_feishu_handoff_operators_account_open_id",
        ),
    )
    op.create_index(
        "ix_feishu_handoff_operators_tenant_status",
        "feishu_handoff_operators",
        ["tenant_id", "status"],
    )

    op.create_table(
        "handoff_notification_intents",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("public_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("human_work_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("notification_config_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("config_version", sa.Integer(), nullable=True),
        sa.Column(
            "feishu_platform_account_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("destination_chat_id", sa.Text(), nullable=True),
        sa.Column("provider_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider_message_id", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Text(),
            server_default=sa.text("'BLOCKED_CONFIG'"),
            nullable=False,
        ),
        sa.Column(
            "desired_card_state",
            sa.Text(),
            server_default=sa.text("'WAITING'"),
            nullable=False,
        ),
        sa.Column("desired_revision", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("delivered_revision", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("action_nonce", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sending_revision", sa.Integer(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.Text(), nullable=True),
        sa.Column("last_error_message", sa.Text(), nullable=True),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
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
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('BLOCKED_CONFIG', 'PENDING', 'SENDING', 'SYNCED', "
            "'FAILED', 'NEEDS_REVIEW', 'CANCELLED')",
            name="ck_handoff_notification_intents_status",
        ),
        sa.CheckConstraint(
            "desired_card_state IN ('WAITING', 'CLAIMED', 'RESOLVED', 'CANCELLED')",
            name="ck_handoff_notification_intents_card_state",
        ),
        sa.CheckConstraint(
            "desired_revision >= 1 AND delivered_revision >= 0 "
            "AND delivered_revision <= desired_revision",
            name="ck_handoff_notification_intents_revisions",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_handoff_notification_intents_attempt_count",
        ),
        sa.CheckConstraint(
            "(status = 'SENDING') = "
            "(claim_token IS NOT NULL AND claim_expires_at IS NOT NULL "
            "AND sending_revision IS NOT NULL)",
            name="ck_handoff_notification_intents_sending_lease",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "human_work_item_id"],
            ["human_work_items.tenant_id", "human_work_items.id"],
            name="fk_handoff_notification_intents_tenant_work",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.id"],
            name="fk_handoff_notification_intents_tenant_conversation",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "notification_config_id"],
            ["tenant_feishu_handoff_configs.tenant_id", "tenant_feishu_handoff_configs.id"],
            name="fk_handoff_notification_intents_tenant_config",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "feishu_platform_account_id"],
            ["platform_accounts.tenant_id", "platform_accounts.id"],
            name="fk_handoff_notification_intents_tenant_account",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id"),
        sa.UniqueConstraint("human_work_item_id"),
        sa.UniqueConstraint("provider_uuid"),
        sa.UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_handoff_notification_intents_tenant_id_id",
        ),
    )
    op.create_index(
        "ix_handoff_notification_intents_due",
        "handoff_notification_intents",
        ["status", "next_attempt_at", "created_at"],
    )
    op.create_index(
        "ix_handoff_notification_intents_tenant_status",
        "handoff_notification_intents",
        ["tenant_id", "status", "created_at"],
    )
    op.create_index(
        "ix_handoff_notification_intents_provider_message",
        "handoff_notification_intents",
        ["provider_message_id"],
    )

    op.create_table(
        "feishu_card_action_receipts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column(
            "feishu_platform_account_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("provider_event_id", sa.Text(), nullable=False),
        sa.Column("notification_intent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("operator_open_id", sa.Text(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "outcome",
            sa.Text(),
            server_default=sa.text("'PROCESSING'"),
            nullable=False,
        ),
        sa.Column(
            "response_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "action IN ('CLAIM', 'RESOLVE')",
            name="ck_feishu_card_action_receipts_action",
        ),
        sa.CheckConstraint(
            "outcome IN ('PROCESSING', 'SUCCEEDED', 'CONFLICT', 'UNAUTHORIZED', 'MAINTENANCE')",
            name="ck_feishu_card_action_receipts_outcome",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "feishu_platform_account_id"],
            ["platform_accounts.tenant_id", "platform_accounts.id"],
            name="fk_feishu_card_action_receipts_tenant_account",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "notification_intent_id"],
            ["handoff_notification_intents.tenant_id", "handoff_notification_intents.id"],
            name="fk_feishu_card_action_receipts_tenant_intent",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "feishu_platform_account_id",
            "provider_event_id",
            name="uq_feishu_card_action_receipts_account_event",
        ),
    )
    op.create_index(
        "ix_feishu_card_action_receipts_tenant_created",
        "feishu_card_action_receipts",
        ["tenant_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_feishu_card_action_receipts_tenant_created",
        table_name="feishu_card_action_receipts",
    )
    op.drop_table("feishu_card_action_receipts")

    op.drop_index(
        "ix_handoff_notification_intents_provider_message",
        table_name="handoff_notification_intents",
    )
    op.drop_index(
        "ix_handoff_notification_intents_tenant_status",
        table_name="handoff_notification_intents",
    )
    op.drop_index(
        "ix_handoff_notification_intents_due",
        table_name="handoff_notification_intents",
    )
    op.drop_table("handoff_notification_intents")

    op.drop_index(
        "ix_feishu_handoff_operators_tenant_status",
        table_name="feishu_handoff_operators",
    )
    op.drop_table("feishu_handoff_operators")
    op.drop_table("tenant_feishu_handoff_configs")

    op.drop_constraint(
        "fk_human_work_items_resolution_outbox_id",
        "human_work_items",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_human_work_items_resolution_evidence",
        "human_work_items",
        type_="check",
    )
    op.drop_column("human_work_items", "resolution_outbox_id")
    op.drop_column("human_work_items", "resolution_evidence")
    op.drop_column("human_work_items", "resolved_actor")
    op.drop_constraint(
        "uq_human_work_items_tenant_id_id",
        "human_work_items",
        type_="unique",
    )
    op.drop_constraint(
        "uq_platform_accounts_tenant_id_id",
        "platform_accounts",
        type_="unique",
    )
    op.drop_constraint(
        "uq_admin_users_tenant_id_id",
        "admin_users",
        type_="unique",
    )
