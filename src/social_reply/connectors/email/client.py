import asyncio
import re
import smtplib
import ssl
from collections.abc import Callable
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import Protocol

from social_reply.connectors.email.contracts import (
    AUTO_RESPONSE_SUPPRESS_HEADER,
    AUTO_RESPONSE_SUPPRESS_VALUE,
    AUTO_SUBMITTED_HEADER,
    AUTO_SUBMITTED_REPLY_VALUE,
    PLATFORM,
    SMTP_TIMEOUT_SECONDS,
    normalize_email_address,
)
from social_reply.connectors.email.network import (
    DEFAULT_EMAIL_ALLOWED_HOSTS,
    EmailNetworkError,
    normalize_allowed_hosts,
    normalize_hostname,
    require_allowed_host,
    resolve_public_target,
    validate_port,
)
from social_reply.connectors.errors import PermanentSendError, RetryableSendError

_REPLY_PREFIX = re.compile(r"^(?:re|回复)\s*[:：]", re.IGNORECASE)


class _SMTPConnection(Protocol):
    def ehlo(self) -> tuple[int, bytes]: ...

    def starttls(self, *, context: ssl.SSLContext) -> tuple[int, bytes]: ...

    def login(self, user: str, password: str) -> tuple[int, bytes]: ...

    def send_message(self, message: EmailMessage) -> dict: ...

    def quit(self) -> tuple[int, bytes]: ...


type _SMTPFactory = Callable[[str, int, float], _SMTPConnection]
type _NetworkValidator = Callable[[str, int], object]


def _smtp_ssl_factory(host: str, port: int, timeout: float) -> smtplib.SMTP_SSL:
    context = ssl.create_default_context()
    return smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)


def _smtp_starttls_factory(host: str, port: int, timeout: float) -> smtplib.SMTP:
    return smtplib.SMTP(host, port, timeout=timeout)


class EmailClient:
    platform = PLATFORM

    def __init__(
        self,
        *,
        smtp_host: str,
        smtp_port: int,
        smtp_security: str,
        username: str,
        password: str,
        self_address: str,
        from_name: str | None = None,
        smtp_factory: _SMTPFactory | None = None,
        network_validator: _NetworkValidator = resolve_public_target,
        allowed_hosts: frozenset[str] = DEFAULT_EMAIL_ALLOWED_HOSTS,
        timeout: float = SMTP_TIMEOUT_SECONDS,
    ) -> None:
        try:
            self._smtp_host = normalize_hostname(smtp_host)
            self._smtp_port = validate_port(smtp_port)
        except EmailNetworkError as exc:
            raise PermanentSendError(exc.code) from exc
        if smtp_security not in {"ssl", "starttls"}:
            raise ValueError("smtp_security_invalid")
        self._smtp_security = smtp_security
        self._username = username
        self._password = password
        self._self_address = normalize_email_address(self_address)
        self._from_name = from_name
        self._smtp_factory = smtp_factory or (
            _smtp_ssl_factory if smtp_security == "ssl" else _smtp_starttls_factory
        )
        self._network_validator = network_validator
        self._allowed_hosts = normalize_allowed_hosts(allowed_hosts)
        self._timeout = timeout

    async def probe(self) -> None:
        """Verify the SMTP transport and credentials without sending a message."""

        await asyncio.to_thread(self._probe)

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

    def _probe(self) -> None:
        smtp: _SMTPConnection | None = None
        try:
            smtp = self._connect_authenticated()
        finally:
            _best_effort_quit(smtp)

    def _send_message(self, message: EmailMessage) -> None:
        smtp: _SMTPConnection | None = None
        try:
            smtp = self._connect_authenticated()
            try:
                smtp.send_message(message)
            except smtplib.SMTPRecipientsRefused as exc:
                raise _recipient_refusal_error(exc) from exc
            except smtplib.SMTPResponseException as exc:
                raise _smtp_response_error(exc.smtp_code, exc.smtp_error) from exc
            except smtplib.SMTPNotSupportedError as exc:
                raise PermanentSendError("smtp_not_supported") from exc
        finally:
            _best_effort_quit(smtp)

    def _connect_authenticated(self) -> _SMTPConnection:
        try:
            # Enforce deployment policy before DNS and re-resolve fail-closed immediately
            # before every high-level SMTP connection.
            require_allowed_host(self._smtp_host, self._allowed_hosts)
            self._network_validator(self._smtp_host, self._smtp_port)
            smtp = self._smtp_factory(self._smtp_host, self._smtp_port, self._timeout)
            try:
                if self._smtp_security == "starttls":
                    _require_smtp_success(smtp.ehlo())
                    context = ssl.create_default_context()
                    _require_smtp_success(smtp.starttls(context=context))
                    _require_smtp_success(smtp.ehlo())
                _require_smtp_success(smtp.login(self._username, self._password))
            except Exception:
                _best_effort_quit(smtp)
                raise
            return smtp
        except EmailNetworkError as exc:
            raise _email_network_error(exc) from exc
        except ssl.SSLError as exc:
            raise PermanentSendError("smtp_tls_invalid") from exc
        except smtplib.SMTPResponseException as exc:
            raise _smtp_response_error(exc.smtp_code, exc.smtp_error) from exc
        except smtplib.SMTPNotSupportedError as exc:
            raise PermanentSendError("smtp_not_supported") from exc
        except (smtplib.SMTPException, OSError) as exc:
            raise RetryableSendError("smtp_transport") from exc

    async def aclose(self) -> None:
        return None


def _best_effort_quit(smtp: _SMTPConnection | None) -> None:
    if smtp is None:
        return
    try:
        smtp.quit()
    except (smtplib.SMTPException, OSError):
        pass


def _require_smtp_success(response: tuple[int, bytes]) -> None:
    code, detail = response
    if not 200 <= code < 300:
        raise smtplib.SMTPResponseException(code, detail)


def _email_network_error(exc: EmailNetworkError) -> PermanentSendError | RetryableSendError:
    if exc.code == "email_dns_resolution_failed":
        return RetryableSendError(exc.code)
    return PermanentSendError(exc.code)


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
        return RetryableSendError(f"smtp_{temporary[0]}")
    if responses:
        code, _detail = responses[0]
        return PermanentSendError(f"smtp_{code}")
    return PermanentSendError("smtp_recipients_refused", "all recipients refused")


def _smtp_response_error(
    code: int,
    detail: bytes | str,
) -> PermanentSendError | RetryableSendError:
    error_type = PermanentSendError if code >= 500 else RetryableSendError
    return error_type(f"smtp_{code}")
