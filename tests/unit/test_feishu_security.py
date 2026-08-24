import base64
import hashlib
from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from social_reply.connectors.feishu.security import (
    FeishuSecurityError,
    decrypt_payload,
    parse_json_object,
    verify_signature,
    verify_token,
)

_ENCRYPT_KEY = "encrypt-key-fixture"
_TIMESTAMP = "1785729600"
_NONCE = "fixture-nonce"
_ENCRYPTED = (
    "AAECAwQFBgcICQoLDA0OD6Wm0rA1pj46VMFtFx7cQ2qVXMB+DDcl9BaOxRhdWFAv"
    "WH3CneyNwr9GeqVPGy14FxXCw2/rYbrZWWl6QowCesbs1wfuUT1qRR57hHLfMSlz"
)
_BODY = (f'{{"encrypt":"{_ENCRYPTED}"}}').encode()
_SIGNATURE = "86097db29784cbd077c9f324bbd8f29ec7af2897399a448b058339209425031c"
_NOW = datetime.fromtimestamp(int(_TIMESTAMP), tz=UTC)


def test_official_compatible_fixed_signature_and_aes_fixture():
    verify_signature(
        timestamp=_TIMESTAMP,
        nonce=_NONCE,
        signature=_SIGNATURE,
        encrypt_key=_ENCRYPT_KEY,
        body=_BODY,
        now=_NOW,
    )
    assert decrypt_payload(_ENCRYPTED, encrypt_key=_ENCRYPT_KEY) == {
        "header": {"app_id": "cli_fixture", "token": "verify-token"},
        "event": {"ok": True},
    }


@pytest.mark.parametrize("value", ["not-base64!", base64.b64encode(b"short").decode()])
def test_decrypt_rejects_invalid_base64_or_length(value):
    with pytest.raises(FeishuSecurityError, match="invalid_feishu_request"):
        decrypt_payload(value, encrypt_key=_ENCRYPT_KEY)


def test_decrypt_rejects_invalid_pkcs7_padding():
    plaintext = b"{}" + b"\x01" * 13 + b"\x02"
    encrypted = _encrypt_raw(plaintext)
    with pytest.raises(FeishuSecurityError, match="invalid_feishu_request"):
        decrypt_payload(encrypted, encrypt_key=_ENCRYPT_KEY)


def test_decrypt_rejects_invalid_utf8_and_json():
    for plaintext in (b"\xff", b"not-json"):
        encrypted = _encrypt_padded(plaintext)
        with pytest.raises(FeishuSecurityError, match="invalid_feishu_request"):
            decrypt_payload(encrypted, encrypt_key=_ENCRYPT_KEY)


def test_signature_accepts_replay_boundary():
    boundary_timestamp = str(int(_TIMESTAMP) - 300)
    boundary_signature = hashlib.sha256(
        boundary_timestamp.encode() + _NONCE.encode() + _ENCRYPT_KEY.encode() + _BODY
    ).hexdigest()
    verify_signature(
        timestamp=boundary_timestamp,
        nonce=_NONCE,
        signature=boundary_signature,
        encrypt_key=_ENCRYPT_KEY,
        body=_BODY,
        now=_NOW,
    )


def test_signature_rejects_mismatch_missing_headers_and_replay():
    for values in (
        {"signature": "0" * 64},
        {"nonce": None},
        {"timestamp": "not-a-timestamp"},
        {"timestamp": str(int(_TIMESTAMP) - 301)},
        {"timestamp": str(int(_TIMESTAMP) + 301)},
    ):
        arguments = {
            "timestamp": _TIMESTAMP,
            "nonce": _NONCE,
            "signature": _SIGNATURE,
            "encrypt_key": _ENCRYPT_KEY,
            "body": _BODY,
            "now": _NOW,
            **values,
        }
        with pytest.raises(FeishuSecurityError):
            verify_signature(**arguments)


def test_token_and_json_validation_fail_closed():
    verify_token("verify-token", expected="verify-token")
    for value in (None, "wrong"):
        with pytest.raises(FeishuSecurityError):
            verify_token(value, expected="verify-token")
    for body in (b"\xff", b"[]", b"not-json"):
        with pytest.raises(FeishuSecurityError):
            parse_json_object(body)


def _encrypt_padded(plaintext: bytes) -> str:
    padding_length = 16 - len(plaintext) % 16
    return _encrypt_raw(plaintext + bytes([padding_length]) * padding_length)


def _encrypt_raw(padded: bytes) -> str:
    iv = bytes(range(16))
    key = hashlib.sha256(_ENCRYPT_KEY.encode()).digest()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return base64.b64encode(iv + encryptor.update(padded) + encryptor.finalize()).decode()
