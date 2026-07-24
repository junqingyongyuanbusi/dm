from datetime import UTC, datetime

from social_reply.domain.messages.canonical import CanonicalEvent, ChannelType


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


class XWebhookAdapter:
    platform = "x"

    def __init__(self, *, account_id: str, external_account_id: str | None = None) -> None:
        self._account_id = account_id
        self._external_account_id = external_account_id

    def normalize(self, payload: dict) -> list[CanonicalEvent]:
        events: list[CanonicalEvent] = []
        # X DM 事件：新版 key=direct_message_events，旧版=dm_events，两者都兼容
        for event in payload.get("direct_message_events", []) + payload.get("dm_events", []):
            # 新格式 sender/text 嵌在 message_create 内；旧格式在事件顶层。
            message_create = event.get("message_create") or {}
            sender_id = str(message_create.get("sender_id") or event.get("sender_id") or "")
            text = (
                (message_create.get("message_data") or {}).get("text")
                or message_create.get("text")
                or event.get("text")
            )
            event_id = event.get("id")
            conversation_id = str(event.get("dm_conversation_id") or sender_id)
            if not sender_id or not event_id or sender_id == self._external_account_id:
                continue
            events.append(
                CanonicalEvent(
                    platform=self.platform,
                    platform_account_key=self._account_id,
                    external_event_id=str(event_id),
                    external_user_id=sender_id,
                    conversation_key=f"x_dm:{self._account_id}:{sender_id}",
                    text=text,
                    occurred_at=_parse_time(event.get("created_at")),
                    event_namespace="x.legacy_dm",
                    external_conversation_id=conversation_id,
                    event_metadata={
                        "event_type": event.get("event_type") or event.get("type"),
                        "dm_conversation_id": event.get("dm_conversation_id"),
                    },
                    reply_target={"kind": "dm", "participant_id": sender_id},
                    raw_payload=event,
                )
            )
        for event in payload.get("tweet_create_events", []) + payload.get("post_create_events", []):
            author_id = str(event.get("user_id_str") or event.get("author_id") or "")
            event_id = event.get("id_str") or event.get("id")
            if not author_id or not event_id or author_id == self._external_account_id:
                continue
            events.append(
                CanonicalEvent(
                    platform=self.platform,
                    platform_account_key=self._account_id,
                    external_event_id=str(event_id),
                    external_user_id=author_id,
                    conversation_key=f"x_reply:{self._account_id}:{event_id}",
                    text=event.get("text"),
                    channel_type=ChannelType.MENTION,
                    event_namespace="x.activity",
                    reply_target={"kind": "reply", "in_reply_to_post_id": str(event_id)},
                    raw_payload=event,
                )
            )
        return events
