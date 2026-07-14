from dataclasses import dataclass
from enum import StrEnum


class ReplyAction(StrEnum):
    AUTO_REPLY = "auto_reply"
    DRAFT = "draft"        # 只写 Chatwoot 私有备注，不对外发
    HANDOFF = "handoff"    # 转人工
    IGNORE = "ignore"      # 不回复也不接管


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
