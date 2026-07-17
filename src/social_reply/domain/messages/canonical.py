from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class ChannelType(StrEnum):
    DM = "dm"
    COMMENT = "comment"
    MENTION = "mention"


@dataclass(frozen=True)
class CanonicalEvent:
    """平台 Adapter 输出的统一入站消息。"""

    platform: str
    platform_account_key: str
    external_event_id: str
    external_user_id: str
    conversation_key: str
    text: str | None
    occurred_at: datetime | None = None
    channel_type: ChannelType = ChannelType.DM
    external_conversation_id: str | None = None
    reply_target: dict[str, Any] = field(default_factory=dict)
    raw_payload: dict[str, Any] = field(default_factory=dict)


def canonical_event_to_dict(event: CanonicalEvent) -> dict[str, Any]:
    value = asdict(event)
    value["channel_type"] = event.channel_type.value
    value["occurred_at"] = event.occurred_at.isoformat() if event.occurred_at else None
    return value


def canonical_event_from_dict(value: dict[str, Any]) -> CanonicalEvent:
    occurred_at = value.get("occurred_at")
    return CanonicalEvent(
        platform=value["platform"],
        platform_account_key=value["platform_account_key"],
        external_event_id=value["external_event_id"],
        external_user_id=value["external_user_id"],
        conversation_key=value["conversation_key"],
        text=value.get("text"),
        occurred_at=datetime.fromisoformat(occurred_at) if occurred_at else None,
        channel_type=ChannelType(value.get("channel_type", ChannelType.DM)),
        external_conversation_id=value.get("external_conversation_id"),
        reply_target=dict(value.get("reply_target") or {}),
        raw_payload=dict(value.get("raw_payload") or {}),
    )
