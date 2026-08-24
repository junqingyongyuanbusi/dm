"""repair human handoff lifecycle data

Revision ID: a9d4e6f2b713
Revises: f8a1c3d5e702

The downgrade is intentionally a data no-op: the previous inconsistent states cannot be
reconstructed safely after repair.
"""

from alembic import op
import sqlalchemy as sa


revision = "a9d4e6f2b713"
down_revision = "f8a1c3d5e702"
branch_labels = None
depends_on = None


_LOCK_AFFECTED_CONVERSATIONS = sa.text(
    """
    DO $$
    DECLARE
        affected_conversation_id uuid;
    BEGIN
        FOR affected_conversation_id IN
            SELECT conversation_id
            FROM (
                SELECT w.conversation_id
                FROM human_work_items AS w
                JOIN automation_states AS s ON s.conversation_id = w.conversation_id
                WHERE w.status IN ('WAITING', 'CLAIMED')
                  AND s.state IN (
                      'BOT_ACTIVE', 'BOT_DRAFT_ONLY', 'HANDOFF_PENDING', 'HUMAN_ACTIVE'
                  )
                UNION
                SELECT s.conversation_id
                FROM automation_states AS s
                WHERE s.state = 'HANDOFF_PENDING'
                  AND EXISTS (
                      SELECT 1 FROM human_work_items AS resolved
                      WHERE resolved.conversation_id = s.conversation_id
                        AND resolved.status = 'RESOLVED'
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM human_work_items AS open_work
                      WHERE open_work.conversation_id = s.conversation_id
                        AND open_work.status IN ('WAITING', 'CLAIMED')
                  )
            ) AS affected
            ORDER BY conversation_id
        LOOP
            PERFORM pg_advisory_xact_lock(
                hashtextextended(
                    'social-reply:conversation-delivery:' || affected_conversation_id::text,
                    0
                )
            );
        END LOOP;
    END
    $$
    """
)

_LOCK_RESOLVED_PLATFORM_ACCOUNTS = sa.text(
    """
    DO $$
    DECLARE
        affected_account_id uuid;
    BEGIN
        FOR affected_account_id IN
            SELECT a.id
            FROM platform_accounts AS a
            JOIN conversations AS c
              ON c.platform_account_id = a.id
             AND c.tenant_id = a.tenant_id
            JOIN automation_states AS s ON s.conversation_id = c.id
            WHERE s.state = 'HANDOFF_PENDING'
              AND EXISTS (
                  SELECT 1 FROM human_work_items AS resolved
                  WHERE resolved.conversation_id = s.conversation_id
                    AND resolved.tenant_id = c.tenant_id
                    AND resolved.status = 'RESOLVED'
              )
              AND NOT EXISTS (
                  SELECT 1 FROM human_work_items AS open_work
                  WHERE open_work.conversation_id = s.conversation_id
                    AND open_work.tenant_id = c.tenant_id
                    AND open_work.status IN ('WAITING', 'CLAIMED')
              )
            GROUP BY a.id
            ORDER BY a.id
        LOOP
            PERFORM 1
            FROM platform_accounts
            WHERE id = affected_account_id
            FOR UPDATE;
        END LOOP;
    END
    $$
    """
)


_REPAIR_RESOLVED_HANDOFFS = sa.text(
    "UPDATE automation_states AS s "
    "SET state = CASE "
    "WHEN a.platform IN ('facebook', 'instagram') "
    "AND a.automation_default = 'BOT_ACTIVE' THEN 'BOT_DRAFT_ONLY' "
    "WHEN a.automation_default IN ('BOT_ACTIVE', 'BOT_DRAFT_ONLY') "
    "THEN a.automation_default "
    "ELSE 'BOT_DRAFT_ONLY' END, "
    "state_version = s.state_version + 1, "
    "human_agent_id = NULL, "
    "state_changed_reason = 'migration_resolved_work_account_policy' "
    "FROM conversations AS c "
    "JOIN platform_accounts AS a ON a.id = c.platform_account_id "
    "WHERE c.id = s.conversation_id "
    "AND a.tenant_id = c.tenant_id "
    "AND s.state = 'HANDOFF_PENDING' "
    "AND EXISTS ("
    "SELECT 1 FROM human_work_items AS resolved "
    "WHERE resolved.conversation_id = s.conversation_id "
    "AND resolved.tenant_id = c.tenant_id "
    "AND resolved.status = 'RESOLVED'"
    ") "
    "AND NOT EXISTS ("
    "SELECT 1 FROM human_work_items AS open_work "
    "WHERE open_work.conversation_id = s.conversation_id "
    "AND open_work.tenant_id = c.tenant_id "
    "AND open_work.status IN ('WAITING', 'CLAIMED')"
    ")"
)


def upgrade() -> None:
    op.execute(_LOCK_AFFECTED_CONVERSATIONS)
    op.execute(_LOCK_RESOLVED_PLATFORM_ACCOUNTS)
    op.execute(
        sa.text(
            "UPDATE automation_states AS s "
            "SET state = 'HUMAN_ACTIVE', "
            "state_version = s.state_version + 1, "
            "human_agent_id = w.assigned_actor, "
            "state_changed_reason = 'migration_claimed_work_human_active' "
            "FROM human_work_items AS w "
            "WHERE w.conversation_id = s.conversation_id "
            "AND w.status = 'CLAIMED' "
            "AND s.state IN ('HANDOFF_PENDING', 'BOT_ACTIVE', 'BOT_DRAFT_ONLY', 'HUMAN_ACTIVE') "
            "AND (s.state <> 'HUMAN_ACTIVE' "
            "OR s.human_agent_id IS DISTINCT FROM w.assigned_actor)"
        )
    )
    op.execute(
        sa.text(
            "UPDATE automation_states AS s "
            "SET state = 'HANDOFF_PENDING', "
            "state_version = s.state_version + 1, "
            "human_agent_id = NULL, "
            "state_changed_reason = 'migration_waiting_work_handoff_pending' "
            "FROM human_work_items AS w "
            "WHERE w.conversation_id = s.conversation_id "
            "AND w.status = 'WAITING' "
            "AND s.state IN ('BOT_ACTIVE', 'BOT_DRAFT_ONLY')"
        )
    )
    op.execute(_REPAIR_RESOLVED_HANDOFFS)
    op.execute(
        sa.text(
            "UPDATE outbox_messages AS o "
            "SET status = 'CANCELLED', last_error_code = 'TAKEOVER' "
            "WHERE o.status IN ('PENDING', 'FAILED') "
            "AND o.actor_kind = 'BOT' "
            "AND o.origin_kind = 'DECISION' "
            "AND EXISTS ("
            "SELECT 1 FROM human_work_items AS w "
            "WHERE w.conversation_id = o.conversation_id "
            "AND w.status IN ('WAITING', 'CLAIMED')"
            ")"
        )
    )


def downgrade() -> None:
    """Data no-op: repaired lifecycle state is irreversible without historical intent."""
