from datetime import UTC, datetime

from social_reply.domain.messages.canonical import CanonicalEvent, ChannelType


class MetaWebhookAdapter:
    """单个 Facebook/Instagram 账号的 Webhook 归一化与回声过滤。"""

    def __init__(
        self,
        *,
        platform: str | None = None,
        account_id: str | None = None,
        external_account_id: str | None = None,
    ) -> None:
        self._platform = platform
        self._account_id = account_id
        self._external_account_id = external_account_id

    def normalize(self, payload: dict) -> list[CanonicalEvent]:
        events: list[CanonicalEvent] = []
        object_type = payload.get("object")
        inferred_platform = "instagram" if object_type == "instagram" else "facebook"
        platform = self._platform or inferred_platform
        for entry in payload.get("entry", []):
            entry_account_id = str(entry.get("id", ""))
            if self._external_account_id and entry_account_id != self._external_account_id:
                continue
            account_key = self._account_id or entry_account_id
            if not account_key:
                continue
            for messaging in entry.get("messaging", []):
                message = messaging.get("message") or {}
                sender_id = str((messaging.get("sender") or {}).get("id", ""))
                recipient_id = str((messaging.get("recipient") or {}).get("id", ""))
                event_id = message.get("mid")
                text = message.get("text")
                if (
                    not sender_id
                    or not event_id
                    or not isinstance(text, str)
                    or message.get("is_echo")
                    or sender_id == self._external_account_id
                    or (self._external_account_id and recipient_id != self._external_account_id)
                ):
                    continue
                events.append(
                    CanonicalEvent(
                        platform=platform,
                        platform_account_key=account_key,
                        external_event_id=str(event_id),
                        external_user_id=sender_id,
                        conversation_key=f"{platform}_dm:{account_key}:{sender_id}",
                        text=text,
                        occurred_at=(
                            datetime.fromtimestamp(messaging["timestamp"] / 1000, tz=UTC)
                            if messaging.get("timestamp")
                            else None
                        ),
                        external_conversation_id=sender_id,
                        reply_target={"kind": "dm", "recipient_id": sender_id},
                        raw_payload=messaging,
                    )
                )
            for change in entry.get("changes", []):
                value = change.get("value") or {}
                if change.get("field") not in {"comments", "feed"}:
                    continue
                comment_id = value.get("id") or value.get("comment_id")
                sender_id = str(
                    (value.get("from") or {}).get("id")
                    or value.get("from_id")
                    or (value.get("user") or {}).get("id")
                    or ""
                )
                verb = value.get("verb") or value.get("action")
                text = value.get("message") or value.get("text")
                if (
                    not comment_id
                    or not sender_id
                    or sender_id == self._external_account_id
                    or not isinstance(text, str)
                    or value.get("is_hidden")
                    or verb in {"remove", "delete", "deleted"}
                ):
                    continue
                post_id = str(
                    value.get("post_id")
                    or value.get("media_id")
                    or (value.get("media") or {}).get("id")
                    or entry.get("id")
                )
                parent_id = str(value.get("parent_id") or comment_id)
                root_comment_id = str(value.get("root_comment_id") or parent_id)
                events.append(
                    CanonicalEvent(
                        platform=platform,
                        platform_account_key=account_key,
                        external_event_id=str(comment_id),
                        external_user_id=sender_id,
                        conversation_key=(
                            f"{platform}_comment:{account_key}:{post_id}:"
                            f"{root_comment_id}:{sender_id}"
                        ),
                        text=text,
                        channel_type=ChannelType.COMMENT,
                        reply_target={"kind": "comment", "comment_id": str(comment_id)},
                        raw_payload=change,
                    )
                )
        return events
