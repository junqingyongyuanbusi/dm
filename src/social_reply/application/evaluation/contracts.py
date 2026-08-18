from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol

_HEX_64 = re.compile(r"[0-9a-f]{64}")
_SAFE_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_EVIDENCE_KEY = re.compile(r"[A-Za-z][A-Za-z0-9._:-]{0,63}")


class EvaluationAction(StrEnum):
    AUTO_REPLY = "auto_reply"
    DRAFT = "draft"
    HANDOFF = "handoff"
    IGNORE = "ignore"


class EvaluationDeliverySurface(StrEnum):
    CHATWOOT = "chatwoot"
    DIRECT = "direct"


class EvaluationTaskKind(StrEnum):
    RETRIEVAL = "retrieval"
    LANGUAGE = "language"
    ACTION = "action"
    RENDERING = "rendering"
    E2E = "e2e"


class EvaluationDataClass(StrEnum):
    SYNTHETIC = "SYNTHETIC"


class EvaluationExecutionMode(StrEnum):
    LOCAL_ONLY = "LOCAL_ONLY"


@dataclass(frozen=True)
class EvaluationEvidence:
    metrics: Mapping[str, int | float | bool] = field(default_factory=dict)
    fingerprints: Mapping[str, str] = field(default_factory=dict)
    labels: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if len(self.metrics) + len(self.fingerprints) + len(self.labels) > 64:
            raise ValueError("evaluation evidence has too many fields")
        for key, value in self.metrics.items():
            _require_evidence_key(key)
            if not isinstance(value, (bool, int, float)):
                raise ValueError("evaluation metric must be numeric or boolean")
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("evaluation metric must be finite")
        for key, value in self.fingerprints.items():
            _require_evidence_key(key)
            _require_hex_64(value, "evaluation evidence fingerprint")
        for key, value in self.labels.items():
            _require_evidence_key(key)
            _require_safe_token(value, "evaluation label", limit=64)
        object.__setattr__(self, "metrics", MappingProxyType(dict(self.metrics)))
        object.__setattr__(self, "fingerprints", MappingProxyType(dict(self.fingerprints)))
        object.__setattr__(self, "labels", MappingProxyType(dict(self.labels)))
        if len(_canonical_json(self.to_persisted()).encode("utf-8")) > 8 * 1024:
            raise ValueError("evaluation evidence exceeds 8 KiB")

    def to_persisted(self) -> dict[str, object]:
        return {
            "fingerprints": dict(self.fingerprints),
            "labels": dict(self.labels),
            "metrics": dict(self.metrics),
        }


@dataclass(frozen=True)
class EvaluationExecutionMetadata:
    estimated_cost_usd: float = 0.0
    model_invocation_count: int = 0
    input_token_count: int = 0
    output_token_count: int = 0

    def __post_init__(self) -> None:
        _validate_cost(self.estimated_cost_usd)
        _validate_count(self.model_invocation_count, "model invocation count")
        _validate_count(self.input_token_count, "input token count")
        _validate_count(self.output_token_count, "output token count")

    def to_persisted(self) -> dict[str, int | float]:
        return {
            "estimated_cost_usd": self.estimated_cost_usd,
            "input_token_count": self.input_token_count,
            "model_invocation_count": self.model_invocation_count,
            "output_token_count": self.output_token_count,
        }


@dataclass(frozen=True)
class CandidateContract:
    contract_id: str
    version: str
    contract_hash: str
    task_kind: EvaluationTaskKind
    result_schema_version: str
    execution_mode: EvaluationExecutionMode
    allowed_reason_codes: tuple[str, ...] = ()
    allowed_metric_keys: tuple[str, ...] = ()
    allowed_fingerprint_keys: tuple[str, ...] = ()
    allowed_label_values: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    allowed_retrieval_sources: tuple[str, ...] = ()
    allowed_language_sources: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_safe_token(self.contract_id, "candidate contract id", limit=128)
        _require_safe_token(self.version, "candidate contract version", limit=64)
        _require_hex_64(self.contract_hash, "candidate contract hash")
        _require_safe_token(self.result_schema_version, "result schema version", limit=64)
        sequence_fields = (
            "allowed_reason_codes",
            "allowed_metric_keys",
            "allowed_fingerprint_keys",
            "allowed_retrieval_sources",
            "allowed_language_sources",
        )
        for field_name in sequence_fields:
            normalized = tuple(getattr(self, field_name))
            if len(set(normalized)) != len(normalized):
                raise ValueError(f"duplicate value in {field_name}")
            for value in normalized:
                _require_safe_token(value, "candidate schema token", limit=128)
            object.__setattr__(self, field_name, normalized)
        frozen_label_values: dict[str, tuple[str, ...]] = {}
        for key, values in self.allowed_label_values.items():
            _require_evidence_key(key)
            normalized = tuple(values)
            if len(set(normalized)) != len(normalized):
                raise ValueError(f"duplicate label value for {key}")
            for value in normalized:
                _require_safe_token(value, "candidate label value", limit=64)
            frozen_label_values[key] = normalized
        object.__setattr__(
            self,
            "allowed_label_values",
            MappingProxyType(frozen_label_values),
        )

    def manifest_entry(self) -> dict[str, object]:
        return {
            "allowed_evidence": {
                "fingerprints": sorted(self.allowed_fingerprint_keys),
                "labels": {
                    key: sorted(values) for key, values in self.allowed_label_values.items()
                },
                "metrics": sorted(self.allowed_metric_keys),
            },
            "allowed_language_sources": sorted(self.allowed_language_sources),
            "allowed_reason_codes": sorted(self.allowed_reason_codes),
            "allowed_retrieval_sources": sorted(self.allowed_retrieval_sources),
            "contract_hash": self.contract_hash,
            "execution_mode": self.execution_mode.value,
            "contract_id": self.contract_id,
            "result_schema_version": self.result_schema_version,
            "task_kind": self.task_kind.value,
            "version": self.version,
        }


@dataclass(frozen=True)
class EvaluationInput:
    tenant_id: str
    source_message_token: str
    scenario_id: str
    delivery_surface: EvaluationDeliverySurface | None
    query_text: str
    context: Mapping[str, object] = field(default_factory=dict)
    input_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if not self.tenant_id.strip() or len(self.tenant_id) > 64:
            raise ValueError("invalid tenant id")
        _require_hex_64(self.source_message_token, "source message token")
        _require_safe_token(self.scenario_id, "scenario id", limit=64)
        canonical_context = _canonical_json_value(self.context, "evaluation context")
        if not isinstance(canonical_context, dict):
            raise ValueError("evaluation context must be an object")
        object.__setattr__(self, "context", _deep_freeze_json(canonical_context))
        object.__setattr__(
            self,
            "input_fingerprint",
            canonical_json_hash(
                {
                    "context": canonical_context,
                    "delivery_surface": (
                        self.delivery_surface.value if self.delivery_surface is not None else None
                    ),
                    "query_text": self.query_text,
                    "scenario_id": self.scenario_id,
                    "source_message_token": self.source_message_token,
                    "tenant_id": self.tenant_id,
                }
            ),
        )


@dataclass(frozen=True)
class RankedPolicyCandidate:
    policy_token: str
    rank: int
    score: float
    source: str

    def __post_init__(self) -> None:
        _require_hex_64(self.policy_token, "policy token")
        _validate_count(self.rank, "retrieval rank", minimum=1)
        _validate_number(self.score, "retrieval score")
        _require_safe_token(self.source, "retrieval source", limit=64)


@dataclass(frozen=True)
class RetrievalEvaluationResult:
    ranked_candidates: tuple[RankedPolicyCandidate, ...]
    evidence: EvaluationEvidence = field(default_factory=EvaluationEvidence)
    execution: EvaluationExecutionMetadata = field(default_factory=EvaluationExecutionMetadata)
    task_kind: EvaluationTaskKind = field(default=EvaluationTaskKind.RETRIEVAL, init=False)

    def __post_init__(self) -> None:
        ranks = [candidate.rank for candidate in self.ranked_candidates]
        if ranks != list(range(1, len(ranks) + 1)):
            raise ValueError("retrieval ranks must be contiguous and ordered")
        if len({candidate.policy_token for candidate in self.ranked_candidates}) != len(ranks):
            raise ValueError("retrieval candidates must be unique")


@dataclass(frozen=True)
class LanguageEvaluationResult:
    locale: str
    confidence: float
    unknown: bool
    source: str
    evidence: EvaluationEvidence = field(default_factory=EvaluationEvidence)
    execution: EvaluationExecutionMetadata = field(default_factory=EvaluationExecutionMetadata)
    task_kind: EvaluationTaskKind = field(default=EvaluationTaskKind.LANGUAGE, init=False)

    def __post_init__(self) -> None:
        _require_safe_token(self.locale, "locale", limit=35)
        _require_safe_token(self.source, "language source", limit=64)
        if type(self.unknown) is not bool:
            raise ValueError("language unknown flag must be boolean")
        _validate_number(
            self.confidence,
            "language confidence",
            minimum=0,
            maximum=1,
        )
        if self.unknown != (self.locale == "und"):
            raise ValueError("unknown language must use locale und")


@dataclass(frozen=True)
class ActionEvaluationResult:
    action: EvaluationAction
    reason_codes: tuple[str, ...] = ()
    evidence: EvaluationEvidence = field(default_factory=EvaluationEvidence)
    execution: EvaluationExecutionMetadata = field(default_factory=EvaluationExecutionMetadata)
    task_kind: EvaluationTaskKind = field(default=EvaluationTaskKind.ACTION, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.action, EvaluationAction):
            raise ValueError("invalid evaluation action")
        _validate_reason_codes(self.reason_codes)


@dataclass(frozen=True)
class RenderingEvaluationResult:
    reply_text: str
    locale: str
    guard_passed: bool
    evidence: EvaluationEvidence = field(default_factory=EvaluationEvidence)
    execution: EvaluationExecutionMetadata = field(default_factory=EvaluationExecutionMetadata)
    task_kind: EvaluationTaskKind = field(default=EvaluationTaskKind.RENDERING, init=False)

    def __post_init__(self) -> None:
        if type(self.guard_passed) is not bool:
            raise ValueError("rendering guard flag must be boolean")
        _validate_reply_text(self.reply_text)
        _require_safe_token(self.locale, "locale", limit=35)


@dataclass(frozen=True)
class EndToEndEvaluationResult:
    action: EvaluationAction
    reply_text: str | None
    locale: str
    reason_codes: tuple[str, ...] = ()
    evidence: EvaluationEvidence = field(default_factory=EvaluationEvidence)
    execution: EvaluationExecutionMetadata = field(default_factory=EvaluationExecutionMetadata)
    task_kind: EvaluationTaskKind = field(default=EvaluationTaskKind.E2E, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.action, EvaluationAction):
            raise ValueError("invalid evaluation action")
        expects_reply = self.action in {EvaluationAction.AUTO_REPLY, EvaluationAction.DRAFT}
        if expects_reply != (self.reply_text is not None):
            raise ValueError("evaluation action and reply text are inconsistent")
        if self.reply_text is not None:
            _validate_reply_text(self.reply_text)
        _require_safe_token(self.locale, "locale", limit=35)
        _validate_reason_codes(self.reason_codes)


type EvaluationResult = (
    RetrievalEvaluationResult
    | LanguageEvaluationResult
    | ActionEvaluationResult
    | RenderingEvaluationResult
    | EndToEndEvaluationResult
)


class ExternalEvaluationCapabilityDeniedError(RuntimeError):
    pass


@dataclass(frozen=True)
class LocalEvaluationCapabilities:
    """Trusted local candidates receive no network or provider capability."""

    def require_external(self, capability: str) -> None:
        raise ExternalEvaluationCapabilityDeniedError(capability)


class EvaluationCandidate(Protocol):
    @property
    def contract(self) -> CandidateContract: ...

    async def evaluate(
        self,
        evaluation_input: EvaluationInput,
        capabilities: LocalEvaluationCapabilities,
    ) -> EvaluationResult: ...


def validate_result_for_contract(contract: CandidateContract, result: EvaluationResult) -> None:
    if not isinstance(
        result,
        (
            RetrievalEvaluationResult,
            LanguageEvaluationResult,
            ActionEvaluationResult,
            RenderingEvaluationResult,
            EndToEndEvaluationResult,
        ),
    ):
        raise TypeError("unsupported evaluation result type")
    if result.task_kind != contract.task_kind:
        raise ValueError("candidate result task kind does not match contract")
    if contract.execution_mode == EvaluationExecutionMode.LOCAL_ONLY and (
        result.execution.estimated_cost_usd != 0
        or result.execution.model_invocation_count != 0
        or result.execution.input_token_count != 0
        or result.execution.output_token_count != 0
    ):
        raise ValueError("local-only candidate reported external execution metadata")
    metrics = set(result.evidence.metrics)
    fingerprints = set(result.evidence.fingerprints)
    labels = set(result.evidence.labels)
    if not metrics <= set(contract.allowed_metric_keys):
        raise ValueError("candidate result contains an unapproved metric")
    if not fingerprints <= set(contract.allowed_fingerprint_keys):
        raise ValueError("candidate result contains an unapproved fingerprint")
    if not labels <= set(contract.allowed_label_values):
        raise ValueError("candidate result contains an unapproved label")
    for key, value in result.evidence.labels.items():
        if value not in contract.allowed_label_values[key]:
            raise ValueError("candidate result contains an unapproved label value")
    if isinstance(result, RetrievalEvaluationResult):
        if any(
            candidate.source not in contract.allowed_retrieval_sources
            for candidate in result.ranked_candidates
        ):
            raise ValueError("candidate result contains an unapproved retrieval source")
    if isinstance(result, LanguageEvaluationResult):
        if result.source not in contract.allowed_language_sources:
            raise ValueError("candidate result contains an unapproved language source")
    reason_codes = result_reason_codes(result)
    if any(reason not in contract.allowed_reason_codes for reason in reason_codes):
        raise ValueError("candidate result contains an unapproved reason code")
    payload = serialize_result(result)
    if len(_canonical_json(payload).encode("utf-8")) > 16 * 1024:
        raise ValueError("candidate result exceeds 16 KiB")


def serialize_result(result: EvaluationResult) -> dict[str, object]:
    evidence = result.evidence.to_persisted()
    execution = result.execution.to_persisted()
    if isinstance(result, RetrievalEvaluationResult):
        return {
            "evidence": evidence,
            "execution": execution,
            "ranked_candidates": [
                {
                    "policy_token": candidate.policy_token,
                    "rank": candidate.rank,
                    "score": candidate.score,
                    "source": candidate.source,
                }
                for candidate in result.ranked_candidates
            ],
        }
    if isinstance(result, LanguageEvaluationResult):
        return {
            "confidence": result.confidence,
            "evidence": evidence,
            "execution": execution,
            "locale": result.locale,
            "source": result.source,
            "unknown": result.unknown,
        }
    if isinstance(result, ActionEvaluationResult):
        return {
            "action": result.action.value,
            "evidence": evidence,
            "execution": execution,
            "reason_codes": list(result.reason_codes),
        }
    if isinstance(result, RenderingEvaluationResult):
        return {
            "evidence": evidence,
            "execution": execution,
            "guard_passed": result.guard_passed,
            "locale": result.locale,
            "reply_text_hash": hash_reply_text(result.reply_text),
        }
    if isinstance(result, EndToEndEvaluationResult):
        return {
            "action": result.action.value,
            "evidence": evidence,
            "execution": execution,
            "locale": result.locale,
            "reason_codes": list(result.reason_codes),
            "reply_text_hash": hash_reply_text(result.reply_text),
        }
    raise TypeError("unsupported evaluation result")


def result_action(result: EvaluationResult) -> str | None:
    if isinstance(result, (ActionEvaluationResult, EndToEndEvaluationResult)):
        return result.action.value
    return None


def result_reason_codes(result: EvaluationResult) -> tuple[str, ...]:
    if isinstance(result, (ActionEvaluationResult, EndToEndEvaluationResult)):
        return result.reason_codes
    return ()


def result_reply_text_hash(result: EvaluationResult) -> str | None:
    if isinstance(result, RenderingEvaluationResult):
        return hash_reply_text(result.reply_text)
    if isinstance(result, EndToEndEvaluationResult):
        return hash_reply_text(result.reply_text)
    return None


def canonical_json_hash(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def hash_reply_text(reply_text: str | None) -> str | None:
    if reply_text is None:
        return None
    return hashlib.sha256(reply_text.encode("utf-8")).hexdigest()


def require_hex_64(value: str, field_name: str) -> None:
    _require_hex_64(value, field_name)


def require_safe_token(value: str, field_name: str, *, limit: int) -> None:
    _require_safe_token(value, field_name, limit=limit)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _canonical_json_value(value: object, field_name: str) -> object:
    try:
        encoded = _canonical_json(value)
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be JSON serializable") from exc
    if len(encoded.encode("utf-8")) > 64 * 1024:
        raise ValueError(f"{field_name} exceeds 64 KiB")
    return decoded


def _deep_freeze_json(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({key: _deep_freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_deep_freeze_json(item) for item in value)
    return value


def _require_hex_64(value: str, field_name: str) -> None:
    if not _HEX_64.fullmatch(value):
        raise ValueError(f"invalid {field_name}")


def _require_safe_token(value: str, field_name: str, *, limit: int) -> None:
    if len(value) > limit or not _SAFE_TOKEN.fullmatch(value):
        raise ValueError(f"invalid {field_name}")


def _require_evidence_key(value: str) -> None:
    if not _EVIDENCE_KEY.fullmatch(value):
        raise ValueError("invalid evaluation evidence key")


def _validate_reason_codes(reason_codes: tuple[str, ...]) -> None:
    if len(reason_codes) > 32:
        raise ValueError("too many evaluation reason codes")
    for reason in reason_codes:
        _require_safe_token(reason, "evaluation reason code", limit=128)


def _validate_cost(value: float) -> None:
    _validate_number(value, "evaluation cost", minimum=0)


def _validate_count(value: int, field_name: str, *, minimum: int = 0) -> None:
    if type(value) is not int or value < minimum or value > 2_147_483_647:
        raise ValueError(f"invalid {field_name}")


def _validate_number(
    value: int | float,
    field_name: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"invalid {field_name}")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ValueError(f"invalid {field_name}")
    if minimum is not None and numeric < minimum:
        raise ValueError(f"invalid {field_name}")
    if maximum is not None and numeric > maximum:
        raise ValueError(f"invalid {field_name}")


def _validate_reply_text(reply_text: str) -> None:
    if not reply_text.strip():
        raise ValueError("evaluation reply text cannot be blank")
    if len(reply_text) > 10_000:
        raise ValueError("evaluation reply text exceeds 10000 characters")
