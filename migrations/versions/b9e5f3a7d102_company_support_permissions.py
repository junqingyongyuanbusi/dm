"""Two business roles, explicit shared inboxes and durable command authority.

Revision ID: b9e5f3a7d102
Revises: a8f4d2c6e901
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "b9e5f3a7d102"
down_revision = "a8f4d2c6e901"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_admin_users_role", "admin_users", type_="check")
    op.create_check_constraint(
        "ck_admin_users_role", "admin_users", "role IN ('USER', 'WORKSPACE_ADMIN')"
    )
    op.add_column(
        "platform_accounts",
        sa.Column("shared_with_support", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "account_reauthorization_grants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("platform_account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("tenant_id", "platform_account_id", "user_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "platform_account_id"],
            ["platform_accounts.tenant_id", "platform_accounts.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["admin_users.tenant_id", "admin_users.id"],
            ondelete="CASCADE",
        ),
    )
    for table in ("provisioning_jobs", "outbox_messages"):
        op.add_column(table, sa.Column("initiator_user_id", postgresql.UUID(as_uuid=True)))
        op.add_column(table, sa.Column("initiator_session_id", postgresql.UUID(as_uuid=True)))
        op.create_foreign_key(
            f"fk_{table}_initiator_user_id", table, "admin_users", ["initiator_user_id"], ["id"]
        )
    op.add_column(
        "provisioning_jobs", sa.Column("target_account_id", postgresql.UUID(as_uuid=True))
    )
    op.create_foreign_key(
        "fk_provisioning_jobs_target_account_id",
        "provisioning_jobs",
        "platform_accounts",
        ["target_account_id"],
        ["id"],
    )
    op.add_column("provisioning_jobs", sa.Column("expected_config_version", sa.Integer()))
    op.add_column("outbox_messages", sa.Column("human_work_item_version", sa.Integer()))
    op.add_column(
        "human_work_items", sa.Column("assigned_session_id", postgresql.UUID(as_uuid=True))
    )
    op.add_column(
        "provisioning_jobs",
        sa.Column("authority_kind", sa.Text(), nullable=False, server_default="UNVERIFIED"),
    )
    op.add_column(
        "provisioning_jobs",
        sa.Column("authority_version", sa.Integer(), nullable=False, server_default="0"),
    )
    # NULL initiators also describe legacy browser/bootstrap jobs. Never infer machine
    # authority from missing data. Advancing the attempt invalidates any old claim.
    op.execute("""
        UPDATE provisioning_jobs
        SET status = 'NEEDS_ACTION', current_step = 'AUTHORITY_RECONFIRM_REQUIRED',
            last_error_code = 'PROVISIONING_AUTHORITY_RECONFIRM_REQUIRED',
            last_error_message = 'Reauthenticate and resubmit; an earlier external attempt may exist.',
            attempt_count = attempt_count + 1, locked_at = NULL, locked_by = NULL,
            next_attempt_at = NULL, staging_secret = NULL, staging_secret_ref = '',
            result = COALESCE(result, '{}'::jsonb) ||
                jsonb_build_object('authority_quarantined', true,
                                   'prior_external_result', 'not_replayed')
        WHERE authority_version = 0
          AND status IN ('PENDING', 'PROCESSING', 'RUNNING', 'FAILED', 'NEEDS_ACTION', 'NEEDS_REVIEW')
    """)
    # Historical command actor strings cannot prove current authority. Keep ambiguous
    # human sends for review rather than guessing a user/session during migration.
    op.execute("""
        UPDATE outbox_messages
        SET status = 'NEEDS_REVIEW', last_error_code = 'HUMAN_AUTHORITY_RECONFIRM_REQUIRED',
            next_attempt_at = NULL
        WHERE actor_kind = 'ADMIN_HUMAN'
          AND status IN ('PENDING', 'FAILED')
    """)
    op.execute("""
        CREATE TEMP TABLE permissions_legacy_work ON COMMIT DROP AS
        SELECT id, tenant_id, conversation_id, assigned_actor, version
        FROM human_work_items WHERE status = 'CLAIMED' AND assigned_user_id IS NULL
    """)
    op.execute("""
        INSERT INTO audit_logs (id, tenant_id, category, actor, action, subject_type, subject_id, detail)
        SELECT gen_random_uuid(), tenant_id, 'permission_migration', 'system:migration',
               'RELEASE_LEGACY_HUMAN_WORK', 'human_work_item', id::text,
               jsonb_build_object('previous_actor', assigned_actor, 'previous_version', version,
                                  'reason', 'UNVERIFIED_ASSIGNEE')
        FROM permissions_legacy_work
    """)
    op.execute("""
        UPDATE human_work_items AS w
        SET status = 'WAITING', assigned_actor = NULL, assigned_user_id = NULL,
            claimed_at = NULL, assigned_session_id = NULL, version = w.version + 1
        FROM permissions_legacy_work AS old WHERE w.id = old.id
    """)
    op.execute("""
        INSERT INTO automation_states
            (conversation_id, state, state_version, resume_policy, state_changed_reason, updated_at)
        SELECT conversation_id, 'HANDOFF_PENDING', 1, 'MANUAL', 'UNVERIFIED_ASSIGNEE', now()
        FROM permissions_legacy_work
        ON CONFLICT (conversation_id) DO UPDATE
        SET state = 'HANDOFF_PENDING', state_version = automation_states.state_version + 1,
            human_agent_id = NULL, resume_policy = 'MANUAL',
            state_changed_reason = 'UNVERIFIED_ASSIGNEE', updated_at = now()
    """)
    op.execute("""
        UPDATE outbox_messages AS o
        SET status = 'CANCELLED', last_error_code = 'UNVERIFIED_ASSIGNEE', next_attempt_at = NULL
        FROM permissions_legacy_work AS old
        WHERE o.conversation_id = old.conversation_id
          AND o.actor_kind = 'BOT' AND o.status IN ('PENDING', 'FAILED')
    """)
    op.execute("""
        UPDATE handoff_notification_intents AS n
        SET desired_card_state = 'WAITING', desired_revision = n.desired_revision + 1,
            action_nonce = gen_random_uuid(), claim_token = NULL,
            claim_expires_at = NULL, sending_revision = NULL,
            status = CASE WHEN n.status = 'SENDING' THEN 'NEEDS_REVIEW'
                          WHEN n.notification_config_id IS NULL THEN 'BLOCKED_CONFIG'
                          ELSE 'PENDING' END,
            last_error_code = 'LEGACY_ASSIGNEE_RECONFIRM_REQUIRED',
            next_attempt_at = NULL, updated_at = now()
        FROM permissions_legacy_work AS old WHERE n.human_work_item_id = old.id
    """)


def downgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM admin_users WHERE role <> 'USER')")):
        raise RuntimeError("Demote business administrators before downgrading permissions schema")
    if connection.scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM platform_accounts WHERE shared_with_support)")
    ):
        raise RuntimeError("Withdraw shared inbox access before downgrading permissions schema")
    op.drop_column("provisioning_jobs", "authority_version")
    op.drop_column("provisioning_jobs", "authority_kind")
    op.drop_column("human_work_items", "assigned_session_id")
    op.drop_column("outbox_messages", "human_work_item_version")
    op.drop_column("provisioning_jobs", "expected_config_version")
    op.drop_constraint(
        "fk_provisioning_jobs_target_account_id", "provisioning_jobs", type_="foreignkey"
    )
    op.drop_column("provisioning_jobs", "target_account_id")
    for table in ("outbox_messages", "provisioning_jobs"):
        op.drop_constraint(f"fk_{table}_initiator_user_id", table, type_="foreignkey")
        op.drop_column(table, "initiator_session_id")
        op.drop_column(table, "initiator_user_id")
    op.drop_table("account_reauthorization_grants")
    op.drop_column("platform_accounts", "shared_with_support")
    op.drop_constraint("ck_admin_users_role", "admin_users", type_="check")
    op.create_check_constraint("ck_admin_users_role", "admin_users", "role = 'USER'")
