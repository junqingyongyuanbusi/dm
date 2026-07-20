import base64
from collections.abc import Iterable

from chat_xdk import Chat


def export_private_key_b64(chat: Chat) -> str:
    exported = chat.export_keys()
    if exported is None:
        raise ValueError("xchat_private_keys_missing")
    try:
        return base64.b64encode(bytes(exported)).decode()
    finally:
        exported[:] = b"\x00" * len(exported)


def import_private_key_b64(value: str) -> Chat:
    raw = bytearray(base64.b64decode(value, validate=True))
    try:
        chat = Chat()
        chat.set_reject_unverified(True)
        # The PyO3 binding currently accepts immutable bytes only. Keep the
        # mutable source short-lived and wipe it immediately after import.
        chat.import_keys(bytes(raw))
        return chat
    finally:
        raw[:] = b"\x00" * len(raw)


def signing_key_entries(user_id: str, records: Iterable[dict]) -> list[dict]:
    entries: list[dict] = []
    for record in records:
        version = record.get("public_key_version") or record.get("version")
        signing_key = record.get("signing_public_key")
        identity_key = record.get("public_key")
        binding = record.get("identity_public_key_signature")
        if not all((version, signing_key, identity_key, binding)):
            continue
        entries.append(
            {
                "user_id": str(user_id),
                "public_key_version": str(version),
                # Chat XDK calls the signing public key simply ``public_key``.
                "public_key": str(signing_key),
                "identity_public_key": str(identity_key),
                "identity_public_key_signature": str(binding),
            }
        )
    return entries


def decrypt_history(
    *,
    private_keys_b64: str,
    message_events: list[dict],
    key_change_events: list[str],
    signing_keys: list[dict],
) -> tuple[list[dict], dict, dict]:
    """Decrypt a Chat history page and return plaintext events plus key material.

    The returned conversation-key mapping is kept in memory by the caller only;
    it must never be serialized into logs or RawEvent payloads.
    """

    chat = import_private_key_b64(private_keys_b64)
    extracted = chat.extract_conversation_keys(key_change_events)
    conversation_keys = dict(extracted.get("keys") or {})
    decrypted: list[dict] = []
    errors: dict[str, str] = {}
    for index, item in enumerate(message_events):
        encoded = item.get("encoded_event")
        if not encoded:
            continue
        try:
            event = chat.decrypt_event(encoded, conversation_keys, signing_keys)
        except Exception as exc:  # noqa: BLE001 - native XDK raises extension exceptions
            errors[str(index)] = f"{type(exc).__name__}:{exc}"
            continue
        decrypted.append({"envelope": item, "event": event})
    return decrypted, extracted, errors


def decrypt_live_event(
    *,
    private_keys_b64: str,
    payload: dict,
    signing_keys: list[dict],
) -> dict:
    chat = import_private_key_b64(private_keys_b64)
    key_change = payload.get("conversation_key_change_event")
    extracted = chat.extract_conversation_keys([key_change] if key_change else [])
    return chat.decrypt_event(
        payload["encoded_event"],
        dict(extracted.get("keys") or {}),
        signing_keys,
    )
