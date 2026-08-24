import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from social_reply.application.evaluation import (
    ActionEvaluationResult,
    CandidateContract,
    CandidateRegistry,
    EndToEndEvaluationResult,
    EvaluationAction,
    EvaluationDataClass,
    EvaluationDeliverySurface,
    EvaluationEvidence,
    EvaluationExecutionMetadata,
    EvaluationExecutionMode,
    EvaluationInput,
    EvaluationRunManifest,
    EvaluationRunner,
    EvaluationTaskKind,
    EvaluationWorkloadItem,
    LanguageEvaluationResult,
    RankedPolicyCandidate,
    RetrievalEvaluationResult,
)
from social_reply.application.evaluation.repository import (
    EvaluationDecisionConflictError,
    EvaluationDecisionReservationRequest,
    EvaluationRepository,
    EvaluationRunCreate,
    EvaluationRunIncompleteError,
    EvaluationRunManifestMismatchError,
    EvaluationWorkItemCreate,
    evaluation_workload_manifest_hash,
)
from social_reply.application.evaluation.runner import EvaluationHeartbeatFailedError
from social_reply.domain.automation.state_machine import ensure_state
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory

pytestmark = pytest.mark.integration


@dataclass
class _StaticCandidate:
    contract: CandidateContract
    result: object
    calls: int = 0
    delay_seconds: float = 0

    async def evaluate(self, evaluation_input, capabilities):
        self.calls += 1
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        return self.result


@dataclass
class _ExternalCandidate:
    contract: CandidateContract

    async def evaluate(self, evaluation_input, capabilities):
        capabilities.require_external("openai")
        raise AssertionError("external capability must be denied")


@dataclass
class _InvalidNumericCandidate:
    contract: CandidateContract

    async def evaluate(self, evaluation_input, capabilities):
        return ActionEvaluationResult(
            action=EvaluationAction.HANDOFF,
            reason_codes=("SAFE_HANDOFF",),
            execution=EvaluationExecutionMetadata(model_invocation_count=1.5),
        )


@dataclass
class _MatrixCandidate:
    contract: CandidateContract
    calls: int = 0

    async def evaluate(self, evaluation_input, capabilities):
        self.calls += 1
        action = EvaluationAction(evaluation_input.context["desired_action"])
        return EndToEndEvaluationResult(
            action=action,
            reply_text="safe synthetic reply" if action != EvaluationAction.HANDOFF else None,
            locale="en",
            reason_codes=("SYNTHETIC_CASE",),
        )


@dataclass
class _LeaseLossCandidate:
    contract: CandidateContract
    started: asyncio.Event
    cancelled: asyncio.Event
    calls: int = 0

    async def evaluate(self, evaluation_input, capabilities):
        self.calls += 1
        if self.calls == 1:
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
        return ActionEvaluationResult(
            action=EvaluationAction.HANDOFF,
            reason_codes=("SAFE_HANDOFF",),
        )


def _contract(
    contract_id: str,
    task_kind: EvaluationTaskKind,
    *,
    marker: str,
) -> CandidateContract:
    return CandidateContract(
        contract_id=contract_id,
        version="v1",
        contract_hash=marker * 64,
        task_kind=task_kind,
        result_schema_version=f"{task_kind.value}-v1",
        execution_mode=EvaluationExecutionMode.LOCAL_ONLY,
        allowed_reason_codes=("SAFE_HANDOFF", "SYNTHETIC_CASE"),
        allowed_metric_keys=("candidate_count",),
        allowed_retrieval_sources=("vector",),
        allowed_language_sources=("lingua",),
    )


def _input(
    token_marker: str,
    *,
    scenario_id: str,
    surface: EvaluationDeliverySurface | None,
    query_text: str = "synthetic question",
    context=None,
) -> EvaluationInput:
    return EvaluationInput(
        tenant_id="tenant-a",
        source_message_token=token_marker * 64,
        scenario_id=scenario_id,
        delivery_surface=surface,
        query_text=query_text,
        context=context or {},
    )


def _manifest() -> EvaluationRunManifest:
    return EvaluationRunManifest(
        tenant_id="tenant-a",
        name="phase-1-synthetic",
        data_class=EvaluationDataClass.SYNTHETIC,
        dataset_fingerprint="d" * 64,
        dataset_version="synthetic-v1",
        source_token_key_version="synthetic-v1",
        code_revision="test-revision",
        retention_class="ephemeral",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


async def test_typed_candidates_execute_and_run_finalizes(migrated_db) -> None:
    retrieval = _StaticCandidate(
        _contract("a-retrieval", EvaluationTaskKind.RETRIEVAL, marker="a"),
        RetrievalEvaluationResult(
            ranked_candidates=(RankedPolicyCandidate("1" * 64, 1, 0.91, "vector"),),
            evidence=EvaluationEvidence(metrics={"candidate_count": 1}),
        ),
    )
    language = _StaticCandidate(
        _contract("l-language", EvaluationTaskKind.LANGUAGE, marker="b"),
        LanguageEvaluationResult(locale="en", confidence=0.99, unknown=False, source="lingua"),
    )
    action = _StaticCandidate(
        _contract("q-action", EvaluationTaskKind.ACTION, marker="c"),
        ActionEvaluationResult(
            action=EvaluationAction.HANDOFF,
            reason_codes=("SAFE_HANDOFF",),
        ),
    )
    registry = CandidateRegistry([retrieval, language, action])
    runner = EvaluationRunner(session_factory=get_session_factory(), registry=registry)
    items = (
        EvaluationWorkloadItem("a-retrieval", _input("1", scenario_id="base", surface=None)),
        EvaluationWorkloadItem("l-language", _input("2", scenario_id="base", surface=None)),
        EvaluationWorkloadItem("q-action", _input("3", scenario_id="base", surface=None)),
    )
    run = await runner.create_run(_manifest(), items)

    results = [
        await runner.execute(
            evaluation_run_id=run.run_id,
            candidate_contract_id=item.candidate_contract_id,
            evaluation_input=item.evaluation_input,
        )
        for item in items
    ]
    assert {result.status for result in results} == {"SUCCEEDED"}
    summary = await runner.finalize_run(tenant_id="tenant-a", evaluation_run_id=run.run_id)
    assert summary.status == "COMPLETED"
    assert summary.succeeded == 3

    async with get_session_factory()() as session:
        decisions = (
            (
                await session.execute(
                    select(models.EvaluationDecision).order_by(models.EvaluationDecision.task_kind)
                )
            )
            .scalars()
            .all()
        )
    assert {decision.task_kind for decision in decisions} == {"retrieval", "language", "action"}
    assert all(decision.result_payload is not None for decision in decisions)
    assert all(decision.model_invocation_count == 0 for decision in decisions)
    action_decision = next(decision for decision in decisions if decision.task_kind == "action")
    with pytest.raises(IntegrityError):
        async with get_session_factory()() as session:
            await session.execute(
                text("UPDATE evaluation_decisions SET action='auto_reply' WHERE id=:id"),
                {"id": action_decision.id},
            )
            await session.commit()


async def test_workload_identity_includes_scenario_and_actual_input(migrated_db) -> None:
    contract = _contract("matrix", EvaluationTaskKind.E2E, marker="d")
    candidate = _MatrixCandidate(contract)
    registry = CandidateRegistry([candidate])
    runner = EvaluationRunner(session_factory=get_session_factory(), registry=registry)
    direct = _input(
        "4",
        scenario_id="direct",
        surface=EvaluationDeliverySurface.DIRECT,
        context={"desired_action": "draft"},
    )
    chatwoot = _input(
        "4",
        scenario_id="chatwoot",
        surface=EvaluationDeliverySurface.CHATWOOT,
        context={"desired_action": "draft"},
    )
    run = await runner.create_run(
        _manifest(),
        (
            EvaluationWorkloadItem("matrix", direct),
            EvaluationWorkloadItem("matrix", chatwoot),
        ),
    )
    assert run.expected_decision_count == 2
    mismatched_runner = EvaluationRunner(
        session_factory=get_session_factory(),
        registry=registry,
        lease_seconds=60,
    )
    with pytest.raises(EvaluationRunManifestMismatchError, match="execution policy"):
        await mismatched_runner.execute(
            evaluation_run_id=run.run_id,
            candidate_contract_id="matrix",
            evaluation_input=direct,
        )

    async with get_session_factory()() as session:
        persisted = (
            await session.execute(
                select(models.EvaluationDecision).where(
                    models.EvaluationDecision.evaluation_run_id == run.run_id,
                    models.EvaluationDecision.scenario_id == "direct",
                )
            )
        ).scalar_one()
        mutated_manifest = dict(persisted.candidate_contract_manifest)
        mutated_manifest["allowed_reason_codes"] = ["MUTATED"]
        await session.execute(
            update(models.EvaluationDecision)
            .where(models.EvaluationDecision.id == persisted.id)
            .values(candidate_contract_manifest=mutated_manifest)
        )
        await session.commit()
    with pytest.raises(EvaluationDecisionConflictError):
        await runner.execute(
            evaluation_run_id=run.run_id,
            candidate_contract_id="matrix",
            evaluation_input=direct,
        )
    assert candidate.__class__ is _MatrixCandidate
    changed_input = _input(
        "4",
        scenario_id="direct",
        surface=EvaluationDeliverySurface.DIRECT,
        query_text="different synthetic question",
        context={"desired_action": "draft"},
    )
    with pytest.raises(EvaluationDecisionConflictError):
        await runner.execute(
            evaluation_run_id=run.run_id,
            candidate_contract_id="matrix",
            evaluation_input=changed_input,
        )
    assert candidate.calls == 0


async def test_repository_rejects_false_workload_manifest(migrated_db) -> None:
    contract = _contract("manifest", EvaluationTaskKind.ACTION, marker="e")
    item = EvaluationWorkItemCreate(
        source_message_token="5" * 64,
        scenario_id="base",
        candidate_contract=contract,
        delivery_surface=None,
        input_fingerprint="6" * 64,
    )
    correct_hash = evaluation_workload_manifest_hash((item,))
    assert correct_hash != "0" * 64

    async with get_session_factory()() as session:
        repository = EvaluationRepository(session)
        with pytest.raises(ValueError, match="workload manifest hash"):
            await repository.create_run(
                EvaluationRunCreate(
                    tenant_id="tenant-a",
                    name="bad-manifest",
                    data_class=EvaluationDataClass.SYNTHETIC,
                    dataset_fingerprint="7" * 64,
                    dataset_version="synthetic-v1",
                    source_token_key_version="synthetic-v1",
                    candidate_manifest_hash="8" * 64,
                    workload_manifest_hash="0" * 64,
                    execution_policy_version="evaluation-execution-v1",
                    execution_policy_hash="9" * 64,
                    code_revision="test-revision",
                    expected_decision_count=1,
                    retention_class="ephemeral",
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                ),
                (item,),
            )


async def test_concurrent_execute_has_one_winner(migrated_db) -> None:
    candidate = _StaticCandidate(
        _contract("single-winner", EvaluationTaskKind.ACTION, marker="9"),
        ActionEvaluationResult(
            action=EvaluationAction.HANDOFF,
            reason_codes=("SAFE_HANDOFF",),
        ),
        delay_seconds=0.2,
    )
    runner = EvaluationRunner(
        session_factory=get_session_factory(),
        registry=CandidateRegistry([candidate]),
    )
    item = _input("a", scenario_id="base", surface=None)
    run = await runner.create_run(
        _manifest(),
        (EvaluationWorkloadItem("single-winner", item),),
    )
    first, second = await asyncio.gather(
        runner.execute(
            evaluation_run_id=run.run_id,
            candidate_contract_id="single-winner",
            evaluation_input=item,
        ),
        runner.execute(
            evaluation_run_id=run.run_id,
            candidate_contract_id="single-winner",
            evaluation_input=item,
        ),
    )
    assert candidate.calls == 1
    assert sorted([first.executed, second.executed]) == [False, True]
    loser = first if not first.executed else second
    assert loser.status in {"RUNNING", "SUCCEEDED"}


async def test_claim_lease_starts_after_row_lock_contention(migrated_db) -> None:
    candidate = _StaticCandidate(
        _contract("row-contention", EvaluationTaskKind.ACTION, marker="7"),
        ActionEvaluationResult(
            action=EvaluationAction.HANDOFF,
            reason_codes=("SAFE_HANDOFF",),
        ),
        delay_seconds=0.2,
    )
    runner = EvaluationRunner(
        session_factory=get_session_factory(),
        registry=CandidateRegistry([candidate]),
        lease_seconds=1,
    )
    item = _input("7", scenario_id="base", surface=None)
    run = await runner.create_run(
        _manifest(),
        (EvaluationWorkloadItem("row-contention", item),),
    )
    async with get_session_factory()() as locking_session:
        await locking_session.execute(
            select(models.EvaluationDecision)
            .where(models.EvaluationDecision.evaluation_run_id == run.run_id)
            .with_for_update()
        )
        execution = asyncio.create_task(
            runner.execute(
                evaluation_run_id=run.run_id,
                candidate_contract_id="row-contention",
                evaluation_input=item,
            )
        )
        await asyncio.sleep(1.2)
        await locking_session.commit()
    result = await execution
    assert result.status == "SUCCEEDED"
    assert candidate.calls == 1


async def test_expired_lease_is_reclaimed_once(migrated_db) -> None:
    candidate = _StaticCandidate(
        _contract("reclaim", EvaluationTaskKind.ACTION, marker="b"),
        ActionEvaluationResult(
            action=EvaluationAction.HANDOFF,
            reason_codes=("SAFE_HANDOFF",),
        ),
        delay_seconds=0.1,
    )
    registry = CandidateRegistry([candidate])
    runner = EvaluationRunner(
        session_factory=get_session_factory(),
        registry=registry,
        lease_seconds=1,
    )
    item = _input("b", scenario_id="base", surface=None)
    run = await runner.create_run(_manifest(), (EvaluationWorkloadItem("reclaim", item),))
    async with get_session_factory()() as session:
        reservation = await EvaluationRepository(session).reserve_decision(
            EvaluationDecisionReservationRequest(
                tenant_id="tenant-a",
                evaluation_run_id=run.run_id,
                source_message_token=item.source_message_token,
                scenario_id=item.scenario_id,
                candidate_contract=registry.resolve("reclaim").contract,
                delivery_surface=None,
                input_fingerprint=item.input_fingerprint,
                lease_seconds=1,
            )
        )
        assert reservation.acquired is True
        await session.commit()
    async with get_session_factory()() as session:
        await session.execute(
            text(
                "UPDATE evaluation_decisions "
                "SET claim_expires_at = clock_timestamp() - interval '1 second'"
            )
        )
        await session.commit()

    first, second = await asyncio.gather(
        runner.execute(
            evaluation_run_id=run.run_id,
            candidate_contract_id="reclaim",
            evaluation_input=item,
        ),
        runner.execute(
            evaluation_run_id=run.run_id,
            candidate_contract_id="reclaim",
            evaluation_input=item,
        ),
    )
    assert candidate.calls == 1
    assert sorted([first.executed, second.executed]) == [False, True]
    async with get_session_factory()() as session:
        decision = (await session.execute(select(models.EvaluationDecision))).scalar_one()
    assert decision.status == "SUCCEEDED"
    assert decision.attempt_count == 2


async def test_heartbeat_prevents_reclaim(migrated_db) -> None:
    candidate = _StaticCandidate(
        _contract("heartbeat", EvaluationTaskKind.ACTION, marker="c"),
        ActionEvaluationResult(
            action=EvaluationAction.HANDOFF,
            reason_codes=("SAFE_HANDOFF",),
        ),
        delay_seconds=1.4,
    )
    runner = EvaluationRunner(
        session_factory=get_session_factory(),
        registry=CandidateRegistry([candidate]),
        lease_seconds=1,
    )
    item = _input("c", scenario_id="base", surface=None)
    run = await runner.create_run(_manifest(), (EvaluationWorkloadItem("heartbeat", item),))
    first_task = asyncio.create_task(
        runner.execute(
            evaluation_run_id=run.run_id,
            candidate_contract_id="heartbeat",
            evaluation_input=item,
        )
    )
    await asyncio.sleep(1.1)
    second = await runner.execute(
        evaluation_run_id=run.run_id,
        candidate_contract_id="heartbeat",
        evaluation_input=item,
    )
    first = await first_task
    assert first.executed is True
    assert second.executed is False
    assert candidate.calls == 1


async def test_heartbeat_loss_cancels_old_candidate_before_reclaim(migrated_db) -> None:
    candidate = _LeaseLossCandidate(
        _contract("lease-loss", EvaluationTaskKind.ACTION, marker="3"),
        asyncio.Event(),
        asyncio.Event(),
    )
    runner = EvaluationRunner(
        session_factory=get_session_factory(),
        registry=CandidateRegistry([candidate]),
        lease_seconds=1,
    )
    item = _input("3", scenario_id="base", surface=None)
    run = await runner.create_run(_manifest(), (EvaluationWorkloadItem("lease-loss", item),))
    old_worker = asyncio.create_task(
        runner.execute(
            evaluation_run_id=run.run_id,
            candidate_contract_id="lease-loss",
            evaluation_input=item,
        )
    )
    await candidate.started.wait()
    async with get_session_factory()() as session:
        await session.execute(
            text(
                "UPDATE evaluation_decisions SET claim_token=:claim_token, "
                "claim_expires_at=clock_timestamp() - interval '1 second'"
            ),
            {"claim_token": uuid.uuid4()},
        )
        await session.commit()
    await asyncio.wait_for(candidate.cancelled.wait(), timeout=2)
    with pytest.raises(EvaluationHeartbeatFailedError):
        await old_worker

    new_owner = await runner.execute(
        evaluation_run_id=run.run_id,
        candidate_contract_id="lease-loss",
        evaluation_input=item,
    )
    assert new_owner.status == "SUCCEEDED"
    assert candidate.calls == 2


async def test_external_capability_and_unapproved_evidence_fail_closed(migrated_db) -> None:
    external_contract = _contract("external", EvaluationTaskKind.ACTION, marker="d")
    unsafe_contract = _contract("unsafe-evidence", EvaluationTaskKind.ACTION, marker="e")
    unsafe_candidate = _StaticCandidate(
        unsafe_contract,
        ActionEvaluationResult(
            action=EvaluationAction.HANDOFF,
            reason_codes=("SAFE_HANDOFF",),
            evidence=EvaluationEvidence(labels={"order_id": "ABC123"}),
        ),
    )
    invalid_numeric_contract = _contract(
        "invalid-numeric",
        EvaluationTaskKind.ACTION,
        marker="6",
    )
    invalid_numeric_candidate = _InvalidNumericCandidate(invalid_numeric_contract)
    runner = EvaluationRunner(
        session_factory=get_session_factory(),
        registry=CandidateRegistry(
            [
                _ExternalCandidate(external_contract),
                unsafe_candidate,
                invalid_numeric_candidate,
            ]
        ),
    )
    external_input = _input("d", scenario_id="base", surface=None)
    unsafe_input = _input("e", scenario_id="base", surface=None)
    invalid_numeric_input = _input("6", scenario_id="base", surface=None)
    run = await runner.create_run(
        _manifest(),
        (
            EvaluationWorkloadItem("external", external_input),
            EvaluationWorkloadItem("unsafe-evidence", unsafe_input),
            EvaluationWorkloadItem("invalid-numeric", invalid_numeric_input),
        ),
    )
    external = await runner.execute(
        evaluation_run_id=run.run_id,
        candidate_contract_id="external",
        evaluation_input=external_input,
    )
    unsafe = await runner.execute(
        evaluation_run_id=run.run_id,
        candidate_contract_id="unsafe-evidence",
        evaluation_input=unsafe_input,
    )
    invalid_numeric = await runner.execute(
        evaluation_run_id=run.run_id,
        candidate_contract_id="invalid-numeric",
        evaluation_input=invalid_numeric_input,
    )
    assert external.status == "FAILED"
    assert unsafe.status == "FAILED"
    assert invalid_numeric.status == "FAILED"
    async with get_session_factory()() as session:
        decisions = (await session.execute(select(models.EvaluationDecision))).scalars().all()
    decisions_by_contract = {decision.candidate_contract_id: decision for decision in decisions}
    assert decisions_by_contract["invalid-numeric"].error_code == "CANDIDATE_RESULT_INVALID"
    assert {decision.error_code for decision in decisions} == {
        "EXTERNAL_CAPABILITY_DENIED",
        "CANDIDATE_RESULT_INVALID",
    }
    assert all(decision.result_payload is None for decision in decisions)
    assert "ABC123" not in repr([decision.__dict__ for decision in decisions])


async def test_finalize_rejects_incomplete_and_marks_failed_workload(migrated_db) -> None:
    success = _StaticCandidate(
        _contract("success", EvaluationTaskKind.ACTION, marker="f"),
        ActionEvaluationResult(
            action=EvaluationAction.HANDOFF,
            reason_codes=("SAFE_HANDOFF",),
        ),
    )
    failure = _ExternalCandidate(_contract("failure", EvaluationTaskKind.ACTION, marker="1"))
    runner = EvaluationRunner(
        session_factory=get_session_factory(),
        registry=CandidateRegistry([success, failure]),
    )
    success_input = _input("f", scenario_id="base", surface=None)
    failure_input = _input("0", scenario_id="base", surface=None)
    run = await runner.create_run(
        _manifest(),
        (
            EvaluationWorkloadItem("success", success_input),
            EvaluationWorkloadItem("failure", failure_input),
        ),
    )
    await runner.execute(
        evaluation_run_id=run.run_id,
        candidate_contract_id="success",
        evaluation_input=success_input,
    )
    with pytest.raises(EvaluationRunIncompleteError):
        await runner.finalize_run(tenant_id="tenant-a", evaluation_run_id=run.run_id)
    await runner.execute(
        evaluation_run_id=run.run_id,
        candidate_contract_id="failure",
        evaluation_input=failure_input,
    )
    summary = await runner.finalize_run(tenant_id="tenant-a", evaluation_run_id=run.run_id)
    assert summary.status == "FAILED"
    assert summary.failed == 1


async def test_finalize_detects_workload_and_result_tampering(migrated_db) -> None:
    candidate = _StaticCandidate(
        _contract("tamper", EvaluationTaskKind.ACTION, marker="4"),
        ActionEvaluationResult(
            action=EvaluationAction.HANDOFF,
            reason_codes=("SAFE_HANDOFF",),
        ),
    )
    runner = EvaluationRunner(
        session_factory=get_session_factory(),
        registry=CandidateRegistry([candidate]),
    )
    pending_input = _input("4", scenario_id="pending", surface=None)
    pending_run = await runner.create_run(
        _manifest(),
        (EvaluationWorkloadItem("tamper", pending_input),),
    )
    async with get_session_factory()() as session:
        await session.execute(
            text(
                "UPDATE evaluation_decisions SET input_fingerprint=:fingerprint "
                "WHERE evaluation_run_id=:run_id"
            ),
            {"fingerprint": "5" * 64, "run_id": pending_run.run_id},
        )
        await session.commit()
    with pytest.raises(EvaluationRunManifestMismatchError):
        await runner.finalize_run(
            tenant_id="tenant-a",
            evaluation_run_id=pending_run.run_id,
        )

    completed_input = _input("5", scenario_id="completed", surface=None)
    completed_run = await runner.create_run(
        _manifest(),
        (EvaluationWorkloadItem("tamper", completed_input),),
    )
    await runner.execute(
        evaluation_run_id=completed_run.run_id,
        candidate_contract_id="tamper",
        evaluation_input=completed_input,
    )
    await runner.finalize_run(
        tenant_id="tenant-a",
        evaluation_run_id=completed_run.run_id,
    )
    async with get_session_factory()() as session:
        await session.execute(
            text(
                "UPDATE evaluation_decisions SET latency_ms=latency_ms + 1 "
                "WHERE evaluation_run_id=:run_id"
            ),
            {"run_id": completed_run.run_id},
        )
        await session.commit()
    with pytest.raises(EvaluationRunManifestMismatchError):
        await runner.finalize_run(
            tenant_id="tenant-a",
            evaluation_run_id=completed_run.run_id,
        )


async def test_evaluation_matrix_does_not_mutate_production_tables(session, migrated_db) -> None:
    await _seed_production_context(session)
    before = await _production_snapshot(session)
    matrix = _MatrixCandidate(_contract("matrix-e2e", EvaluationTaskKind.E2E, marker="2"))
    runner = EvaluationRunner(
        session_factory=get_session_factory(),
        registry=CandidateRegistry([matrix]),
    )
    workload = tuple(
        EvaluationWorkloadItem(
            "matrix-e2e",
            _input(
                str(index),
                scenario_id=f"{surface.value}-{action.value}",
                surface=surface,
                context={"desired_action": action.value},
            ),
        )
        for index, (surface, action) in enumerate(
            (
                (EvaluationDeliverySurface.CHATWOOT, EvaluationAction.AUTO_REPLY),
                (EvaluationDeliverySurface.CHATWOOT, EvaluationAction.DRAFT),
                (EvaluationDeliverySurface.CHATWOOT, EvaluationAction.HANDOFF),
                (EvaluationDeliverySurface.DIRECT, EvaluationAction.AUTO_REPLY),
                (EvaluationDeliverySurface.DIRECT, EvaluationAction.DRAFT),
                (EvaluationDeliverySurface.DIRECT, EvaluationAction.HANDOFF),
            ),
            start=1,
        )
    )
    run = await runner.create_run(_manifest(), workload)
    for item in workload:
        result = await runner.execute(
            evaluation_run_id=run.run_id,
            candidate_contract_id=item.candidate_contract_id,
            evaluation_input=item.evaluation_input,
        )
        assert result.status == "SUCCEEDED"

    session.expire_all()
    after = await _production_snapshot(session)
    assert after == before
    assert await _count(session, models.EvaluationRun) == 1
    assert await _count(session, models.EvaluationDecision) == 6


async def _seed_production_context(session) -> None:
    account_id, contact_id, conversation_id, message_id = (uuid.uuid4() for _ in range(4))
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            tenant_id="tenant-a",
            brand_id="b1",
            platform="telegram",
            name="evaluation-isolation",
            chatwoot_inbox_id=101,
        )
    )
    await session.execute(
        insert(models.Contact).values(
            id=contact_id,
            tenant_id="tenant-a",
            platform="telegram",
            platform_account_id=account_id,
            external_user_id="evaluation-isolation",
        )
    )
    await session.execute(
        insert(models.Conversation).values(
            id=conversation_id,
            tenant_id="tenant-a",
            brand_id="b1",
            platform="telegram",
            platform_account_id=account_id,
            contact_id=contact_id,
            conversation_key=f"evaluation:{conversation_id}",
        )
    )
    await session.execute(
        insert(models.Message).values(
            id=message_id,
            conversation_id=conversation_id,
            direction="inbound",
            sender_type="contact",
            text="production row must remain untouched",
        )
    )
    await ensure_state(session, conversation_id, "BOT_ACTIVE")
    await session.commit()


async def _production_snapshot(session) -> dict[str, object]:
    state_rows = (
        await session.execute(
            select(
                models.AutomationState.conversation_id,
                models.AutomationState.state,
                models.AutomationState.state_version,
            )
        )
    ).all()
    return {
        "automation_states": tuple(state_rows),
        "decision_jobs": await _count(session, models.DecisionJob),
        "handoff_notification_intents": await _count(session, models.HandoffNotificationIntent),
        "human_work_items": await _count(session, models.HumanWorkItem),
        "outbox_messages": await _count(session, models.OutboxMessage),
        "reply_decisions": await _count(session, models.ReplyDecision),
    }


async def _count(session, model) -> int:
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()
