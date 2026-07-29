from datetime import UTC, datetime

from social_reply.domain.messages.canonical import CanonicalEvent


class WhatsAppWebhookAdapter:
    platform = "whatsapp"

    def __init__(self, *, account_id: str, phone_number_id: str) -> None:
        self._account_id = account_id
        self._phone_number_id = phone_number_id

    def normalize(self, payload: dict) -> list[CanonicalEvent]:
        events: list[CanonicalEvent] = []
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value") or {}
                metadata = value.get("metadata") or {}
                phone_number_id = str(metadata.get("phone_number_id", ""))
                if phone_number_id != self._phone_number_id:
                    continue
                for message in value.get("messages", []):
                    sender_id = str(message.get("from", ""))
                    event_id = message.get("id")
                    if not phone_number_id or not sender_id or not event_id:
                        continue
                    text = (message.get("text") or {}).get("body")
                    if not isinstance(text, str):
                        continue
                    occurred_at = None
                    if message.get("timestamp"):
                        occurred_at = datetime.fromtimestamp(int(message["timestamp"]), tz=UTC)
                    events.append(
                        CanonicalEvent(
                            platform=self.platform,
                            platform_account_key=self._account_id,
                            external_event_id=str(event_id),
                            external_user_id=sender_id,
                            conversation_key=f"whatsapp:{self._account_id}:{sender_id}",
                            text=text,
                            occurred_at=occurred_at,
                            external_conversation_id=sender_id,
                            reply_target={
                                "kind": "session_message",
                                "phone_number_id": phone_number_id,
                                "to": sender_id,
                            },
                            raw_payload=message,
                        )
                    )
        return events
