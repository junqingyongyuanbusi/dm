from dataclasses import dataclass

import pytest

from social_reply.application.evaluation import (
    ActionEvaluationResult,
    CandidateContract,
    CandidateRegistry,
    EndToEndEvaluationResult,
    EvaluationAction,
    EvaluationEvidence,
    EvaluationExecutionMode,
    EvaluationInput,
    EvaluationTaskKind,
    LanguageEvaluationResult,
    LocalEvaluationCapabilities,
    RenderingEvaluationResult,
)
from social_reply.application.evaluation.contracts import (
    ExternalEvaluationCapabilityDeniedError,
    validate_result_for_contract,
)
from social_reply.application.evaluation.registry import (
    CandidateContractDriftError,
    DuplicateCandidateContractError,
)


@dataclass
class _Candidate:
    contract: CandidateContract

    async def evaluate(self, evaluation_input, capabilities):
        return ActionEvaluationResult(
            action=EvaluationAction.HANDOFF,
            reason_codes=("SAFE_HANDOFF",),
        )


def _contract(
    contract_id: str,
    *,
    version: str = "v1",
    marker: str = "a",
) -> CandidateContract:
    return CandidateContract(
        contract_id=contract_id,
        version=version,
        contract_hash=marker * 64,
        task_kind=EvaluationTaskKind.ACTION,
        result_schema_version="action-v1",
        execution_mode=EvaluationExecutionMode.LOCAL_ONLY,
        allowed_reason_codes=("SAFE_HANDOFF",),
    )


def _input(*, query_text: str = "hello", context=None) -> EvaluationInput:
    return EvaluationInput(
        tenant_id="tenant-a",
        source_message_token="a" * 64,
        scenario_id="default",
        delivery_surface=None,
        query_text=query_text,
        context=context or {"history": [{"label": "safe"}]},
    )


def test_evaluation_input_requires_precomputed_token_and_deep_freezes_context() -> None:
    with pytest.raises(ValueError, match="source message token"):
        EvaluationInput(
            tenant_id="tenant-a",
            source_message_token="production-message-id",
            scenario_id="default",
            delivery_surface=None,
            query_text="hello",
        )

    original_context = {"history": [{"label": "safe"}]}
    item = _input(context=original_context)
    fingerprint = item.input_fingerprint
    original_context["history"][0]["label"] = "mutated"

    assert item.input_fingerprint == fingerprint
    assert item.context["history"][0]["label"] == "safe"
    with pytest.raises(TypeError):
        item.context["history"][0]["label"] = "candidate-mutation"
    assert _input(query_text="different").input_fingerprint != fingerprint
    assert not hasattr(item, "message_id")
    assert not hasattr(item, "conversation_id")


def test_candidate_registry_is_immutable_and_order_independent() -> None:
    first = CandidateRegistry([_Candidate(_contract("b")), _Candidate(_contract("a", marker="c"))])
    second = CandidateRegistry([_Candidate(_contract("a", marker="c")), _Candidate(_contract("b"))])

    assert first.manifest_hash == second.manifest_hash
    assert [contract.contract_id for contract in first.contracts] == ["a", "b"]
    assert first.resolve("a").contract.contract_hash == "c" * 64
    assert not hasattr(first, "register")


def test_candidate_registry_rejects_duplicate_and_drifted_contracts() -> None:
    with pytest.raises(DuplicateCandidateContractError):
        CandidateRegistry(
            [_Candidate(_contract("same")), _Candidate(_contract("same", version="v2"))]
        )

    candidate = _Candidate(_contract("mutable"))
    registry = CandidateRegistry([candidate])
    candidate.contract = _contract("mutable", version="v2", marker="b")
    with pytest.raises(CandidateContractDriftError):
        registry.resolve("mutable")


def test_candidate_contract_copies_mutable_allowlists() -> None:
    reasons = ["SAFE_HANDOFF"]
    contract = CandidateContract(
        contract_id="copy-test",
        version="v1",
        contract_hash="c" * 64,
        task_kind=EvaluationTaskKind.ACTION,
        result_schema_version="action-v1",
        execution_mode=EvaluationExecutionMode.LOCAL_ONLY,
        allowed_reason_codes=reasons,
    )
    reasons.append("MUTATED")
    assert contract.allowed_reason_codes == ("SAFE_HANDOFF",)


def test_result_schema_rejects_raw_labels_and_reason_codes() -> None:
    contract = _contract("safe")
    with pytest.raises(ValueError, match="reason code"):
        ActionEvaluationResult(
            action=EvaluationAction.HANDOFF,
            reason_codes=("support@example.com",),
        )

    result = ActionEvaluationResult(
        action=EvaluationAction.HANDOFF,
        reason_codes=("SAFE_HANDOFF",),
        evidence=EvaluationEvidence(labels={"order_id": "ABC123"}),
    )
    with pytest.raises(ValueError, match="unapproved label"):
        validate_result_for_contract(contract, result)


def test_e2e_action_requires_consistent_reply_text() -> None:
    for action in (EvaluationAction.AUTO_REPLY, EvaluationAction.DRAFT):
        EndToEndEvaluationResult(action=action, reply_text="reply", locale="en")
        with pytest.raises(ValueError, match="inconsistent"):
            EndToEndEvaluationResult(action=action, reply_text=None, locale="en")
    for action in (EvaluationAction.HANDOFF, EvaluationAction.IGNORE):
        EndToEndEvaluationResult(action=action, reply_text=None, locale="en")
        with pytest.raises(ValueError, match="inconsistent"):
            EndToEndEvaluationResult(action=action, reply_text="unsafe", locale="en")


def test_typed_boolean_fields_reject_truthy_non_booleans() -> None:
    with pytest.raises(ValueError, match="boolean"):
        LanguageEvaluationResult(locale="und", confidence=0.0, unknown=1, source="lingua")
    with pytest.raises(ValueError, match="boolean"):
        RenderingEvaluationResult(
            reply_text="reply",
            locale="en",
            guard_passed="false",
        )


def test_local_capabilities_fail_closed_for_external_calls() -> None:
    with pytest.raises(ExternalEvaluationCapabilityDeniedError):
        LocalEvaluationCapabilities().require_external("openai")
