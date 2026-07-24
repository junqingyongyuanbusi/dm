import base64
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, load_der_public_key

from social_reply.connectors.xchat.crypto import import_private_key_b64


class XChatKeyState(StrEnum):
    NOT_REGISTERED = "NOT_REGISTERED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    READY = "READY"
    INVALID = "INVALID"


@dataclass(frozen=True)
class XChatState:
    key_state: XChatKeyState
    registered: bool
    public_key_version: str | None


def _version(record: dict[str, Any]) -> str | None:
    value = record.get("public_key_version") or record.get("version")
    return str(value) if value is not None and str(value) else None


def _version_sort_key(record: dict[str, Any]) -> tuple[int, str]:
    value = _version(record) or ""
    try:
        return int(value), value
    except ValueError:
        return -1, value


def registered_public_key_records(records: list[dict]) -> list[dict]:
    return [
        record
        for record in records
        if _version(record) and record.get("public_key") and record.get("signing_public_key")
    ]


def newest_public_key_record(records: list[dict]) -> dict | None:
    valid = registered_public_key_records(records)
    return max(valid, key=_version_sort_key) if valid else None


def _x962_public_key(value: object) -> bytes | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        public_key = load_der_public_key(base64.b64decode(value, validate=True))
    except (TypeError, ValueError):
        return None
    if not isinstance(public_key, EllipticCurvePublicKey):
        return None
    return public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)


def matching_public_key_version(private_keys_b64: str, records: list[dict]) -> str | None:
    chat = import_private_key_b64(private_keys_b64)
    local_keys = chat.get_public_keys()
    try:
        local_identity = base64.b64decode(local_keys.identity, validate=True)
        local_signing = base64.b64decode(local_keys.signing, validate=True)
    except (TypeError, ValueError):
        return None

    for record in registered_public_key_records(records):
        if (
            _x962_public_key(record.get("public_key")) == local_identity
            and _x962_public_key(record.get("signing_public_key")) == local_signing
        ):
            return _version(record)
    return None


def classify_xchat_state(
    records: list[dict],
    *,
    private_keys_b64: str | None,
) -> XChatState:
    registered = registered_public_key_records(records)
    if not registered:
        return XChatState(XChatKeyState.NOT_REGISTERED, False, None)

    if private_keys_b64:
        try:
            version = matching_public_key_version(private_keys_b64, registered)
        except (TypeError, ValueError):
            version = None
        if version:
            return XChatState(XChatKeyState.READY, True, version)
        return XChatState(
            XChatKeyState.INVALID,
            True,
            _version(newest_public_key_record(registered) or {}),
        )

    newest = newest_public_key_record(registered)
    if newest is not None and isinstance(newest.get("juicebox_config"), dict):
        return XChatState(XChatKeyState.RECOVERY_REQUIRED, True, _version(newest))
    return XChatState(XChatKeyState.INVALID, True, _version(newest or {}))


def xchat_state_config(state: XChatState, *, probed_at: str) -> dict[str, object]:
    return {
        "xchat_registered": state.registered,
        "xchat_key_state": state.key_state.value,
        "xchat_public_key_version": state.public_key_version,
        "xchat_last_probed_at": probed_at,
    }
