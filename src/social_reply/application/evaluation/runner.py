from __future__ import annotations

import asyncio
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from time import perf_counter

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .contracts import (
    EvaluationDataClass,
    EvaluationInput,
    ExternalEvaluationCapabilityDeniedError,
    LocalEvaluationCapabilities,
    canonical_json_hash,
    result_action,
    result_reason_codes,
    result_reply_text_hash,
    serialize_result,
    validate_result_for_contract,
)
from .registry import CandidateRegistry
from .repository import (
    EvaluationDecisionFailure,
    EvaluationDecisionReservationRequest,
    EvaluationDecisionSuccess,
    EvaluationRepository,
    EvaluationRunCreate,
    EvaluationRunManifestMismatchError,
    EvaluationRunSummary,
    EvaluationWorkItemCreate,
    evaluation_workload_manifest_hash,
)


class EvaluationHeartbeatFailedError(RuntimeError):
    pass


@dataclass(frozen=True)
class EvaluationRunManifest:
    tenant_id: str
    name: str
    data_class: EvaluationDataClass
    dataset_fingerprint: str
    dataset_version: str
    source_token_key_version: str
    code_revision: str
    retention_class: str
    expires_at: datetime


@dataclass(frozen=True)
class EvaluationWorkloadItem:
    candidate_contract_id: str
    evaluation_input: EvaluationInput


@dataclass(frozen=True)
class EvaluationRunRecord:
    run_id: uuid.UUID
    status: str
    expected_decision_count: int
    candidate_manifest_hash: str
    workload_manifest_hash: str
    execution_policy_version: str
    execution_policy_hash: str


@dataclass(frozen=True)
class EvaluationExecutionResult:
    decision_id: uuid.UUID
    status: str
    executed: bool


class EvaluationRunner:
    """Synthetic evaluation for trusted local candidates; no production persistence imports."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        registry: CandidateRegistry,
        lease_seconds: int = 300,
        max_attempts: int = 3,
    ) -> None:
        if lease_seconds < 1 or lease_seconds > 3600:
            raise ValueError("evaluation lease must be between 1 and 3600 seconds")
        if max_attempts < 1 or max_attempts > 10:
            raise ValueError("evaluation max attempts must be between 1 and 10")
        self._session_factory = session_factory
        self._registry = registry
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._execution_policy_version = "evaluation-execution-v1"
        self._execution_policy_hash = canonical_json_hash(
            {
                "heartbeat": "lease-third-capped-30s",
                "lease_seconds": lease_seconds,
                "max_attempts": max_attempts,
                "version": self._execution_policy_version,
            }
        )

    @property
    def registry(self) -> CandidateRegistry:
        return self._registry

    async def create_run(
        self,
        manifest: EvaluationRunManifest,
        workload: tuple[EvaluationWorkloadItem, ...],
    ) -> EvaluationRunRecord:
        if manifest.data_class != EvaluationDataClass.SYNTHETIC:
            raise ValueError("Phase 1 evaluation only accepts synthetic data")
        work_items: list[EvaluationWorkItemCreate] = []
        for workload_item in workload:
            registered = self._registry.resolve(workload_item.candidate_contract_id)
            evaluation_input = workload_item.evaluation_input
            if evaluation_input.tenant_id != manifest.tenant_id:
                raise ValueError("workload tenant does not match run tenant")
            work_items.append(
                EvaluationWorkItemCreate(
                    source_message_token=evaluation_input.source_message_token,
                    scenario_id=evaluation_input.scenario_id,
                    candidate_contract=registered.contract,
                    delivery_surface=evaluation_input.delivery_surface,
                    input_fingerprint=evaluation_input.input_fingerprint,
                )
            )
        immutable_work_items = tuple(work_items)
        workload_manifest_hash = evaluation_workload_manifest_hash(immutable_work_items)
        async with self._session_factory() as session:
            repository = EvaluationRepository(session)
            run = await repository.create_run(
                EvaluationRunCreate(
                    tenant_id=manifest.tenant_id,
                    name=manifest.name,
                    data_class=manifest.data_class,
                    dataset_fingerprint=manifest.dataset_fingerprint,
                    dataset_version=manifest.dataset_version,
                    source_token_key_version=manifest.source_token_key_version,
                    candidate_manifest_hash=self._registry.manifest_hash,
                    workload_manifest_hash=workload_manifest_hash,
                    execution_policy_version=self._execution_policy_version,
                    execution_policy_hash=self._execution_policy_hash,
                    code_revision=manifest.code_revision,
                    expected_decision_count=len(immutable_work_items),
                    retention_class=manifest.retention_class,
                    expires_at=manifest.expires_at,
                ),
                immutable_work_items,
            )
            record = EvaluationRunRecord(
                run_id=run.id,
                status=run.status,
                expected_decision_count=run.expected_decision_count,
                candidate_manifest_hash=run.candidate_manifest_hash,
                workload_manifest_hash=run.workload_manifest_hash,
                execution_policy_version=run.execution_policy_version,
                execution_policy_hash=run.execution_policy_hash,
            )
            await session.commit()
        return record

    async def execute(
        self,
        *,
        evaluation_run_id: uuid.UUID,
        candidate_contract_id: str,
        evaluation_input: EvaluationInput,
    ) -> EvaluationExecutionResult:
        registered = self._registry.resolve(candidate_contract_id)
        async with self._session_factory() as session:
            repository = EvaluationRepository(session)
            run = await repository.require_run(
                tenant_id=evaluation_input.tenant_id,
                evaluation_run_id=evaluation_run_id,
            )
            if run.candidate_manifest_hash != self._registry.manifest_hash:
                raise EvaluationRunManifestMismatchError(
                    "candidate registry does not match the evaluation run manifest"
                )
            if run.execution_policy_hash != self._execution_policy_hash:
                raise EvaluationRunManifestMismatchError(
                    "runner execution policy does not match the evaluation run"
                )
            reservation = await repository.reserve_decision(
                EvaluationDecisionReservationRequest(
                    tenant_id=evaluation_input.tenant_id,
                    evaluation_run_id=evaluation_run_id,
                    source_message_token=evaluation_input.source_message_token,
                    scenario_id=evaluation_input.scenario_id,
                    candidate_contract=registered.contract,
                    delivery_surface=evaluation_input.delivery_surface,
                    input_fingerprint=evaluation_input.input_fingerprint,
                    lease_seconds=self._lease_seconds,
                    max_attempts=self._max_attempts,
                )
            )
            await session.commit()

        if not reservation.acquired:
            return EvaluationExecutionResult(
                decision_id=reservation.decision_id,
                status=reservation.status,
                executed=False,
            )
        if reservation.claim_token is None:
            raise RuntimeError("acquired evaluation reservation has no claim token")

        started = perf_counter()
        stop_heartbeat = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._heartbeat(
                decision_id=reservation.decision_id,
                claim_token=reservation.claim_token,
                stop=stop_heartbeat,
            )
        )
        candidate_task = asyncio.create_task(
            registered.candidate.evaluate(
                evaluation_input,
                LocalEvaluationCapabilities(),
            )
        )
        try:
            result = await self._await_candidate_or_heartbeat(candidate_task, heartbeat)
            validate_result_for_contract(registered.contract, result)
            result_payload = serialize_result(result)
            action = result_action(result)
            reason_codes = result_reason_codes(result)
            reply_text_hash = result_reply_text_hash(result)
            result_fingerprint = canonical_json_hash(
                {
                    "candidate_contract": registered.contract.manifest_entry(),
                    "input_fingerprint": evaluation_input.input_fingerprint,
                    "result_payload": result_payload,
                }
            )
        except asyncio.CancelledError:
            candidate_task.cancel()
            stop_heartbeat.set()
            with suppress(asyncio.CancelledError, Exception):
                await candidate_task
            with suppress(asyncio.CancelledError, Exception):
                await heartbeat
            raise
        except EvaluationHeartbeatFailedError:
            candidate_task.cancel()
            stop_heartbeat.set()
            with suppress(asyncio.CancelledError, Exception):
                await candidate_task
            raise
        except Exception as exc:
            await self._assert_heartbeat_active(heartbeat)
            latency_ms = (perf_counter() - started) * 1000
            error_code = _error_code(exc)
            error_detail = type(exc).__name__
            result_fingerprint = canonical_json_hash(
                {
                    "candidate_contract": registered.contract.manifest_entry(),
                    "error_code": error_code,
                    "error_detail": error_detail,
                    "input_fingerprint": evaluation_input.input_fingerprint,
                }
            )
            try:
                async with self._session_factory() as session:
                    await EvaluationRepository(session).complete_failure(
                        decision_id=reservation.decision_id,
                        claim_token=reservation.claim_token,
                        result=EvaluationDecisionFailure(
                            error_code=error_code,
                            error_detail=error_detail,
                            latency_ms=latency_ms,
                            result_fingerprint=result_fingerprint,
                        ),
                    )
                    await session.commit()
            finally:
                await self._stop_heartbeat_after_terminal(
                    stop_heartbeat=stop_heartbeat,
                    heartbeat=heartbeat,
                )
            return EvaluationExecutionResult(reservation.decision_id, "FAILED", True)

        await self._assert_heartbeat_active(heartbeat)
        latency_ms = (perf_counter() - started) * 1000
        try:
            async with self._session_factory() as session:
                await EvaluationRepository(session).complete_success(
                    decision_id=reservation.decision_id,
                    claim_token=reservation.claim_token,
                    result=EvaluationDecisionSuccess(
                        action=action,
                        reply_text_hash=reply_text_hash,
                        reason_codes=reason_codes,
                        result_payload=result_payload,
                        latency_ms=latency_ms,
                        estimated_cost_usd=result.execution.estimated_cost_usd,
                        model_invocation_count=result.execution.model_invocation_count,
                        input_token_count=result.execution.input_token_count,
                        output_token_count=result.execution.output_token_count,
                        result_fingerprint=result_fingerprint,
                    ),
                )
                await session.commit()
        finally:
            await self._stop_heartbeat_after_terminal(
                stop_heartbeat=stop_heartbeat,
                heartbeat=heartbeat,
            )
        return EvaluationExecutionResult(reservation.decision_id, "SUCCEEDED", True)

    async def finalize_run(
        self,
        *,
        tenant_id: str,
        evaluation_run_id: uuid.UUID,
    ) -> EvaluationRunSummary:
        async with self._session_factory() as session:
            summary = await EvaluationRepository(session).finalize_run(
                tenant_id=tenant_id,
                evaluation_run_id=evaluation_run_id,
            )
            await session.commit()
        return summary

    async def _await_candidate_or_heartbeat(self, candidate_task, heartbeat):
        done, _ = await asyncio.wait(
            {candidate_task, heartbeat},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if heartbeat in done:
            candidate_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await candidate_task
            try:
                await heartbeat
            except Exception as exc:
                raise EvaluationHeartbeatFailedError("evaluation heartbeat failed") from exc
            raise EvaluationHeartbeatFailedError("evaluation heartbeat stopped unexpectedly")
        return await candidate_task

    async def _assert_heartbeat_active(self, heartbeat: asyncio.Task) -> None:
        if not heartbeat.done():
            return
        try:
            await heartbeat
        except Exception as exc:
            raise EvaluationHeartbeatFailedError("evaluation heartbeat failed") from exc
        raise EvaluationHeartbeatFailedError("evaluation heartbeat stopped unexpectedly")

    async def _stop_heartbeat_after_terminal(
        self,
        *,
        stop_heartbeat: asyncio.Event,
        heartbeat: asyncio.Task,
    ) -> None:
        stop_heartbeat.set()
        with suppress(asyncio.CancelledError, Exception):
            await heartbeat

    async def _heartbeat(
        self,
        *,
        decision_id: uuid.UUID,
        claim_token: uuid.UUID,
        stop: asyncio.Event,
    ) -> None:
        interval = max(0.1, min(self._lease_seconds / 3, 30.0))
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except TimeoutError:
                async with self._session_factory() as session:
                    renewed = await EvaluationRepository(session).renew_lease(
                        decision_id=decision_id,
                        claim_token=claim_token,
                        lease_seconds=self._lease_seconds,
                    )
                    await session.commit()
                    if renewed is None:
                        return


def _error_code(exc: Exception) -> str:
    if isinstance(exc, ExternalEvaluationCapabilityDeniedError):
        return "EXTERNAL_CAPABILITY_DENIED"
    if isinstance(exc, (TypeError, ValueError)):
        return "CANDIDATE_RESULT_INVALID"
    return "CANDIDATE_EXCEPTION"
