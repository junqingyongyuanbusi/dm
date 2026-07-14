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


def test_non_ascii_signature_rejected_not_raising():
    # 攻击者可控输入不得抛异常（否则 500 而非 401）
    assert not verify_signature(
        secret=SECRET, timestamp="1000", body=BODY,
        signature="sha256=пример", now=1100, tolerance=300,
    )


def test_future_timestamp_clock_skew():
    # abs() 双向窗口：未来时间戳窗内放行、超窗拒绝
    assert verify_signature(
        secret=SECRET, timestamp="1200", body=BODY,
        signature=_sign("1200", BODY), now=1000, tolerance=300,
    )
    assert not verify_signature(
        secret=SECRET, timestamp="2000", body=BODY,
        signature=_sign("2000", BODY), now=1000, tolerance=300,
    )
