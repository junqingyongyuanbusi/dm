import json
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from tests.integration.migration_support import assert_alembic_succeeds, temporary_database

pytestmark = pytest.mark.integration

_BASE_REVISION = "f3b8c1d4e726"
_HEAD_REVISION = "a6f1c3d8e205"


async def test_evaluation_foundation_upgrade_constraints_and_downgrade() -> None:
    async with temporary_database("social_reply_evaluation") as database_url:
        await assert_alembic_succeeds(database_url, "upgrade", _BASE_REVISION)
        engine = create_async_engine(database_url)
        async with engine.connect() as connection:
            tables_before = {
                row[0]
                for row in await connection.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema='public'"
                    )
                )
            }
        await engine.dispose()
        assert "evaluation_runs" not in tables_before
        assert "evaluation_decisions" not in tables_before

        await assert_alembic_succeeds(database_url, "upgrade", "head")
        engine = create_async_engine(database_url)
        async with engine.connect() as connection:
            revision = (
                await connection.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one()
            run_constraints = {
                row[0]
                for row in await connection.execute(
                    text(
                        "SELECT constraint_name FROM information_schema.table_constraints "
                        "WHERE table_name='evaluation_runs'"
                    )
                )
            }
            decision_constraints = {
                row[0]
                for row in await connection.execute(
                    text(
                        "SELECT constraint_name FROM information_schema.table_constraints "
                        "WHERE table_name='evaluation_decisions'"
                    )
                )
            }
            decision_columns = {
                row[0]
                for row in await connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name='evaluation_decisions'"
                    )
                )
            }
        assert revision == _HEAD_REVISION
        assert {
            "uq_evaluation_runs_tenant_id_id",
            "ck_evaluation_runs_status",
            "ck_evaluation_runs_manifest",
            "ck_evaluation_runs_data_class",
            "ck_evaluation_runs_expected_count",
        } <= run_constraints
        assert {
            "uq_evaluation_decisions_tenant_id_id",
            "uq_evaluation_decisions_run_source_candidate",
            "fk_evaluation_decisions_tenant_run",
            "ck_evaluation_decisions_running_lease",
        } <= decision_constraints
        assert {
            "scenario_id",
            "source_message_token",
            "task_kind",
            "result_schema_version",
            "result_payload",
        } <= decision_columns
        assert "message_id" not in decision_columns
        assert "conversation_id" not in decision_columns

        run_id = uuid.uuid4()
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO evaluation_runs "
                    "(id, tenant_id, name, data_class, dataset_fingerprint, dataset_version, "
                    "source_token_key_version, candidate_manifest_hash, workload_manifest_hash, "
                    "execution_policy_version, execution_policy_hash, code_revision, status, "
                    "expected_decision_count, retention_class, expires_at) VALUES "
                    "(:id, 'tenant-a', 'run', 'SYNTHETIC', :hash, 'v1', "
                    "'synthetic-v1', :hash, :hash, 'evaluation-execution-v1', :hash, "
                    "'test-revision', 'RUNNING', 3, 'ephemeral', "
                    "now() + interval '1 day')"
                ),
                {"id": run_id, "hash": "a" * 64},
            )

        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO evaluation_runs "
                        "(id, tenant_id, name, data_class, dataset_fingerprint, dataset_version, "
                        "source_token_key_version, candidate_manifest_hash, "
                        "workload_manifest_hash, execution_policy_version, "
                        "execution_policy_hash, code_revision, status, expected_decision_count, "
                        "retention_class, expires_at) VALUES "
                        "(:id, 'tenant-a', 'bad-run', 'REAL_CUSTOMER', :hash, 'v1', "
                        "'synthetic-v1', :hash, :hash, 'evaluation-execution-v1', :hash, "
                        "'test-revision', 'RUNNING', 1, 'ephemeral', "
                        "now() + interval '1 day')"
                    ),
                    {"id": uuid.uuid4(), "hash": "f" * 64},
                )
        decision_id = uuid.uuid4()
        valid_decision = {
            "id": decision_id,
            "tenant_id": "tenant-a",
            "run_id": run_id,
            "token": "b" * 64,
            "scenario": "direct",
            "surface": "direct",
            "contract_id": "candidate-a",
            "contract_manifest": json.dumps(
                {
                    "allowed_evidence": {
                        "fingerprints": [],
                        "labels": {},
                        "metrics": [],
                    },
                    "allowed_language_sources": [],
                    "allowed_reason_codes": [],
                    "allowed_retrieval_sources": [],
                    "contract_hash": "c" * 64,
                    "contract_id": "candidate-a",
                    "execution_mode": "LOCAL_ONLY",
                    "result_schema_version": "e2e-v1",
                    "task_kind": "e2e",
                    "version": "v1",
                }
            ),
            "result_payload": json.dumps(
                {
                    "action": "handoff",
                    "execution": {
                        "estimated_cost_usd": 0.0,
                        "input_token_count": 0,
                        "model_invocation_count": 0,
                        "output_token_count": 0,
                    },
                    "locale": "en",
                    "reason_codes": [],
                }
            ),
            "hash": "c" * 64,
            "input_hash": "d" * 64,
            "result_hash": "e" * 64,
        }
        async with engine.begin() as connection:
            await connection.execute(_decision_insert(), valid_decision)

        async with engine.begin() as connection:
            await connection.execute(
                _decision_insert(),
                {
                    **valid_decision,
                    "id": uuid.uuid4(),
                    "scenario": "chatwoot",
                    "surface": "chatwoot",
                },
            )

        pending = {
            **valid_decision,
            "id": uuid.uuid4(),
            "scenario": "pending",
            "token": "1" * 64,
            "contract_manifest": json.dumps(
                {
                    "allowed_evidence": {
                        "fingerprints": [],
                        "labels": {},
                        "metrics": [],
                    },
                    "allowed_language_sources": [],
                    "allowed_reason_codes": [],
                    "allowed_retrieval_sources": [],
                    "contract_hash": "c" * 64,
                    "contract_id": "candidate-a",
                    "execution_mode": "LOCAL_ONLY",
                    "result_schema_version": "action-v1",
                    "task_kind": "action",
                    "version": "v1",
                }
            ),
        }
        async with engine.begin() as connection:
            await connection.execute(_pending_decision_insert(), pending)

        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    _pending_decision_insert(),
                    {**pending, "id": uuid.uuid4(), "scenario": "support@example.com"},
                )
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    _pending_decision_insert(),
                    {
                        **pending,
                        "id": uuid.uuid4(),
                        "scenario": "invalid-token",
                        "token": "x" * 64,
                    },
                )

        with pytest.raises(DBAPIError):
            async with engine.begin() as connection:
                await connection.execute(
                    text("UPDATE evaluation_decisions SET scenario_id='mutated' WHERE id=:id"),
                    {"id": pending["id"]},
                )
        with pytest.raises(DBAPIError):
            async with engine.begin() as connection:
                await connection.execute(
                    text("UPDATE evaluation_decisions SET action='auto_reply' WHERE id=:id"),
                    {"id": valid_decision["id"]},
                )
        with pytest.raises(DBAPIError):
            async with engine.begin() as connection:
                await connection.execute(
                    text("UPDATE evaluation_runs SET retention_class='forever' WHERE id=:id"),
                    {"id": run_id},
                )
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE evaluation_decisions SET status='FAILED', "
                        "error_code='TEST_FAILURE', action='auto_reply', "
                        "result_payload='{}'::jsonb, result_fingerprint=:fingerprint, "
                        "completed_at=clock_timestamp() WHERE id=:id"
                    ),
                    {"fingerprint": "6" * 64, "id": pending["id"]},
                )

        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    _decision_insert(),
                    {
                        **valid_decision,
                        "id": uuid.uuid4(),
                        "tenant_id": "tenant-b",
                        "token": "f" * 64,
                    },
                )
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    _decision_insert(),
                    {**valid_decision, "id": uuid.uuid4()},
                )
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    _decision_insert(),
                    {
                        **valid_decision,
                        "id": uuid.uuid4(),
                        "scenario": "empty-payload",
                        "token": "3" * 64,
                        "result_payload": json.dumps({}),
                    },
                )
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE evaluation_decisions SET status='FAILED', "
                    "error_code='TEST_FAILURE', result_fingerprint=:fingerprint, "
                    "completed_at=clock_timestamp() WHERE id=:id"
                ),
                {"fingerprint": "7" * 64, "id": pending["id"]},
            )
            await connection.execute(
                text(
                    "UPDATE evaluation_runs SET status='FAILED', "
                    "result_set_fingerprint=:fingerprint, completed_at=clock_timestamp() "
                    "WHERE id=:id"
                ),
                {"fingerprint": "9" * 64, "id": run_id},
            )
        with pytest.raises(DBAPIError):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE evaluation_runs SET status='RUNNING', completed_at=NULL, "
                        "result_set_fingerprint=NULL WHERE id=:id"
                    ),
                    {"id": run_id},
                )
        with pytest.raises(DBAPIError):
            async with engine.begin() as connection:
                await connection.execute(
                    _pending_decision_insert(),
                    {
                        **pending,
                        "id": uuid.uuid4(),
                        "scenario": "after-terminal",
                        "token": "2" * 64,
                    },
                )
        await engine.dispose()

        await assert_alembic_succeeds(database_url, "downgrade", _BASE_REVISION)
        engine = create_async_engine(database_url)
        async with engine.connect() as connection:
            tables_after = {
                row[0]
                for row in await connection.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema='public'"
                    )
                )
            }
        await engine.dispose()
        assert "evaluation_runs" not in tables_after
        assert "evaluation_decisions" not in tables_after

        await assert_alembic_succeeds(database_url, "upgrade", "head")
        engine = create_async_engine(database_url)
        async with engine.connect() as connection:
            reupgraded_revision = (
                await connection.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one()
        await engine.dispose()
        assert reupgraded_revision == _HEAD_REVISION


def _decision_insert():
    return text(
        "INSERT INTO evaluation_decisions "
        "(id, tenant_id, evaluation_run_id, source_message_token, scenario_id, "
        "candidate_contract_id, candidate_contract_version, candidate_contract_hash, "
        "candidate_contract_manifest, "
        "task_kind, result_schema_version, delivery_surface, input_fingerprint, status, "
        "action, result_payload, latency_ms, estimated_cost_usd, model_invocation_count, "
        "input_token_count, output_token_count, result_fingerprint, attempt_count, "
        "completed_at) VALUES "
        "(:id, :tenant_id, :run_id, :token, :scenario, :contract_id, 'v1', :hash, "
        "CAST(:contract_manifest AS jsonb), "
        "'e2e', 'e2e-v1', :surface, :input_hash, 'SUCCEEDED', 'handoff', "
        "CAST(:result_payload AS jsonb), "
        "0.0, 0.0, 0, 0, 0, :result_hash, 1, clock_timestamp())"
    )


def _pending_decision_insert():
    return text(
        "INSERT INTO evaluation_decisions "
        "(id, tenant_id, evaluation_run_id, source_message_token, scenario_id, "
        "candidate_contract_id, candidate_contract_version, candidate_contract_hash, "
        "candidate_contract_manifest, task_kind, result_schema_version, delivery_surface, "
        "input_fingerprint, status, attempt_count) VALUES "
        "(:id, :tenant_id, :run_id, :token, :scenario, :contract_id, 'v1', :hash, "
        "CAST(:contract_manifest AS jsonb), 'action', 'action-v1', NULL, :input_hash, "
        "'PENDING', 0)"
    )
