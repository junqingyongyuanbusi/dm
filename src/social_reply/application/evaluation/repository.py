from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.infrastructure.database import models

from .contracts import (
    CandidateContract,
    EvaluationDataClass,
    EvaluationDeliverySurface,
    canonical_json_hash,
    require_hex_64,
    require_safe_token,
)


class EvaluationRunNotFoundError(LookupError):
    pass


class EvaluationRunManifestMismatchError(RuntimeError):
    pass


class EvaluationRunIncompleteError(RuntimeError):
    pass


class EvaluationWorkItemNotFoundError(LookupError):
    pass


class EvaluationDecisionConflictError(RuntimeError):
    pass


class EvaluationDecisionLeaseLostError(RuntimeError):
    pass


@dataclass(frozen=True)
class EvaluationRunCreate:
    tenant_id: str
    name: str
    data_class: EvaluationDataClass
    dataset_fingerprint: str
    dataset_version: str
    source_token_key_version: str
    candidate_manifest_hash: str
    workload_manifest_hash: str
    execution_policy_version: str
    execution_policy_hash: str
    code_revision: str
    expected_decision_count: int
    retention_class: str
    expires_at: datetime

    def __post_init__(self) -> None:
        if not self.tenant_id.strip() or len(self.tenant_id) > 64:
            raise ValueError("invalid tenant id")
        if not self.name.strip() or len(self.name) > 128:
            raise ValueError("invalid evaluation run name")
        require_hex_64(self.dataset_fingerprint, "dataset fingerprint")
        require_hex_64(self.candidate_manifest_hash, "candidate manifest hash")
        require_hex_64(self.workload_manifest_hash, "workload manifest hash")
        require_hex_64(self.execution_policy_hash, "execution policy hash")
        for value, field_name, limit in (
            (self.dataset_version, "dataset version", 128),
            (self.source_token_key_version, "source token key version", 64),
            (self.code_revision, "code revision", 64),
            (self.execution_policy_version, "execution policy version", 64),
            (self.retention_class, "retention class", 32),
        ):
            if not value.strip() or len(value) > limit:
                raise ValueError(f"invalid {field_name}")
        if self.expected_decision_count < 1:
            raise ValueError("expected decision count must be positive")
        if self.expires_at.tzinfo is None or self.expires_at <= datetime.now(UTC):
            raise ValueError("evaluation run expiry must be a future aware datetime")


@dataclass(frozen=True)
class EvaluationWorkItemCreate:
    source_message_token: str
    scenario_id: str
    candidate_contract: CandidateContract
    delivery_surface: EvaluationDeliverySurface | None
    input_fingerprint: str

    def __post_init__(self) -> None:
        require_hex_64(self.source_message_token, "source message token")
        require_safe_token(self.scenario_id, "scenario id", limit=64)
        require_hex_64(self.input_fingerprint, "input fingerprint")
        needs_surface = self.candidate_contract.task_kind.value in {"rendering", "e2e"}
        if needs_surface != (self.delivery_surface is not None):
            raise ValueError("delivery surface does not match candidate task kind")


@dataclass(frozen=True)
class EvaluationDecisionReservationRequest:
    tenant_id: str
    evaluation_run_id: uuid.UUID
    source_message_token: str
    scenario_id: str
    candidate_contract: CandidateContract
    delivery_surface: EvaluationDeliverySurface | None
    input_fingerprint: str
    lease_seconds: int = 300
    max_attempts: int = 3

    def __post_init__(self) -> None:
        require_hex_64(self.source_message_token, "source message token")
        require_safe_token(self.scenario_id, "scenario id", limit=64)
        require_hex_64(self.input_fingerprint, "input fingerprint")
        if self.lease_seconds < 1 or self.lease_seconds > 3600:
            raise ValueError("evaluation lease must be between 1 and 3600 seconds")
        if self.max_attempts < 1 or self.max_attempts > 10:
            raise ValueError("evaluation max attempts must be between 1 and 10")


@dataclass(frozen=True)
class EvaluationDecisionReservation:
    decision_id: uuid.UUID
    status: str
    acquired: bool
    claim_token: uuid.UUID | None


@dataclass(frozen=True)
class EvaluationDecisionSuccess:
    action: str | None
    reply_text_hash: str | None
    reason_codes: tuple[str, ...]
    result_payload: dict[str, object]
    latency_ms: float
    estimated_cost_usd: float
    model_invocation_count: int
    input_token_count: int
    output_token_count: int
    result_fingerprint: str


@dataclass(frozen=True)
class EvaluationDecisionFailure:
    error_code: str
    error_detail: str
    latency_ms: float
    result_fingerprint: str


@dataclass(frozen=True)
class EvaluationRunSummary:
    run_id: uuid.UUID
    status: str
    expected: int
    total: int
    succeeded: int
    failed: int


def _workload_manifest_from_decisions(
    decisions: list[models.EvaluationDecision],
) -> str:
    return canonical_json_hash(
        sorted(
            (
                {
                    "candidate_contract": decision.candidate_contract_manifest,
                    "delivery_surface": decision.delivery_surface,
                    "input_fingerprint": decision.input_fingerprint,
                    "scenario_id": decision.scenario_id,
                    "source_message_token": decision.source_message_token,
                }
                for decision in decisions
            ),
            key=lambda item: (
                item["source_message_token"],
                item["scenario_id"],
                item["candidate_contract"]["contract_id"],
            ),
        )
    )


def _result_set_fingerprint(decisions: list[models.EvaluationDecision]) -> str:
    return canonical_json_hash(
        sorted(
            (
                {
                    "action": decision.action,
                    "attempt_count": decision.attempt_count,
                    "candidate_contract_id": decision.candidate_contract_id,
                    "error_code": decision.error_code,
                    "estimated_cost_usd": decision.estimated_cost_usd,
                    "input_token_count": decision.input_token_count,
                    "latency_ms": decision.latency_ms,
                    "model_invocation_count": decision.model_invocation_count,
                    "output_token_count": decision.output_token_count,
                    "reason_codes": decision.reason_codes,
                    "reply_text_hash": decision.reply_text_hash,
                    "result_fingerprint": decision.result_fingerprint,
                    "result_payload": decision.result_payload,
                    "scenario_id": decision.scenario_id,
                    "source_message_token": decision.source_message_token,
                    "status": decision.status,
                }
                for decision in decisions
            ),
            key=lambda item: (
                item["source_message_token"],
                item["scenario_id"],
                item["candidate_contract_id"],
            ),
        )
    )


def _summary_from_decisions(
    run: models.EvaluationRun,
    decisions: list[models.EvaluationDecision],
) -> EvaluationRunSummary:
    statuses = [decision.status for decision in decisions]
    return EvaluationRunSummary(
        run_id=run.id,
        status=run.status,
        expected=run.expected_decision_count,
        total=len(decisions),
        succeeded=statuses.count("SUCCEEDED"),
        failed=statuses.count("FAILED"),
    )


def evaluation_workload_manifest_hash(
    work_items: tuple[EvaluationWorkItemCreate, ...],
) -> str:
    return canonical_json_hash(
        sorted(
            (
                {
                    "candidate_contract": item.candidate_contract.manifest_entry(),
                    "delivery_surface": (
                        item.delivery_surface.value if item.delivery_surface is not None else None
                    ),
                    "input_fingerprint": item.input_fingerprint,
                    "scenario_id": item.scenario_id,
                    "source_message_token": item.source_message_token,
                }
                for item in work_items
            ),
            key=lambda item: (
                item["source_message_token"],
                item["scenario_id"],
                item["candidate_contract"]["contract_id"],
            ),
        )
    )


class EvaluationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_run(
        self,
        request: EvaluationRunCreate,
        work_items: tuple[EvaluationWorkItemCreate, ...],
    ) -> models.EvaluationRun:
        if len(work_items) != request.expected_decision_count:
            raise ValueError("workload size does not match expected decision count")
        workload_keys = {
            (
                item.source_message_token,
                item.scenario_id,
                item.candidate_contract.contract_id,
            )
            for item in work_items
        }
        if len(workload_keys) != len(work_items):
            raise ValueError("evaluation workload contains duplicate work items")
        if evaluation_workload_manifest_hash(work_items) != request.workload_manifest_hash:
            raise ValueError("workload manifest hash does not match work items")
        run = models.EvaluationRun(
            tenant_id=request.tenant_id,
            name=request.name,
            data_class=request.data_class.value,
            dataset_fingerprint=request.dataset_fingerprint,
            dataset_version=request.dataset_version,
            source_token_key_version=request.source_token_key_version,
            candidate_manifest_hash=request.candidate_manifest_hash,
            workload_manifest_hash=request.workload_manifest_hash,
            execution_policy_version=request.execution_policy_version,
            execution_policy_hash=request.execution_policy_hash,
            code_revision=request.code_revision,
            status="RUNNING",
            expected_decision_count=request.expected_decision_count,
            retention_class=request.retention_class,
            expires_at=request.expires_at,
        )
        self._session.add(run)
        await self._session.flush()
        self._session.add_all(
            [
                models.EvaluationDecision(
                    tenant_id=request.tenant_id,
                    evaluation_run_id=run.id,
                    source_message_token=item.source_message_token,
                    scenario_id=item.scenario_id,
                    candidate_contract_id=item.candidate_contract.contract_id,
                    candidate_contract_version=item.candidate_contract.version,
                    candidate_contract_hash=item.candidate_contract.contract_hash,
                    candidate_contract_manifest=item.candidate_contract.manifest_entry(),
                    task_kind=item.candidate_contract.task_kind.value,
                    result_schema_version=item.candidate_contract.result_schema_version,
                    delivery_surface=(
                        item.delivery_surface.value if item.delivery_surface is not None else None
                    ),
                    input_fingerprint=item.input_fingerprint,
                    status="PENDING",
                    attempt_count=0,
                )
                for item in work_items
            ]
        )
        await self._session.flush()
        return run

    async def require_run(
        self,
        *,
        tenant_id: str,
        evaluation_run_id: uuid.UUID,
        for_update: bool = False,
    ) -> models.EvaluationRun:
        statement = select(models.EvaluationRun).where(
            models.EvaluationRun.tenant_id == tenant_id,
            models.EvaluationRun.id == evaluation_run_id,
        )
        if for_update:
            statement = statement.with_for_update()
        run = (await self._session.execute(statement)).scalar_one_or_none()
        if run is None:
            raise EvaluationRunNotFoundError(str(evaluation_run_id))
        return run

    async def reserve_decision(
        self,
        request: EvaluationDecisionReservationRequest,
    ) -> EvaluationDecisionReservation:
        run = await self.require_run(
            tenant_id=request.tenant_id,
            evaluation_run_id=request.evaluation_run_id,
        )
        decision = (
            await self._session.execute(
                select(models.EvaluationDecision)
                .where(
                    models.EvaluationDecision.tenant_id == request.tenant_id,
                    models.EvaluationDecision.evaluation_run_id == request.evaluation_run_id,
                    models.EvaluationDecision.source_message_token == request.source_message_token,
                    models.EvaluationDecision.scenario_id == request.scenario_id,
                    models.EvaluationDecision.candidate_contract_id
                    == request.candidate_contract.contract_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if decision is None:
            raise EvaluationWorkItemNotFoundError(request.candidate_contract.contract_id)
        now = await self._database_now()
        if run.status != "RUNNING" or run.expires_at <= now:
            raise EvaluationRunManifestMismatchError("evaluation run is not active")
        self._assert_immutable_inputs(decision, request)
        if decision.status in {"SUCCEEDED", "FAILED"}:
            return EvaluationDecisionReservation(decision.id, decision.status, False, None)
        if decision.status == "RUNNING" and decision.claim_expires_at is not None:
            if decision.claim_expires_at > now:
                return EvaluationDecisionReservation(decision.id, decision.status, False, None)
            if decision.attempt_count >= request.max_attempts:
                failed = await self._fail_expired_decision(decision, now)
                if failed:
                    return EvaluationDecisionReservation(decision.id, "FAILED", False, None)
                current = await self._get_decision(decision.id)
                return EvaluationDecisionReservation(current.id, current.status, False, None)

        claim_token = uuid.uuid4()
        lease_interval = timedelta(seconds=request.lease_seconds)
        allowed_statuses = ["PENDING"]
        statement = update(models.EvaluationDecision).where(
            models.EvaluationDecision.id == decision.id,
            models.EvaluationDecision.status.in_(allowed_statuses),
        )
        if decision.status == "RUNNING":
            statement = update(models.EvaluationDecision).where(
                models.EvaluationDecision.id == decision.id,
                models.EvaluationDecision.status == "RUNNING",
                models.EvaluationDecision.claim_expires_at <= func.clock_timestamp(),
                models.EvaluationDecision.attempt_count < request.max_attempts,
            )
        claimed_id = (
            await self._session.execute(
                statement.values(
                    status="RUNNING",
                    claim_token=claim_token,
                    claim_expires_at=now + lease_interval,
                    attempt_count=models.EvaluationDecision.attempt_count + 1,
                ).returning(models.EvaluationDecision.id)
            )
        ).scalar_one_or_none()
        if claimed_id is None:
            current = await self._get_decision(decision.id)
            return EvaluationDecisionReservation(current.id, current.status, False, None)
        return EvaluationDecisionReservation(claimed_id, "RUNNING", True, claim_token)

    async def renew_lease(
        self,
        *,
        decision_id: uuid.UUID,
        claim_token: uuid.UUID,
        lease_seconds: int,
    ) -> datetime | None:
        lease_interval = timedelta(seconds=lease_seconds)
        renewed = (
            await self._session.execute(
                update(models.EvaluationDecision)
                .where(
                    models.EvaluationDecision.id == decision_id,
                    models.EvaluationDecision.status == "RUNNING",
                    models.EvaluationDecision.claim_token == claim_token,
                    models.EvaluationDecision.claim_expires_at > func.clock_timestamp(),
                )
                .values(claim_expires_at=func.clock_timestamp() + lease_interval)
                .returning(models.EvaluationDecision.claim_expires_at)
            )
        ).scalar_one_or_none()
        if renewed is not None:
            return renewed
        current = await self._get_decision(decision_id)
        if current.status in {"SUCCEEDED", "FAILED"}:
            return None
        raise EvaluationDecisionLeaseLostError(str(decision_id))

    async def complete_success(
        self,
        *,
        decision_id: uuid.UUID,
        claim_token: uuid.UUID,
        result: EvaluationDecisionSuccess,
    ) -> None:
        require_hex_64(result.result_fingerprint, "evaluation result fingerprint")
        if result.reply_text_hash is not None:
            require_hex_64(result.reply_text_hash, "reply text hash")
        self._assert_success_projection(result)
        await self._complete(
            decision_id=decision_id,
            claim_token=claim_token,
            values={
                "status": "SUCCEEDED",
                "action": result.action,
                "reply_text_hash": result.reply_text_hash,
                "reason_codes": list(result.reason_codes),
                "result_payload": result.result_payload,
                "latency_ms": result.latency_ms,
                "estimated_cost_usd": result.estimated_cost_usd,
                "model_invocation_count": result.model_invocation_count,
                "input_token_count": result.input_token_count,
                "output_token_count": result.output_token_count,
                "error_code": None,
                "error_detail": None,
                "result_fingerprint": result.result_fingerprint,
            },
        )

    async def complete_failure(
        self,
        *,
        decision_id: uuid.UUID,
        claim_token: uuid.UUID,
        result: EvaluationDecisionFailure,
    ) -> None:
        require_hex_64(result.result_fingerprint, "evaluation result fingerprint")
        await self._complete(
            decision_id=decision_id,
            claim_token=claim_token,
            values={
                "status": "FAILED",
                "action": None,
                "reply_text_hash": None,
                "reason_codes": [],
                "result_payload": None,
                "latency_ms": result.latency_ms,
                "estimated_cost_usd": None,
                "model_invocation_count": None,
                "input_token_count": None,
                "output_token_count": None,
                "error_code": result.error_code,
                "error_detail": result.error_detail,
                "result_fingerprint": result.result_fingerprint,
            },
        )

    async def finalize_run(
        self,
        *,
        tenant_id: str,
        evaluation_run_id: uuid.UUID,
    ) -> EvaluationRunSummary:
        run = await self.require_run(
            tenant_id=tenant_id,
            evaluation_run_id=evaluation_run_id,
            for_update=True,
        )
        decisions = (
            (
                await self._session.execute(
                    select(models.EvaluationDecision)
                    .where(
                        models.EvaluationDecision.tenant_id == tenant_id,
                        models.EvaluationDecision.evaluation_run_id == evaluation_run_id,
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        if _workload_manifest_from_decisions(decisions) != run.workload_manifest_hash:
            raise EvaluationRunManifestMismatchError("stored workload does not match run manifest")
        summary = _summary_from_decisions(run, decisions)
        if run.status != "RUNNING":
            result_set_fingerprint = _result_set_fingerprint(decisions)
            if run.result_set_fingerprint != result_set_fingerprint:
                raise EvaluationRunManifestMismatchError(
                    "sealed evaluation result set does not match its fingerprint"
                )
            return summary
        if summary.total != summary.expected or summary.total == 0:
            raise EvaluationRunIncompleteError("evaluation workload is incomplete")
        pending_or_running = summary.total - summary.succeeded - summary.failed
        if pending_or_running:
            raise EvaluationRunIncompleteError("evaluation workload has unfinished claims")
        now = await self._database_now()
        run.status = "FAILED" if summary.failed else "COMPLETED"
        run.result_set_fingerprint = _result_set_fingerprint(decisions)
        run.completed_at = now
        await self._session.flush()
        return EvaluationRunSummary(
            run_id=run.id,
            status=run.status,
            expected=summary.expected,
            total=summary.total,
            succeeded=summary.succeeded,
            failed=summary.failed,
        )

    def _assert_success_projection(self, result: EvaluationDecisionSuccess) -> None:
        execution = result.result_payload.get("execution")
        if not isinstance(execution, dict):
            raise ValueError("result payload has no canonical execution metadata")
        expected = {
            "action": result.result_payload.get("action"),
            "estimated_cost_usd": execution.get("estimated_cost_usd"),
            "input_token_count": execution.get("input_token_count"),
            "model_invocation_count": execution.get("model_invocation_count"),
            "output_token_count": execution.get("output_token_count"),
            "reason_codes": result.result_payload.get("reason_codes", []),
            "reply_text_hash": result.result_payload.get("reply_text_hash"),
        }
        actual = {
            "action": result.action,
            "estimated_cost_usd": result.estimated_cost_usd,
            "input_token_count": result.input_token_count,
            "model_invocation_count": result.model_invocation_count,
            "output_token_count": result.output_token_count,
            "reason_codes": list(result.reason_codes),
            "reply_text_hash": result.reply_text_hash,
        }
        if actual != expected:
            raise ValueError("result projection does not match canonical payload")

    async def _complete(
        self,
        *,
        decision_id: uuid.UUID,
        claim_token: uuid.UUID,
        values: dict[str, object],
    ) -> None:
        completed_id = (
            await self._session.execute(
                update(models.EvaluationDecision)
                .where(
                    models.EvaluationDecision.id == decision_id,
                    models.EvaluationDecision.status == "RUNNING",
                    models.EvaluationDecision.claim_token == claim_token,
                    models.EvaluationDecision.claim_expires_at > func.clock_timestamp(),
                )
                .values(
                    **values,
                    claim_token=None,
                    claim_expires_at=None,
                    completed_at=func.clock_timestamp(),
                )
                .returning(models.EvaluationDecision.id)
            )
        ).scalar_one_or_none()
        if completed_id is None:
            raise EvaluationDecisionLeaseLostError(str(decision_id))

    async def _fail_expired_decision(
        self,
        decision: models.EvaluationDecision,
        now: datetime,
    ) -> bool:
        result_fingerprint = hashlib.sha256(
            f"LEASE_EXHAUSTED:{decision.id}:{decision.attempt_count}".encode()
        ).hexdigest()
        failed_id = (
            await self._session.execute(
                update(models.EvaluationDecision)
                .where(
                    models.EvaluationDecision.id == decision.id,
                    models.EvaluationDecision.status == "RUNNING",
                    models.EvaluationDecision.claim_expires_at <= func.clock_timestamp(),
                )
                .values(
                    status="FAILED",
                    error_code="LEASE_EXHAUSTED",
                    error_detail="maximum evaluation attempts exhausted",
                    result_fingerprint=result_fingerprint,
                    claim_token=None,
                    claim_expires_at=None,
                    completed_at=now,
                )
                .returning(models.EvaluationDecision.id)
            )
        ).scalar_one_or_none()
        return failed_id is not None

    def _assert_immutable_inputs(
        self,
        decision: models.EvaluationDecision,
        request: EvaluationDecisionReservationRequest,
    ) -> None:
        expected_surface = (
            request.delivery_surface.value if request.delivery_surface is not None else None
        )
        if (
            decision.scenario_id != request.scenario_id
            or decision.candidate_contract_version != request.candidate_contract.version
            or decision.candidate_contract_hash != request.candidate_contract.contract_hash
            or decision.candidate_contract_manifest != request.candidate_contract.manifest_entry()
            or decision.task_kind != request.candidate_contract.task_kind.value
            or decision.result_schema_version != request.candidate_contract.result_schema_version
            or decision.delivery_surface != expected_surface
            or decision.input_fingerprint != request.input_fingerprint
        ):
            raise EvaluationDecisionConflictError(
                "evaluation input does not match the immutable workload item"
            )

    async def _database_now(self) -> datetime:
        return (await self._session.execute(select(func.clock_timestamp()))).scalar_one()

    async def _get_decision(self, decision_id: uuid.UUID) -> models.EvaluationDecision:
        return (
            await self._session.execute(
                select(models.EvaluationDecision)
                .where(models.EvaluationDecision.id == decision_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
