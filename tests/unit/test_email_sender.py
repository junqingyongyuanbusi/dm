"""EmailClient（SMTP）：MIME 组装、线程头、RFC 3834 防循环头与错误分类。"""

import smtplib
import ssl

import pytest

from social_reply.connectors.email.client import EmailClient, _smtp_ssl_factory
from social_reply.connectors.errors import PermanentSendError, RetryableSendError


class _FakeSmtp:
    """捕获 send_message 的最小 SMTP stub，支持按需抛错。"""

    def __init__(
        self,
        *,
        raise_on_login: Exception | None = None,
        raise_on_send: Exception | None = None,
    ) -> None:
        self.logins: list[tuple[str, str]] = []
        self.sent = []
        self.quit_called = False
        self._raise_on_login = raise_on_login
        self._raise_on_send = raise_on_send

    def login(self, username: str, password: str) -> None:
        if self._raise_on_login is not None:
            raise self._raise_on_login
        self.logins.append((username, password))

    def send_message(self, message) -> dict:
        if self._raise_on_send is not None:
            raise self._raise_on_send
        self.sent.append(message)
        return {}

    def quit(self) -> None:
        self.quit_called = True


def _client(fake: _FakeSmtp, **overrides) -> EmailClient:
    kwargs = {
        "smtp_host": "smtp.larksuite.com",
        "smtp_port": 465,
        "username": "support@corp.com",
        "password": "app-password",
        "self_address": "support@corp.com",
        "from_name": "Corp 客服",
        "smtp_factory": lambda host, port, timeout: fake,
    }
    kwargs.update(overrides)
    return EmailClient(**kwargs)


def _target(**overrides) -> dict:
    target = {
        "kind": "email",
        "to": "alice@example.com",
        "to_name": "Alice",
        "subject": "退款咨询",
        "message_id": "<msg-1@example.com>",
        "references": "",
    }
    target.update(overrides)
    return target


def test_email_client_default_factory_verifies_tls(monkeypatch):
    seen = {}
    sentinel = object()

    def smtp_ssl(host, port, *, timeout, context):
        seen.update(host=host, port=port, timeout=timeout, context=context)
        return sentinel

    monkeypatch.setattr(smtplib, "SMTP_SSL", smtp_ssl)

    assert _smtp_ssl_factory("smtp.example.com", 465, 7.0) is sentinel
    assert seen["host"] == "smtp.example.com"
    assert seen["port"] == 465
    assert seen["timeout"] == 7.0
    assert seen["context"].check_hostname is True
    assert seen["context"].verify_mode == ssl.CERT_REQUIRED


async def test_email_client_sends_threaded_reply_with_loop_protection_headers():
    fake = _FakeSmtp()
    client = _client(fake)

    message_id = await client.send_text(target=_target(), text="您好，退款流程如下……")

    assert fake.logins == [("support@corp.com", "app-password")]
    assert fake.quit_called
    [message] = fake.sent
    assert message["To"] == "Alice <alice@example.com>"
    assert message["From"] == "Corp 客服 <support@corp.com>"
    assert message["Subject"] == "Re: 退款咨询"
    assert message["In-Reply-To"] == "<msg-1@example.com>"
    assert message["References"] == "<msg-1@example.com>"
    assert message["Auto-Submitted"] == "auto-replied"
    assert message["X-Auto-Response-Suppress"] == "OOF, AutoReply"
    assert "退款流程如下" in message.get_content()
    assert message_id
    assert message["Message-ID"] == f"<{message_id}>"


async def test_email_client_does_not_stack_reply_prefixes():
    fake = _FakeSmtp()
    client = _client(fake)

    await client.send_text(target=_target(subject="Re: 退款咨询"), text="好的")
    await client.send_text(target=_target(subject="回复: 退款咨询"), text="好的")

    assert fake.sent[0]["Subject"] == "Re: 退款咨询"
    assert fake.sent[1]["Subject"] == "回复: 退款咨询"


async def test_email_client_appends_original_message_to_references():
    fake = _FakeSmtp()
    client = _client(fake)

    await client.send_text(
        target=_target(
            message_id="<msg-3@example.com>",
            references="<msg-1@example.com> <msg-2@corp.com>",
        ),
        text="好的",
    )

    assert fake.sent[0]["References"] == "<msg-1@example.com> <msg-2@corp.com> <msg-3@example.com>"


async def test_email_client_maps_smtp_5xx_to_permanent_error():
    fake = _FakeSmtp(raise_on_send=smtplib.SMTPResponseException(550, b"mailbox unavailable"))
    client = _client(fake)

    with pytest.raises(PermanentSendError) as excinfo:
        await client.send_text(target=_target(), text="hi")
    assert excinfo.value.code == "smtp_550"


async def test_email_client_maps_smtp_4xx_to_retryable_error():
    fake = _FakeSmtp(raise_on_send=smtplib.SMTPResponseException(451, b"try later"))
    client = _client(fake)

    with pytest.raises(RetryableSendError) as excinfo:
        await client.send_text(target=_target(), text="hi")
    assert excinfo.value.code == "smtp_451"


async def test_email_client_maps_permanent_recipient_refusal():
    fake = _FakeSmtp(
        raise_on_send=smtplib.SMTPRecipientsRefused({"alice@example.com": (550, b"user unknown")})
    )
    client = _client(fake)

    with pytest.raises(PermanentSendError) as excinfo:
        await client.send_text(target=_target(), text="hi")
    assert excinfo.value.code == "smtp_550"


async def test_email_client_maps_temporary_recipient_refusal_to_retryable():
    fake = _FakeSmtp(
        raise_on_send=smtplib.SMTPRecipientsRefused({"alice@example.com": (450, b"try later")})
    )
    client = _client(fake)

    with pytest.raises(RetryableSendError) as excinfo:
        await client.send_text(target=_target(), text="hi")
    assert excinfo.value.code == "smtp_450"


async def test_email_client_maps_pre_dispatch_transport_error_to_retryable():
    fake = _FakeSmtp(raise_on_login=OSError("connection reset"))
    client = _client(fake)

    with pytest.raises(RetryableSendError) as excinfo:
        await client.send_text(target=_target(), text="hi")
    assert excinfo.value.code == "smtp_transport"


async def test_email_client_leaves_send_disconnect_ambiguous():
    fake = _FakeSmtp(raise_on_send=smtplib.SMTPServerDisconnected("connection reset"))
    client = _client(fake)

    with pytest.raises(smtplib.SMTPServerDisconnected):
        await client.send_text(target=_target(), text="hi")


async def test_email_client_missing_target_fields_is_permanent():
    fake = _FakeSmtp()
    client = _client(fake)

    with pytest.raises(PermanentSendError):
        await client.send_text(target={"kind": "email"}, text="hi")


async def test_email_client_aclose_is_supported():
    await _client(_FakeSmtp()).aclose()


def test_email_client_declares_platform():
    assert EmailClient.platform == "email"
