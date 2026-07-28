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

    def normalize_activity_dm(self, payload: dict) -> tuple[list[CanonicalEvent], str]:
        data = payload.get("data") or {}
        if data.get("event_type") != "dm.received":
            return [], "IGNORED_AT_INGRESS"
        event = data.get("payload")
        if not isinstance(event, dict):
            return [], "X_ACTIVITY_DM_SCHEMA_UNSUPPORTED"
        sender_id = str(event.get("sender_id") or "")
        event_id = event.get("id")
        text = event.get("text")
        if sender_id and sender_id == self._external_account_id:
            return [], "IGNORED_SELF_MESSAGE"
        if not sender_id or not event_id or not isinstance(text, str):
            return [], "X_ACTIVITY_DM_SCHEMA_UNSUPPORTED"
        dm_event_type = event.get("event_type")
        if dm_event_type not in (None, "MessageCreate", "message_create"):
            return [], "IGNORED_NON_MESSAGE_DM"
        conversation_id = str(event.get("dm_conversation_id") or sender_id)
        return [
            CanonicalEvent(
                platform=self.platform,
                platform_account_key=self._account_id,
                external_event_id=str(event_id),
                external_user_id=sender_id,
                conversation_key=f"x_dm:{self._account_id}:{sender_id}",
                text=text,
                occurred_at=_parse_time(event.get("created_at")),
                event_namespace="x.activity.dm_received",
                external_conversation_id=conversation_id,
                event_metadata={
                    "event_type": dm_event_type,
                    "activity_event_uuid": data.get("event_uuid"),
                    "dm_conversation_id": event.get("dm_conversation_id"),
                },
                reply_target={"kind": "dm", "participant_id": sender_id},
                raw_payload=event,
            )
        ], "PENDING"

    def normalize_activity_mention(self, payload: dict) -> tuple[list[CanonicalEvent], str]:
        """XAA ``post.mention.create``：有人在帖子里 @ 本账号。

        与旧 Account Activity API 的 ``tweet_create_events`` 不同，这里的事件包在
        ``data.payload`` 里，且带 ``conversation_id``。会话键按 thread 而非单条帖子，
        否则同一个对话里的每条回复都会变成孤立会话，拿不到上下文。
        """
        data = payload.get("data") or {}
        if data.get("event_type") != "post.mention.create":
            return [], "IGNORED_AT_INGRESS"
        event = data.get("payload")
        if not isinstance(event, dict):
            return [], "X_ACTIVITY_MENTION_SCHEMA_UNSUPPORTED"
        author_id = str(event.get("author_id") or "")
        event_id = event.get("id")
        text = event.get("text")
        # 本账号发的帖也会 @ 到自己（例如回复自己的 thread），不自接自答。
        if author_id and author_id == self._external_account_id:
            return [], "IGNORED_SELF_MENTION"
        if not author_id or not event_id or not isinstance(text, str) or not text.strip():
            return [], "X_ACTIVITY_MENTION_SCHEMA_UNSUPPORTED"
        conversation_id = str(event.get("conversation_id") or event_id)
        return [
            CanonicalEvent(
                platform=self.platform,
                platform_account_key=self._account_id,
                external_event_id=str(event_id),
                external_user_id=author_id,
                conversation_key=f"x_reply:{self._account_id}:{conversation_id}",
                text=text,
                channel_type=ChannelType.MENTION,
                occurred_at=_parse_time(event.get("created_at")),
                event_namespace="x.activity.post_mention_create",
                external_conversation_id=conversation_id,
                event_metadata={
                    "activity_event_uuid": data.get("event_uuid"),
                    "conversation_id": conversation_id,
                    # 发送时可能因为楼主限制了回复对象而失败，先存证以便事后定位。
                    "reply_settings": event.get("reply_settings"),
                    "lang": event.get("lang"),
                },
                reply_target={"kind": "reply", "in_reply_to_post_id": str(event_id)},
                raw_payload=event,
            )
        ], "PENDING"

    def normalize(self, payload: dict) -> list[CanonicalEvent]:
        events: list[CanonicalEvent] = []
        if (payload.get("data") or {}).get("event_type") == "dm.received":
            activity_events, _status = self.normalize_activity_dm(payload)
            events.extend(activity_events)
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
            event_type = event.get("event_type") or event.get("type")
            conversation_id = str(event.get("dm_conversation_id") or sender_id)
            if (
                not sender_id
                or not event_id
                or sender_id == self._external_account_id
                or event_type not in (None, "MessageCreate", "message_create")
                or not isinstance(text, str)
            ):
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
                        "event_type": event_type,
                        "dm_conversation_id": event.get("dm_conversation_id"),
                    },
                    reply_target={"kind": "dm", "participant_id": sender_id},
                    raw_payload=event,
                )
            )
        return events
