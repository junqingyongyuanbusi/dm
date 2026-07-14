import hashlib
import hmac

from social_reply.connectors.chatwoot.signature import verify_signature

SECRET = "s3cret"
BODY = b'{"event":"message_created"}'


def _sign(ts: str, body: bytes, secret: str = SECRET) -> str:
    digest = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def test_valid_signature_passes():
    assert verify_signature(
        secret=SECRET, timestamp="1000", body=BODY,
        signature=_sign("1000", BODY), now=1100, tolerance=300,
    )


def test_wrong_signature_rejected():
    assert not verify_signature(
        secret=SECRET, timestamp="1000", body=BODY,
        signature=_sign("1000", BODY, secret="other"), now=1100, tolerance=300,
    )


def test_stale_timestamp_rejected():
    # PLAN.md §十七：时间戳容忍窗口，超窗拒绝以防重放
    assert not verify_signature(
        secret=SECRET, timestamp="1000", body=BODY,
        signature=_sign("1000", BODY), now=2000, tolerance=300,
    )


def test_malformed_header_rejected():
    assert not verify_signature(
        secret=SECRET, timestamp="1000", body=BODY,
        signature="not-a-signature", now=1100, tolerance=300,
    )
    assert not verify_signature(
        secret=SECRET, timestamp="abc", body=BODY,
        signature=_sign("1000", BODY), now=1100, tolerance=300,
    )
