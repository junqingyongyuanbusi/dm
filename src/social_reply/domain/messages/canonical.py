from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class ChannelType(StrEnum):
    DM = "dm"
    COMMENT = "comment"
    MENTION = "mention"


class CanonicalEventKind(StrEnum):
    MESSAGE = "message"


@dataclass(frozen=True)
class CanonicalEvent:
    """平台 Adapter 输出的统一入站消息。"""

    platform: str
    platform_account_key: str
    external_event_id: str
    external_user_id: str
    conversation_key: str
    text: str | None
    event_kind: CanonicalEventKind = CanonicalEventKind.MESSAGE
    occurred_at: datetime | None = None
    channel_type: ChannelType = ChannelType.DM
    event_namespace: str | None = None
    external_conversation_id: str | None = None
    event_metadata: dict[str, Any] = field(default_factory=dict)
    reply_target: dict[str, Any] = field(default_factory=dict)
    attachments: list[dict[str, Any]] = field(default_factory=list)
    raw_payload: dict[str, Any] = field(default_factory=dict)


def canonical_event_to_dict(event: CanonicalEvent) -> dict[str, Any]:
    value = asdict(event)
    value["event_kind"] = event.event_kind.value
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
        event_kind=CanonicalEventKind(value.get("event_kind", CanonicalEventKind.MESSAGE)),
        occurred_at=datetime.fromisoformat(occurred_at) if occurred_at else None,
        channel_type=ChannelType(value.get("channel_type", ChannelType.DM)),
        event_namespace=value.get("event_namespace"),
        external_conversation_id=value.get("external_conversation_id"),
        event_metadata=dict(value.get("event_metadata") or {}),
        reply_target=dict(value.get("reply_target") or {}),
        attachments=list(value.get("attachments") or []),
        raw_payload=dict(value.get("raw_payload") or {}),
    )
