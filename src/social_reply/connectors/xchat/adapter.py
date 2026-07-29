from datetime import UTC, datetime

from social_reply.domain.messages.canonical import CanonicalEvent


def _event_text(event: dict) -> str | None:
    if event.get("type") != "Message":
        return None
    content = event.get("content") or {}
    if content.get("content_type") != "Text":
        return None
    return content.get("text") or ""


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _parse_msec(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC)
    except (TypeError, ValueError, OSError):
        return None


def canonical_from_decrypted(
    *,
    account_id: str,
    external_account_id: str,
    envelope: dict,
    event: dict,
) -> CanonicalEvent | None:
    sender_id = str(event.get("sender_id") or envelope.get("sender_id") or "")
    event_id = str(event.get("message_id") or event.get("id") or envelope.get("id") or "")
    conversation_id = str(envelope.get("conversation_id") or event.get("conversation_id") or "")
    text = _event_text(event)
    if event.get("verified") is not True:
        return None
    if (
        not sender_id
        or not event_id
        or not conversation_id
        or sender_id == external_account_id
        or text is None
    ):
        return None
    return CanonicalEvent(
        platform="x",
        platform_account_key=account_id,
        external_event_id=event_id,
        external_user_id=sender_id,
        conversation_key=f"x_chat:{account_id}:{conversation_id}",
        text=text,
        occurred_at=(
            _parse_time(envelope.get("created_at")) or _parse_msec(envelope.get("created_at_msec"))
        ),
        event_namespace="x.xchat",
        external_conversation_id=conversation_id,
        event_metadata={
            "envelope_id": envelope.get("id"),
            "previous_id": envelope.get("previous_id"),
            "is_trusted": envelope.get("is_trusted"),
        },
        reply_target={
            "kind": "x_chat",
            "conversation_id": conversation_id,
            "conversation_token": envelope.get("conversation_token"),
        },
        raw_payload={"envelope": envelope, "decrypted_event": event},
    )
