import asyncio
import re
import smtplib
import ssl
from collections.abc import Callable
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from social_reply.connectors.email.contracts import (
    AUTO_RESPONSE_SUPPRESS_HEADER,
    AUTO_RESPONSE_SUPPRESS_VALUE,
    AUTO_SUBMITTED_HEADER,
    AUTO_SUBMITTED_REPLY_VALUE,
    PLATFORM,
    SMTP_TIMEOUT_SECONDS,
)
from social_reply.connectors.errors import PermanentSendError, RetryableSendError

_REPLY_PREFIX = re.compile(r"^(?:re|回复)\s*[:：]", re.IGNORECASE)
_SMTPFactory = Callable[[str, int, float], smtplib.SMTP]


def _smtp_ssl_factory(host: str, port: int, timeout: float) -> smtplib.SMTP_SSL:
    context = ssl.create_default_context()
    return smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)


class EmailClient:
    platform = PLATFORM

    def __init__(
        self,
        *,
        smtp_host: str,
        smtp_port: int,
        username: str,
        password: str,
        self_address: str,
        from_name: str | None = None,
        smtp_factory: _SMTPFactory = _smtp_ssl_factory,
        timeout: float = SMTP_TIMEOUT_SECONDS,
    ) -> None:
        self._smtp_host = smtp_host
        self._smtp_port = smtp_port
        self._username = username
        self._password = password
        self._self_address = self_address
        self._from_name = from_name
        self._smtp_factory = smtp_factory
        self._timeout = timeout

    async def send_text(self, *, target: dict, text: str) -> str:
        recipient, recipient_name, subject, original_message_id, references = _target_fields(target)
        outbound_message_id = make_msgid(domain=_address_domain(self._self_address))
        message = EmailMessage()
        message["From"] = _formatted_address(self._from_name, self._self_address)
        message["To"] = _formatted_address(recipient_name, recipient)
        message["Subject"] = subject if _REPLY_PREFIX.match(subject) else f"Re: {subject}"
        message["In-Reply-To"] = original_message_id
        message["References"] = _append_reference(references, original_message_id)
        message[AUTO_SUBMITTED_HEADER] = AUTO_SUBMITTED_REPLY_VALUE
        message[AUTO_RESPONSE_SUPPRESS_HEADER] = AUTO_RESPONSE_SUPPRESS_VALUE
        message["Message-ID"] = outbound_message_id
        message.set_content(text)

        await asyncio.to_thread(self._send_message, message)
        return outbound_message_id.removeprefix("<").removesuffix(">")

    def _send_message(self, message: EmailMessage) -> None:
        smtp: smtplib.SMTP | None = None
        try:
            try:
                smtp = self._smtp_factory(self._smtp_host, self._smtp_port, self._timeout)
                smtp.login(self._username, self._password)
            except smtplib.SMTPResponseException as exc:
                raise _smtp_response_error(exc.smtp_code, exc.smtp_error) from exc
            except smtplib.SMTPNotSupportedError as exc:
                raise PermanentSendError("smtp_not_supported", _smtp_detail(str(exc))) from exc
            except (smtplib.SMTPException, OSError) as exc:
                raise RetryableSendError("smtp_transport", _smtp_detail(str(exc))) from exc

            try:
                smtp.send_message(message)
            except smtplib.SMTPRecipientsRefused as exc:
                raise _recipient_refusal_error(exc) from exc
            except smtplib.SMTPResponseException as exc:
                raise _smtp_response_error(exc.smtp_code, exc.smtp_error) from exc
            except smtplib.SMTPNotSupportedError as exc:
                raise PermanentSendError("smtp_not_supported", _smtp_detail(str(exc))) from exc
        finally:
            if smtp is not None:
                try:
                    smtp.quit()
                except (smtplib.SMTPException, OSError):
                    pass

    async def aclose(self) -> None:
        return None


def _target_fields(target: dict) -> tuple[str, str | None, str, str, str]:
    if target.get("kind") != PLATFORM:
        raise PermanentSendError("email_target_invalid", "unexpected target kind")

    recipient = target.get("to")
    recipient_name = target.get("to_name")
    subject = target.get("subject")
    message_id = target.get("message_id")
    references = target.get("references", "")
    if (
        not isinstance(recipient, str)
        or not recipient.strip()
        or (recipient_name is not None and not isinstance(recipient_name, str))
        or not isinstance(subject, str)
        or not isinstance(message_id, str)
        or not message_id.strip()
        or not isinstance(references, str)
    ):
        raise PermanentSendError("email_target_invalid", "missing or invalid target fields")
    return (
        recipient.strip(),
        recipient_name,
        subject.strip(),
        message_id.strip(),
        references.strip(),
    )


def _formatted_address(name: str | None, address: str) -> str:
    return formataddr((name, address)) if name else address


def _address_domain(address: str) -> str | None:
    domain = address.rpartition("@")[2].strip().lower()
    return domain or None


def _append_reference(references: str, message_id: str) -> str:
    existing = references.split()
    if message_id not in existing:
        existing.append(message_id)
    return " ".join(existing)


def _recipient_refusal_error(
    exc: smtplib.SMTPRecipientsRefused,
) -> PermanentSendError | RetryableSendError:
    responses = list(exc.recipients.values())
    temporary = next(
        (response for response in responses if 400 <= int(response[0]) < 500),
        None,
    )
    if temporary is not None:
        return RetryableSendError(f"smtp_{temporary[0]}", _smtp_detail(temporary[1]))
    if responses:
        code, detail = responses[0]
        return PermanentSendError(f"smtp_{code}", _smtp_detail(detail))
    return PermanentSendError("smtp_recipients_refused", "all recipients refused")


def _smtp_response_error(
    code: int,
    detail: bytes | str,
) -> PermanentSendError | RetryableSendError:
    error_type = PermanentSendError if code >= 500 else RetryableSendError
    return error_type(f"smtp_{code}", _smtp_detail(detail))


def _smtp_detail(value: bytes | str) -> str:
    if isinstance(value, bytes):
        detail = value.decode("utf-8", errors="replace")
    else:
        detail = value
    return " ".join(detail.split())[:300]
