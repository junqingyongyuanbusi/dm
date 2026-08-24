import base64
import hashlib
import json
import secrets
from datetime import UTC, datetime
from typing import Any

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

FEISHU_MAX_BODY_BYTES = 1024 * 1024
FEISHU_REPLAY_WINDOW_SECONDS = 300
_BLOCK_SIZE = 16


class FeishuSecurityError(ValueError):
    def __init__(self, code: str = "invalid_feishu_request") -> None:
        super().__init__(code)
        self.code = code


def verify_signature(
    *,
    timestamp: str | None,
    nonce: str | None,
    signature: str | None,
    encrypt_key: str,
    body: bytes,
    now: datetime | None = None,
) -> None:
    if not timestamp or not nonce or not signature:
        raise FeishuSecurityError()
    try:
        request_timestamp = int(timestamp)
    except (TypeError, ValueError) as exc:
        raise FeishuSecurityError() from exc
    current_timestamp = int((now or datetime.now(UTC)).timestamp())
    if abs(current_timestamp - request_timestamp) > FEISHU_REPLAY_WINDOW_SECONDS:
        raise FeishuSecurityError("stale_feishu_request")
    expected = hashlib.sha256(
        timestamp.encode("utf-8") + nonce.encode("utf-8") + encrypt_key.encode("utf-8") + body
    ).hexdigest()
    if not secrets.compare_digest(expected, signature):
        raise FeishuSecurityError()


def decrypt_payload(encrypted: str, *, encrypt_key: str) -> dict[str, Any]:
    if not isinstance(encrypted, str) or not encrypted:
        raise FeishuSecurityError()
    try:
        decoded = base64.b64decode(encrypted, validate=True)
    except (ValueError, TypeError) as exc:
        raise FeishuSecurityError() from exc
    if len(decoded) < _BLOCK_SIZE * 2 or (len(decoded) - _BLOCK_SIZE) % _BLOCK_SIZE:
        raise FeishuSecurityError()
    iv = decoded[:_BLOCK_SIZE]
    ciphertext = decoded[_BLOCK_SIZE:]
    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    if not padded:
        raise FeishuSecurityError()
    padding_length = padded[-1]
    if padding_length < 1 or padding_length > _BLOCK_SIZE:
        raise FeishuSecurityError()
    expected_padding = bytes([padding_length]) * padding_length
    if not secrets.compare_digest(padded[-padding_length:], expected_padding):
        raise FeishuSecurityError()
    plaintext = padded[:-padding_length]
    try:
        value = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FeishuSecurityError() from exc
    if not isinstance(value, dict):
        raise FeishuSecurityError()
    return value


def parse_json_object(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FeishuSecurityError() from exc
    if not isinstance(value, dict):
        raise FeishuSecurityError()
    return value


def verify_token(actual: object, *, expected: str) -> None:
    if not isinstance(actual, str) or not secrets.compare_digest(actual, expected):
        raise FeishuSecurityError()
