import pytest

from social_reply.infrastructure.secret_crypto import SecretCipher

KEY_1 = "Wm5wbamjBFvTmkGIU2NskIKCrJfsb4AdUBDZR-m1-CM="
KEY_2 = "yASNyJyjNVx7X4qNSoNbYqm5qVTxvGnLu10AfwrRmjw="


def test_secret_cipher_round_trip_and_no_plaintext():
    cipher = SecretCipher((KEY_1,))
    encrypted = cipher.encrypt({"token": "super-secret"})
    assert encrypted is not None
    assert "super-secret" not in str(encrypted)
    assert cipher.decrypt(encrypted) == {"token": "super-secret"}


def test_secret_cipher_supports_key_rotation():
    old = SecretCipher((KEY_1,)).encrypt({"token": "old"})
    assert SecretCipher((KEY_2, KEY_1)).decrypt(old) == {"token": "old"}


def test_secret_cipher_rejects_plaintext_bundle():
    with pytest.raises(ValueError, match="unencrypted_secret_bundle"):
        SecretCipher((KEY_1,)).decrypt({"token": "plaintext"})
