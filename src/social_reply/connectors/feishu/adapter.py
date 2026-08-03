import json
from datetime import UTC, datetime
from typing import Any

from social_reply.connectors.feishu.contracts import FEISHU_GROUP_MODE
from social_reply.domain.messages.canonical import CanonicalEvent, ChannelType

_EVENT_NAMESPACE = "im.message.receive_v1"
_SAFE_ATTACHMENT_METADATA = (
    "file_name",
    "mime_type",
    "duration",
    "width",
    "height",
)


class FeishuWebhookAdapter:
    platform = "feishu"

    def __init__(
        self,
        *,
        account_id: str,
        bot_open_id: str,
        group_mode: str,
    ) -> None:
        self._account_id = account_id
        self._bot_open_id = bot_open_id
        self._group_mode = group_mode

    def normalize(self, payload: dict[str, Any]) -> list[CanonicalEvent]:
        header = payload.get("header")
        event = payload.get("event")
        if not isinstance(header, dict) or not isinstance(event, dict):
            return []
        if payload.get("schema") != "2.0" or header.get("event_type") != _EVENT_NAMESPACE:
            return []

        sender = event.get("sender")
        message = event.get("message")
        if not isinstance(sender, dict) or not isinstance(message, dict):
            return []
        sender_id = sender.get("sender_id")
        sender_open_id = sender_id.get("open_id") if isinstance(sender_id, dict) else None
        if (
            sender.get("sender_type") != "user"
            or not _nonempty(sender_open_id)
            or sender_open_id == self._bot_open_id
        ):
            return []

        required = {
            key: message.get(key)
            for key in (
                "message_id",
                "chat_id",
                "chat_type",
                "message_type",
                "content",
                "create_time",
            )
        }
        if not all(_nonempty(value) for value in required.values()):
            return []
        occurred_at = _occurred_at(required["create_time"])
        if occurred_at is None:
            return []
        message_id = required["message_id"]
        chat_id = required["chat_id"]
        chat_type = required["chat_type"]
        message_type = required["message_type"]
        try:
            content = json.loads(required["content"])
        except (TypeError, json.JSONDecodeError):
            return []
        if not isinstance(content, dict):
            return []

        if chat_type == "p2p":
            channel_type = ChannelType.DM
            kind = "dm"
        elif chat_type == "group":
            mention_key = self._bot_mention_key(message.get("mentions"))
            if self._group_mode != FEISHU_GROUP_MODE or mention_key is None:
                return []
            channel_type = ChannelType.MENTION
            kind = "mention"
        else:
            return []

        attachments: list[dict[str, Any]] = []
        if message_type == "text":
            text = content.get("text")
            if not isinstance(text, str):
                return []
            if chat_type == "group":
                text = text.replace(mention_key, "").strip()
            if not text.strip():
                return []
        else:
            text = None
            attachments.append(_attachment(message_type, message_id, content))

        thread_id = _optional_string(message.get("thread_id"))
        root_id = _optional_string(message.get("root_id"))
        reply_target = {
            "kind": kind,
            "message_id": message_id,
            "chat_id": chat_id,
            "chat_type": chat_type,
            "sender_open_id": sender_open_id,
        }
        conversation_parts = [
            "feishu",
            self._account_id,
            chat_id,
            sender_open_id,
        ]
        if thread_id is not None:
            reply_target["thread_id"] = thread_id
        if root_id is not None:
            reply_target["root_id"] = root_id
        thread_scope = thread_id or root_id
        if thread_scope is not None:
            conversation_parts.extend(("thread", thread_scope))

        return [
            CanonicalEvent(
                platform=self.platform,
                platform_account_key=self._account_id,
                external_event_id=message_id,
                external_user_id=sender_open_id,
                conversation_key=":".join(conversation_parts),
                text=text,
                occurred_at=occurred_at,
                channel_type=channel_type,
                event_namespace=_EVENT_NAMESPACE,
                external_conversation_id=chat_id,
                event_metadata=_safe_header_metadata(header),
                reply_target=reply_target,
                attachments=attachments,
                raw_payload=payload,
            )
        ]

    def _bot_mention_key(self, mentions: object) -> str | None:
        if not isinstance(mentions, list):
            return None
        for mention in mentions:
            if not isinstance(mention, dict):
                continue
            mention_id = mention.get("id")
            if not isinstance(mention_id, dict):
                continue
            if mention_id.get("open_id") == self._bot_open_id and _nonempty(mention.get("key")):
                return mention["key"]
        return None


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _optional_string(value: object) -> str | None:
    return value if _nonempty(value) else None


def _occurred_at(value: object) -> datetime | None:
    try:
        milliseconds = int(value)
    except (TypeError, ValueError):
        return None
    try:
        return datetime.fromtimestamp(milliseconds / 1000, tz=UTC)
    except (OSError, OverflowError, ValueError):
        return None


def _safe_header_metadata(header: dict[str, Any]) -> dict[str, Any]:
    metadata = {"event_namespace": _EVENT_NAMESPACE}
    for key in ("event_id", "event_type", "create_time", "tenant_key", "app_id"):
        value = header.get(key)
        if isinstance(value, (str, int, float, bool)) and not isinstance(value, dict):
            metadata[key] = value
    return metadata


def _attachment(message_type: str, message_id: str, content: dict[str, Any]) -> dict[str, Any]:
    platform_id = next(
        (
            content[key]
            for key in ("file_key", "image_key", "media_id")
            if _nonempty(content.get(key))
        ),
        message_id,
    )
    metadata = {
        key: content[key]
        for key in _SAFE_ATTACHMENT_METADATA
        if isinstance(content.get(key), (str, int, float, bool))
    }
    return {
        "type": message_type,
        "platform_id": platform_id,
        "metadata": metadata,
    }
