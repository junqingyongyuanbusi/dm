import json

from chat_xdk import Chat

from social_reply.connectors.xchat.client import XChatClient
from social_reply.connectors.xchat.crypto import export_private_key_b64
from social_reply.connectors.xchat.state import (
    matching_public_key_version,
    newest_public_key_record,
)


class XChatKeyConfigurationError(ValueError):
    pass


class XChatKeyUnlockError(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _unlock_error_reason(message: str) -> str:
    normalized = message.lower().replace("_", " ")
    if "invalid pin" in normalized or "invalidpin" in normalized:
        return "invalid_pin"
    if "not registered" in normalized or "keys not registered" in normalized:
        return "not_registered"
    if "invalid auth" in normalized or "no juicebox tokens" in normalized:
        return "invalid_auth"
    if "upgrade required" in normalized:
        return "upgrade_required"
    if "rate limit" in normalized or "too many" in normalized:
        return "rate_limited"
    if any(
        marker in normalized
        for marker in ("transient", "timeout", "timed out", "storage failed", "internal error")
    ):
        return "temporarily_unavailable"
    return "recovery_failed"


async def unlock_xchat_private_keys(
    *,
    client: XChatClient,
    user_id: str,
    pin: str,
    records: list[dict] | None = None,
) -> tuple[str, str]:
    records = records if records is not None else await client.get_user_public_keys(user_id)
    if not records:
        raise ValueError("xchat_public_keys_not_found")
    record = newest_public_key_record(records)
    if record is None:
        raise ValueError("xchat_public_key_record_incomplete")
    juicebox = record.get("juicebox_config")
    version = record.get("public_key_version") or record.get("version")
    if not isinstance(juicebox, dict) or not version:
        raise ValueError("xchat_public_key_record_incomplete")
    try:
        chat = Chat(json.dumps(juicebox, separators=(",", ":")))
    except (ValueError, RuntimeError) as exc:
        raise XChatKeyConfigurationError("xchat_juicebox_config_invalid") from exc
    # PIN is deliberately used only for this unlock call and is never returned
    # to the caller or persisted; the exported private-key blob is stored instead.
    secret = bytearray(pin.encode())
    try:
        try:
            chat.unlock(secret)
        except (ValueError, RuntimeError) as exc:
            raise XChatKeyUnlockError(_unlock_error_reason(str(exc))) from exc
    finally:
        secret[:] = b"\x00" * len(secret)
    chat.set_key_version(str(version))
    private_keys_b64 = export_private_key_b64(chat)
    matched_version = matching_public_key_version(private_keys_b64, records)
    if matched_version is None:
        raise XChatKeyConfigurationError("xchat_public_key_mismatch")
    return private_keys_b64, matched_version
