from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .contracts import CandidateContract, EvaluationCandidate, canonical_json_hash


class DuplicateCandidateContractError(ValueError):
    pass


class CandidateContractNotFoundError(LookupError):
    pass


class CandidateContractDriftError(RuntimeError):
    pass


@dataclass(frozen=True)
class RegisteredCandidate:
    candidate: EvaluationCandidate
    contract: CandidateContract

    def assert_not_drifted(self) -> None:
        if self.candidate.contract != self.contract:
            raise CandidateContractDriftError(self.contract.contract_id)


class CandidateRegistry:
    """Immutable candidate bindings for one reproducible evaluation run."""

    def __init__(self, candidates: Iterable[EvaluationCandidate]) -> None:
        registered: dict[str, RegisteredCandidate] = {}
        contracts: list[CandidateContract] = []
        for candidate in candidates:
            captured_contract = candidate.contract
            if captured_contract.contract_id in registered:
                raise DuplicateCandidateContractError(captured_contract.contract_id)
            registered[captured_contract.contract_id] = RegisteredCandidate(
                candidate=candidate,
                contract=captured_contract,
            )
            contracts.append(captured_contract)
        if not registered:
            raise ValueError("candidate registry cannot be empty")
        contracts.sort(key=lambda item: item.contract_id)
        self._candidates: Mapping[str, RegisteredCandidate] = MappingProxyType(registered)
        self._contracts = tuple(contracts)
        self._manifest_hash = canonical_json_hash(
            [contract.manifest_entry() for contract in contracts]
        )

    @property
    def manifest_hash(self) -> str:
        return self._manifest_hash

    @property
    def contracts(self) -> tuple[CandidateContract, ...]:
        return self._contracts

    def resolve(self, contract_id: str) -> RegisteredCandidate:
        try:
            registered = self._candidates[contract_id]
        except KeyError as exc:
            raise CandidateContractNotFoundError(contract_id) from exc
        registered.assert_not_drifted()
        return registered
