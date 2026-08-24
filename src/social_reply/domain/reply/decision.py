import uuid
from dataclasses import dataclass
from enum import StrEnum


class ReplyAction(StrEnum):
    AUTO_REPLY = "auto_reply"
    DRAFT = "draft"  # 只写 Chatwoot 私有备注，不对外发
    HANDOFF = "handoff"  # 转人工
    IGNORE = "ignore"  # 不回复也不接管


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Visibility(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"


@dataclass(frozen=True)
class ReplyDecision:
    action: ReplyAction
    reply_text: str | None = None
    intent: str | None = None
    risk_level: RiskLevel = RiskLevel.LOW
    confidence: float = 0.0
    reply_visibility: Visibility = Visibility.PUBLIC
    handoff_team: str | None = None
    reason_codes: tuple[str, ...] = ()
    source: str = "llm"  # rule / llm / guard
    request_language: str = "und"
    reply_language: str = "und"
    resolved_locale: str = "und"
    knowledge_localization_id: uuid.UUID | None = None
    knowledge_localization_release_id: str | None = None
    knowledge_localization_text_hash: str | None = None
    knowledge_localization_source_hash: str | None = None
    knowledge_content_hash: str | None = None
    knowledge_document_id: uuid.UUID | None = None
    knowledge_chunk_id: uuid.UUID | None = None
    knowledge_similarity: float | None = None
    knowledge_similarity_margin: float | None = None
    multilingual_shadow: bool = False
    multilingual_contract_version: str | None = None
    multilingual_shadow_evidence: dict | None = None
    request_language_confidence: float | None = None
    request_language_source: str | None = None
    knowledge_top2_content_hash: str | None = None
    knowledge_top2_similarity: float | None = None
    knowledge_match_status: str | None = None
    knowledge_gate_version: str | None = None
    knowledge_min_similarity_threshold: float | None = None
    knowledge_min_margin_threshold: float | None = None
    grounding_verified: bool | None = None
    grounding_verifier_version: str | None = None
    grounding_latency_ms: float | None = None
