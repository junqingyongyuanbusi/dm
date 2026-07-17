import hashlib
import hmac


def verify_meta_signature(*, app_secret: str, body: bytes, signature: str | None) -> bool:
    if not app_secret or not signature or not signature.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature.removeprefix("sha256="), expected)


def verify_meta_challenge(
    *, verify_token: str, mode: str | None, token: str | None, challenge: str | None
) -> str | None:
    if mode == "subscribe" and token == verify_token and challenge is not None:
        return challenge
    return None
