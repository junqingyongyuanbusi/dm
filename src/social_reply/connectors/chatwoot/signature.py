import hashlib
import hmac


def verify_signature(
    *, secret: str, timestamp: str, body: bytes, signature: str, now: float, tolerance: int
) -> bool:
    """校验 Chatwoot v4.13+ Webhook 签名：sha256=HMAC-SHA256(secret, "{timestamp}.{body}")"""
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(now - ts) > tolerance:
        return False
    if not signature.startswith("sha256="):
        return False
    expected = hmac.new(
        secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(signature.removeprefix("sha256="), expected)
