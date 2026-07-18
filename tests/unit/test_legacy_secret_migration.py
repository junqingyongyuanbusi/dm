from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

from social_reply.infrastructure.secret_crypto import SecretCipher

_spec = spec_from_file_location("migrate_legacy_secrets", Path("scripts/migrate_legacy_secrets.py"))
assert _spec is not None and _spec.loader is not None
_module = module_from_spec(_spec)
_spec.loader.exec_module(_module)
_is_encrypted = _module._is_encrypted
_file_values = _module._file_values

KEY_1 = "Wm5wbamjBFvTmkGIU2NskIKCrJfsb4AdUBDZR-m1-CM="
KEY_2 = "yASNyJyjNVx7X4qNSoNbYqm5qVTxvGnLu10AfwrRmjw="


def test_legacy_job_token_key_is_preserved(monkeypatch):
    monkeypatch.setattr(
        _module.secret_store,
        "read_mapping",
        lambda *_args, **_kwargs: {"token": "legacy"},
    )
    assert _file_values("file:///tmp/job", fallback_key="token") == {"token": "legacy"}
    assert _file_values(
        "file:///tmp/account", fallback_key="token", rename_token_to_bot_token=True
    ) == {"bot_token": "legacy"}


def test_unavailable_encryption_key_fails_hard(monkeypatch):
    envelope = SecretCipher((KEY_1,)).encrypt({"token": "secret"})
    monkeypatch.setenv("PLATFORM_SECRET_KEYS", KEY_2)
    from social_reply.shared.config import get_settings

    get_settings.cache_clear()
    with pytest.raises(ValueError, match="invalid_encrypted_secret_bundle"):
        _is_encrypted(envelope)
    get_settings.cache_clear()
