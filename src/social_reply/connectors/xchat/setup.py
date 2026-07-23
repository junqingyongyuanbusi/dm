import json

from chat_xdk import Chat

from social_reply.connectors.xchat.client import XChatClient
from social_reply.connectors.xchat.crypto import export_private_key_b64


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
) -> tuple[str, str]:
    records = await client.get_user_public_keys(user_id)
    if not records:
        raise ValueError("xchat_public_keys_not_found")
    record = records[0]
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
    return export_private_key_b64(chat), str(version)
