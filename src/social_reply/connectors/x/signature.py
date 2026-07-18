import base64
import hashlib
import hmac


def crc_response(*, consumer_secret: str, crc_token: str) -> str:
    digest = hmac.new(consumer_secret.encode(), crc_token.encode(), hashlib.sha256).digest()
    return "sha256=" + base64.b64encode(digest).decode()


def verify_x_signature(*, consumer_secret: str, body: bytes, signature: str | None) -> bool:
    if not consumer_secret or not signature or not signature.startswith("sha256="):
        return False
    digest = hmac.new(consumer_secret.encode(), body, hashlib.sha256).digest()
    expected = "sha256=" + base64.b64encode(digest).decode()
    return hmac.compare_digest(signature, expected)
