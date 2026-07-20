import json

from chat_xdk import Chat

from social_reply.connectors.xchat.client import XChatClient
from social_reply.connectors.xchat.crypto import export_private_key_b64


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
    chat = Chat(json.dumps(juicebox, separators=(",", ":")))
    # PIN is deliberately used only for this unlock call and is never returned
    # to the caller or persisted; the exported private-key blob is stored instead.
    secret = bytearray(pin.encode())
    try:
        chat.unlock(secret)
    finally:
        secret[:] = b"\x00" * len(secret)
    chat.set_key_version(str(version))
    return export_private_key_b64(chat), str(version)
