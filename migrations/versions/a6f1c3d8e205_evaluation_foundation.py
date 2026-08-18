"""add isolated evaluation foundation

Revision ID: a6f1c3d8e205
Revises: f3b8c1d4e726
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "a6f1c3d8e205"
down_revision = "f3b8c1d4e726"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "evaluation_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("data_class", sa.String(length=32), nullable=False),
        sa.Column("dataset_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("dataset_version", sa.String(length=128), nullable=False),
        sa.Column("source_token_key_version", sa.String(length=64), nullable=False),
        sa.Column("candidate_manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("workload_manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("result_set_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("execution_policy_version", sa.String(length=64), nullable=False),
        sa.Column("execution_policy_hash", sa.String(length=64), nullable=False),
        sa.Column("code_revision", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'RUNNING'"),
            nullable=False,
        ),
        sa.Column("expected_decision_count", sa.Integer(), nullable=False),
        sa.Column("retention_class", sa.String(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('RUNNING', 'COMPLETED', 'FAILED')",
            name="ck_evaluation_runs_status",
        ),
        sa.CheckConstraint(
            "(status = 'RUNNING') = (completed_at IS NULL)",
            name="ck_evaluation_runs_completed_at",
        ),
        sa.CheckConstraint(
            "(status = 'RUNNING') = (result_set_fingerprint IS NULL)",
            name="ck_evaluation_runs_result_fingerprint",
        ),
        sa.CheckConstraint(
            "data_class = 'SYNTHETIC'",
            name="ck_evaluation_runs_data_class",
        ),
        sa.CheckConstraint(
            "dataset_fingerprint ~ '^[0-9a-f]{64}$' "
            "AND candidate_manifest_hash ~ '^[0-9a-f]{64}$' "
            "AND workload_manifest_hash ~ '^[0-9a-f]{64}$' "
            "AND execution_policy_hash ~ '^[0-9a-f]{64}$' "
            "AND (result_set_fingerprint IS NULL OR "
            "result_set_fingerprint ~ '^[0-9a-f]{64}$') "
            "AND length(btrim(dataset_version)) > 0 "
            "AND length(btrim(source_token_key_version)) > 0 "
            "AND length(btrim(execution_policy_version)) > 0 "
            "AND length(btrim(code_revision)) > 0",
            name="ck_evaluation_runs_manifest",
        ),
        sa.CheckConstraint(
            "expected_decision_count >= 1",
            name="ck_evaluation_runs_expected_count",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_evaluation_runs_tenant_id_id",
        ),
    )
    op.create_index(
        "ix_evaluation_runs_tenant_status_created",
        "evaluation_runs",
        ["tenant_id", "status", "created_at"],
    )

    op.create_table(
        "evaluation_decisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("evaluation_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_message_token", sa.String(length=64), nullable=False),
        sa.Column("scenario_id", sa.String(length=64), nullable=False),
        sa.Column("candidate_contract_id", sa.String(length=128), nullable=False),
        sa.Column("candidate_contract_version", sa.String(length=64), nullable=False),
        sa.Column("candidate_contract_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "candidate_contract_manifest",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("task_kind", sa.String(length=16), nullable=False),
        sa.Column("result_schema_version", sa.String(length=64), nullable=False),
        sa.Column("delivery_surface", sa.String(length=16), nullable=True),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'PENDING'"),
            nullable=False,
        ),
        sa.Column("action", sa.String(length=16), nullable=True),
        sa.Column("reply_text_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "reason_codes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("result_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        sa.Column("estimated_cost_usd", sa.Float(), nullable=True),
        sa.Column("model_invocation_count", sa.Integer(), nullable=True),
        sa.Column("input_token_count", sa.Integer(), nullable=True),
        sa.Column("output_token_count", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("result_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("claim_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED')",
            name="ck_evaluation_decisions_status",
        ),
        sa.CheckConstraint(
            "(status IN ('PENDING', 'RUNNING')) = (completed_at IS NULL)",
            name="ck_evaluation_decisions_completed_at",
        ),
        sa.CheckConstraint(
            "task_kind IN ('retrieval', 'language', 'action', 'rendering', 'e2e')",
            name="ck_evaluation_decisions_task_kind",
        ),
        sa.CheckConstraint(
            "((task_kind IN ('rendering', 'e2e')) "
            "AND delivery_surface IN ('chatwoot', 'direct')) OR "
            "((task_kind NOT IN ('rendering', 'e2e')) AND delivery_surface IS NULL)",
            name="ck_evaluation_decisions_delivery_surface",
        ),
        sa.CheckConstraint(
            "action IS NULL OR action IN ('auto_reply', 'draft', 'handoff', 'ignore')",
            name="ck_evaluation_decisions_action",
        ),
        sa.CheckConstraint(
            "source_message_token ~ '^[0-9a-f]{64}$' "
            "AND scenario_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$' "
            "AND input_fingerprint ~ '^[0-9a-f]{64}$' "
            "AND candidate_contract_hash ~ '^[0-9a-f]{64}$' "
            "AND (reply_text_hash IS NULL OR reply_text_hash ~ '^[0-9a-f]{64}$') "
            "AND (result_fingerprint IS NULL OR result_fingerprint ~ '^[0-9a-f]{64}$') "
            "AND length(btrim(result_schema_version)) > 0",
            name="ck_evaluation_decisions_hashes",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(candidate_contract_manifest) = 'object' "
            "AND candidate_contract_manifest ?& "
            "ARRAY['contract_id','version','contract_hash','task_kind',"
            "'result_schema_version','execution_mode'] "
            "AND candidate_contract_manifest ->> 'contract_id' = candidate_contract_id "
            "AND candidate_contract_manifest ->> 'version' = candidate_contract_version "
            "AND candidate_contract_manifest ->> 'contract_hash' = candidate_contract_hash "
            "AND candidate_contract_manifest ->> 'task_kind' = task_kind "
            "AND candidate_contract_manifest ->> 'result_schema_version' = "
            "result_schema_version",
            name="ck_evaluation_decisions_contract_manifest",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_evaluation_decisions_attempt_count",
        ),
        sa.CheckConstraint(
            "COALESCE(model_invocation_count, 0) >= 0 "
            "AND COALESCE(input_token_count, 0) >= 0 "
            "AND COALESCE(output_token_count, 0) >= 0",
            name="ck_evaluation_decisions_execution_counts",
        ),
        sa.CheckConstraint(
            "(status = 'RUNNING') = (claim_token IS NOT NULL AND claim_expires_at IS NOT NULL)",
            name="ck_evaluation_decisions_running_lease",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'RUNNING') OR result_fingerprint IS NOT NULL",
            name="ck_evaluation_decisions_terminal_result",
        ),
        sa.CheckConstraint(
            "status <> 'SUCCEEDED' OR (result_payload IS NOT NULL "
            "AND error_code IS NULL AND error_detail IS NULL)",
            name="ck_evaluation_decisions_success_payload",
        ),
        sa.CheckConstraint(
            "status <> 'SUCCEEDED' OR ("
            "jsonb_typeof(result_payload) = 'object' "
            "AND jsonb_typeof(result_payload -> 'execution') = 'object' "
            "AND (result_payload -> 'execution') ?& "
            "ARRAY['estimated_cost_usd','input_token_count',"
            "'model_invocation_count','output_token_count'] "
            "AND latency_ms IS NOT NULL AND latency_ms >= 0 "
            "AND latency_ms < 'Infinity'::double precision "
            "AND estimated_cost_usd IS NOT NULL AND estimated_cost_usd >= 0 "
            "AND estimated_cost_usd < 'Infinity'::double precision "
            "AND model_invocation_count IS NOT NULL "
            "AND input_token_count IS NOT NULL AND output_token_count IS NOT NULL "
            "AND ((task_kind = 'retrieval' "
            "AND result_payload ? 'ranked_candidates' "
            "AND jsonb_typeof(result_payload -> 'ranked_candidates') = 'array') "
            "OR (task_kind = 'language' "
            "AND result_payload ?& ARRAY['locale','confidence','unknown','source'] "
            "AND jsonb_typeof(result_payload -> 'locale') = 'string' "
            "AND jsonb_typeof(result_payload -> 'confidence') = 'number' "
            "AND (result_payload ->> 'confidence')::double precision BETWEEN 0 AND 1 "
            "AND jsonb_typeof(result_payload -> 'unknown') = 'boolean' "
            "AND jsonb_typeof(result_payload -> 'source') = 'string') "
            "OR (task_kind = 'action' "
            "AND result_payload ?& ARRAY['action','reason_codes'] "
            "AND jsonb_typeof(result_payload -> 'action') = 'string' "
            "AND jsonb_typeof(result_payload -> 'reason_codes') = 'array') "
            "OR (task_kind = 'rendering' "
            "AND result_payload ?& ARRAY['reply_text_hash','locale','guard_passed'] "
            "AND jsonb_typeof(result_payload -> 'reply_text_hash') = 'string' "
            "AND jsonb_typeof(result_payload -> 'locale') = 'string' "
            "AND jsonb_typeof(result_payload -> 'guard_passed') = 'boolean') "
            "OR (task_kind = 'e2e' "
            "AND result_payload ?& ARRAY['action','locale','reason_codes'] "
            "AND jsonb_typeof(result_payload -> 'action') = 'string' "
            "AND jsonb_typeof(result_payload -> 'locale') = 'string' "
            "AND jsonb_typeof(result_payload -> 'reason_codes') = 'array')))",
            name="ck_evaluation_decisions_typed_payload",
        ),
        sa.CheckConstraint(
            "status <> 'SUCCEEDED' OR task_kind <> 'e2e' OR "
            "((action IN ('auto_reply','draft') AND reply_text_hash IS NOT NULL) OR "
            "(action IN ('handoff','ignore') AND reply_text_hash IS NULL))",
            name="ck_evaluation_decisions_e2e_reply",
        ),
        sa.CheckConstraint(
            "status <> 'SUCCEEDED' OR ("
            "action IS NOT DISTINCT FROM (result_payload ->> 'action') "
            "AND reason_codes = COALESCE(result_payload -> 'reason_codes', '[]'::jsonb) "
            "AND reply_text_hash IS NOT DISTINCT FROM (result_payload ->> 'reply_text_hash') "
            "AND estimated_cost_usd IS NOT DISTINCT FROM "
            "((result_payload #>> '{execution,estimated_cost_usd}')::double precision) "
            "AND model_invocation_count IS NOT DISTINCT FROM "
            "((result_payload #>> '{execution,model_invocation_count}')::integer) "
            "AND input_token_count IS NOT DISTINCT FROM "
            "((result_payload #>> '{execution,input_token_count}')::integer) "
            "AND output_token_count IS NOT DISTINCT FROM "
            "((result_payload #>> '{execution,output_token_count}')::integer))",
            name="ck_evaluation_decisions_result_projection",
        ),
        sa.CheckConstraint(
            "status <> 'FAILED' OR (error_code IS NOT NULL "
            "AND result_payload IS NULL AND action IS NULL AND reply_text_hash IS NULL "
            "AND reason_codes = '[]'::jsonb AND estimated_cost_usd IS NULL "
            "AND model_invocation_count IS NULL AND input_token_count IS NULL "
            "AND output_token_count IS NULL)",
            name="ck_evaluation_decisions_failure_payload",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "evaluation_run_id"],
            ["evaluation_runs.tenant_id", "evaluation_runs.id"],
            name="fk_evaluation_decisions_tenant_run",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_evaluation_decisions_tenant_id_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "evaluation_run_id",
            "source_message_token",
            "scenario_id",
            "candidate_contract_id",
            name="uq_evaluation_decisions_run_source_candidate",
        ),
    )
    op.create_index(
        "ix_evaluation_decisions_run_status_created",
        "evaluation_decisions",
        ["evaluation_run_id", "status", "created_at"],
    )
    op.create_index(
        "ix_evaluation_decisions_status_claim_expiry",
        "evaluation_decisions",
        ["status", "claim_expires_at"],
    )

    op.execute(
        """
        CREATE FUNCTION guard_evaluation_run_immutable() RETURNS trigger AS $$
        BEGIN
          IF OLD.tenant_id IS DISTINCT FROM NEW.tenant_id
             OR OLD.name IS DISTINCT FROM NEW.name
             OR OLD.data_class IS DISTINCT FROM NEW.data_class
             OR OLD.dataset_fingerprint IS DISTINCT FROM NEW.dataset_fingerprint
             OR OLD.dataset_version IS DISTINCT FROM NEW.dataset_version
             OR OLD.source_token_key_version IS DISTINCT FROM NEW.source_token_key_version
             OR OLD.candidate_manifest_hash IS DISTINCT FROM NEW.candidate_manifest_hash
             OR OLD.workload_manifest_hash IS DISTINCT FROM NEW.workload_manifest_hash
             OR OLD.execution_policy_version IS DISTINCT FROM NEW.execution_policy_version
             OR OLD.execution_policy_hash IS DISTINCT FROM NEW.execution_policy_hash
             OR OLD.code_revision IS DISTINCT FROM NEW.code_revision
             OR OLD.expected_decision_count IS DISTINCT FROM NEW.expected_decision_count
             OR OLD.retention_class IS DISTINCT FROM NEW.retention_class
             OR OLD.expires_at IS DISTINCT FROM NEW.expires_at
          THEN
            RAISE EXCEPTION 'evaluation run manifest is immutable';
          END IF;
          IF OLD.status IN ('COMPLETED', 'FAILED') AND NEW IS DISTINCT FROM OLD THEN
            RAISE EXCEPTION 'terminal evaluation run is immutable';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_evaluation_runs_immutable
        BEFORE UPDATE ON evaluation_runs
        FOR EACH ROW EXECUTE FUNCTION guard_evaluation_run_immutable()
        """
    )
    op.execute(
        """
        CREATE FUNCTION guard_evaluation_decision_immutable() RETURNS trigger AS $$
        BEGIN
          IF OLD.tenant_id IS DISTINCT FROM NEW.tenant_id
             OR OLD.evaluation_run_id IS DISTINCT FROM NEW.evaluation_run_id
             OR OLD.source_message_token IS DISTINCT FROM NEW.source_message_token
             OR OLD.scenario_id IS DISTINCT FROM NEW.scenario_id
             OR OLD.candidate_contract_id IS DISTINCT FROM NEW.candidate_contract_id
             OR OLD.candidate_contract_version IS DISTINCT FROM NEW.candidate_contract_version
             OR OLD.candidate_contract_hash IS DISTINCT FROM NEW.candidate_contract_hash
             OR OLD.candidate_contract_manifest IS DISTINCT FROM NEW.candidate_contract_manifest
             OR OLD.task_kind IS DISTINCT FROM NEW.task_kind
             OR OLD.result_schema_version IS DISTINCT FROM NEW.result_schema_version
             OR OLD.delivery_surface IS DISTINCT FROM NEW.delivery_surface
             OR OLD.input_fingerprint IS DISTINCT FROM NEW.input_fingerprint
          THEN
            RAISE EXCEPTION 'evaluation workload identity is immutable';
          END IF;
          IF OLD.status IN ('SUCCEEDED', 'FAILED') AND NEW IS DISTINCT FROM OLD THEN
            RAISE EXCEPTION 'terminal evaluation decision is immutable';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_evaluation_decisions_immutable
        BEFORE UPDATE ON evaluation_decisions
        FOR EACH ROW EXECUTE FUNCTION guard_evaluation_decision_immutable()
        """
    )
    op.execute(
        """
        CREATE FUNCTION guard_evaluation_decision_insert() RETURNS trigger AS $$
        DECLARE parent_status text;
        BEGIN
          SELECT status INTO parent_status
          FROM evaluation_runs
          WHERE tenant_id = NEW.tenant_id AND id = NEW.evaluation_run_id
          FOR SHARE;
          IF parent_status IS NULL THEN
            RETURN NEW;
          END IF;
          IF parent_status <> 'RUNNING' THEN
            RAISE EXCEPTION 'evaluation work items require a running parent';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_evaluation_decisions_running_parent
        BEFORE INSERT ON evaluation_decisions
        FOR EACH ROW EXECUTE FUNCTION guard_evaluation_decision_insert()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_evaluation_decisions_running_parent ON evaluation_decisions"
    )
    op.execute("DROP FUNCTION IF EXISTS guard_evaluation_decision_insert()")
    op.execute("DROP TRIGGER IF EXISTS trg_evaluation_decisions_immutable ON evaluation_decisions")
    op.execute("DROP FUNCTION IF EXISTS guard_evaluation_decision_immutable()")
    op.execute("DROP TRIGGER IF EXISTS trg_evaluation_runs_immutable ON evaluation_runs")
    op.execute("DROP FUNCTION IF EXISTS guard_evaluation_run_immutable()")
    op.drop_index(
        "ix_evaluation_decisions_status_claim_expiry",
        table_name="evaluation_decisions",
    )
    op.drop_index(
        "ix_evaluation_decisions_run_status_created",
        table_name="evaluation_decisions",
    )
    op.drop_table("evaluation_decisions")
    op.drop_index(
        "ix_evaluation_runs_tenant_status_created",
        table_name="evaluation_runs",
    )
    op.drop_table("evaluation_runs")
