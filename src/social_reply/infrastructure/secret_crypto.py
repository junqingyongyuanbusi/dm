import json
from collections.abc import Mapping
from typing import Any

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

_ENVELOPE_KEY = "__encrypted__"


class SecretCipher:
    def __init__(self, keys: tuple[str, ...]) -> None:
        if not keys:
            raise ValueError("PLATFORM_SECRET_KEYS must contain at least one Fernet key")
        try:
            self._fernet = MultiFernet([Fernet(key.encode()) for key in keys])
        except (TypeError, ValueError) as exc:
            raise ValueError("PLATFORM_SECRET_KEYS contains an invalid Fernet key") from exc

    def encrypt(self, values: Mapping[str, Any] | None) -> dict | None:
        if values is None:
            return None
        plaintext = json.dumps(dict(values), separators=(",", ":"), sort_keys=True).encode()
        return {_ENVELOPE_KEY: self._fernet.encrypt(plaintext).decode()}

    def decrypt(self, envelope: Mapping[str, Any] | None) -> dict[str, str]:
        if not envelope:
            return {}
        token = envelope.get(_ENVELOPE_KEY)
        if not isinstance(token, str):
            raise ValueError("unencrypted_secret_bundle")
        try:
            value = json.loads(self._fernet.decrypt(token.encode()))
        except (InvalidToken, json.JSONDecodeError) as exc:
            raise ValueError("invalid_encrypted_secret_bundle") from exc
        if not isinstance(value, dict):
            raise ValueError("invalid_encrypted_secret_bundle")
        return {str(key): str(item) for key, item in value.items() if item is not None}


def encrypt_secret_bundle(values: Mapping[str, Any] | None) -> dict | None:
    from social_reply.shared.config import get_settings

    return SecretCipher(get_settings().platform_secret_key_list).encrypt(values)


def decrypt_secret_bundle(envelope: Mapping[str, Any] | None) -> dict[str, str]:
    from social_reply.shared.config import get_settings

    return SecretCipher(get_settings().platform_secret_key_list).decrypt(envelope)
