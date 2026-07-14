from dataclasses import dataclass
from enum import Enum, auto

_MESSAGE_TYPE_BY_INT = {0: "incoming", 1: "outgoing", 2: "activity", 3: "template"}


class EventClass(Enum):
    INBOUND_USER = auto()       # 进入决策管线
    AGENT_PUBLIC_REPLY = auto() # 人工坐席公开回复 → HUMAN_ACTIVE
    BOT_ECHO = auto()           # 机器人自身消息，仅对账
    IGNORE = auto()             # 私有备注 / activity 等


@dataclass(frozen=True)
class ChatwootMessage:
    chatwoot_message_id: int
    content: str | None
    message_type: str
    private: bool
    sender_id: str | None
    sender_type: str | None
    chatwoot_conversation_id: int
    chatwoot_inbox_id: int
    chatwoot_account_id: int
    occurred_at_iso: str | None


def parse_message_created(payload: dict) -> ChatwootMessage:
    mt = payload.get("message_type")
    if isinstance(mt, int):
        mt = _MESSAGE_TYPE_BY_INT.get(mt, "unknown")
    sender = payload.get("sender") or {}
    conversation = payload.get("conversation") or {}
    return ChatwootMessage(
        chatwoot_message_id=int(payload["id"]),
        content=payload.get("content"),
        message_type=mt or "unknown",
        private=bool(payload.get("private", False)),
        sender_id=str(sender["id"]) if "id" in sender else None,
        sender_type=sender.get("type"),
        chatwoot_conversation_id=int(conversation["id"]),
        chatwoot_inbox_id=int(conversation["inbox_id"]),
        chatwoot_account_id=int((payload.get("account") or {}).get("id", 0)),
        occurred_at_iso=payload.get("created_at"),
    )


def classify(msg: ChatwootMessage) -> EventClass:
    """PLAN.md §四 发送者甄别（self-echo 的 Outbox 比对在 processor 中另行执行）"""
    if msg.private:
        return EventClass.IGNORE
    if msg.message_type == "incoming":
        return EventClass.INBOUND_USER
    if msg.message_type == "outgoing":
        if msg.sender_type == "agent_bot":
            return EventClass.BOT_ECHO
        if msg.sender_type == "user":
            return EventClass.AGENT_PUBLIC_REPLY
    return EventClass.IGNORE
