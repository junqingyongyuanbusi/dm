from datetime import UTC, datetime

from social_reply.domain.messages.canonical import CanonicalEvent


class TelegramWebhookAdapter:
    platform = "telegram"

    def __init__(self, *, account_id: str, secret: str) -> None:
        self._account_id = account_id
        self._secret = secret

    def verify(self, *, headers: dict[str, str], body: bytes) -> bool:
        return headers.get("x-telegram-bot-api-secret-token", "") == self._secret

    def normalize(self, payload: dict) -> list[CanonicalEvent]:
        update_id = payload.get("update_id")
        message = payload.get("message") or payload.get("edited_message")
        if update_id is None or not isinstance(message, dict):
            return []
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        chat_id = chat.get("id")
        message_id = message.get("message_id")
        user_id = sender.get("id")
        text = message.get("text") or message.get("caption")
        attachments = []
        for kind in ("photo", "document", "audio", "voice", "video", "animation", "sticker"):
            raw = message.get(kind)
            values = raw if isinstance(raw, list) else [raw] if isinstance(raw, dict) else []
            for item in values[-1:]:
                attachments.append(
                    {
                        "type": kind,
                        "platform_id": item.get("file_id"),
                        "metadata": {
                            key: item.get(key)
                            for key in ("file_unique_id", "file_name", "mime_type", "file_size")
                            if item.get(key) is not None
                        },
                    }
                )
        if (
            chat_id is None
            or message_id is None
            or user_id is None
            or (not isinstance(text, str) and not attachments)
        ):
            return []
        occurred_at = None
        if message.get("date") is not None:
            occurred_at = datetime.fromtimestamp(message["date"], tz=UTC)
        return [
            CanonicalEvent(
                platform=self.platform,
                platform_account_key=self._account_id,
                external_event_id=str(update_id),
                external_user_id=str(user_id),
                conversation_key=f"telegram:{self._account_id}:{chat_id}",
                text=text,
                occurred_at=occurred_at,
                external_conversation_id=str(chat_id),
                reply_target={"chat_id": chat_id},
                attachments=attachments,
                raw_payload=payload,
            )
        ]
