import hashlib
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from itertools import islice

from social_reply.connectors.email.contracts import (
    BULK_PRECEDENCE_VALUES,
    MAX_ATTACHMENT_CONTENT_TYPE_CHARS,
    MAX_ATTACHMENT_FILENAME_CHARS,
    MAX_ATTACHMENTS,
    MAX_INBOUND_TEXT_CHARS,
    MAX_MESSAGE_ID_BYTES,
    MAX_REFERENCE_IDS,
    MAX_REFERENCES_CHARS,
    MAX_SENDER_NAME_CHARS,
    MAX_SUBJECT_CHARS,
    PLATFORM,
    STATUS_IGNORED_AUTO_SUBMITTED,
    STATUS_IGNORED_BULK_LIST,
    STATUS_IGNORED_INTERNAL,
    STATUS_IGNORED_SELF,
    STATUS_IGNORED_SYSTEM_SENDER,
    STATUS_PENDING,
    STATUS_SCHEMA_UNSUPPORTED,
    SYSTEM_SENDER_LOCAL_PARTS,
    email_address_domain,
    email_address_identity_key,
    email_address_local_part,
    is_email_domain,
    normalize_email_address,
)
from social_reply.domain.messages.canonical import CanonicalEvent, ChannelType

_MESSAGE_ID_PATTERN = re.compile(r"<[^<>]+>")
_QUOTED_HISTORY_PATTERNS = (
    re.compile(r"(?im)^\s*On .+ wrote:\s*$"),
    re.compile(r"(?im)^\s*-{2,}\s*Original Message\s*-{2,}\s*$"),
)


class EmailInboundAdapter:
    platform = PLATFORM

    def __init__(
        self,
        *,
        account_id: str,
        self_address: str,
        internal_domain_policy: str = "ignore",
    ) -> None:
        self._account_id = account_id
        self._self_address = normalize_email_address(self_address)
        self._self_identity = email_address_identity_key(self._self_address)
        self._self_domain = email_address_domain(self._self_address)
        self._internal_domain_policy = internal_domain_policy

    def normalize_message(
        self,
        raw: bytes,
        *,
        uid: int,
        uidvalidity: int,
    ) -> tuple[list[CanonicalEvent], str]:
        try:
            message = BytesParser(policy=policy.default).parsebytes(raw)
        except (TypeError, ValueError):
            return [], STATUS_SCHEMA_UNSUPPORTED

        if _is_auto_submitted(message):
            return [], STATUS_IGNORED_AUTO_SUBMITTED
        if _is_bulk_or_list(message):
            return [], STATUS_IGNORED_BULK_LIST
        if _has_system_return_path(message):
            return [], STATUS_IGNORED_SYSTEM_SENDER

        sender_name, sender_address = parseaddr(str(message.get("From", "")))
        try:
            sender_address = normalize_email_address(sender_address)
        except ValueError:
            return [], STATUS_SCHEMA_UNSUPPORTED
        sender_name = sender_name.strip()[:MAX_SENDER_NAME_CHARS]

        sender_identity = email_address_identity_key(sender_address)
        if email_address_local_part(sender_address) in SYSTEM_SENDER_LOCAL_PARTS:
            return [], STATUS_IGNORED_SYSTEM_SENDER
        if sender_identity == self._self_identity:
            return [], STATUS_IGNORED_SELF
        if (
            self._internal_domain_policy != "allow"
            and self._self_domain
            and email_address_domain(sender_address) == self._self_domain
        ):
            return [], STATUS_IGNORED_INTERNAL

        subject = str(message.get("Subject", "")).strip()[:MAX_SUBJECT_CHARS]
        body = _extract_body(message)
        text = _combine_subject_and_body(subject, body)
        message_id, message_identity = _message_id(
            message,
            raw=raw,
            uid=uid,
            uidvalidity=uidvalidity,
            account_id=self._account_id,
            fallback_domain=self._self_domain,
        )
        references, reference_root = _message_id_list(
            str(message.get("References", "")),
            account_id=self._account_id,
            fallback_domain=self._self_domain,
        )
        _, in_reply_to_root = _message_id_list(
            str(message.get("In-Reply-To", "")),
            account_id=self._account_id,
            fallback_domain=self._self_domain,
        )
        thread_root = reference_root or in_reply_to_root or message_identity
        external_event_id = f"{uidvalidity}:{uid}"

        event = CanonicalEvent(
            platform=self.platform,
            platform_account_key=self._account_id,
            external_event_id=external_event_id,
            external_user_id=sender_identity,
            conversation_key=f"email:{self._account_id}:{sender_identity}:{thread_root}",
            text=text,
            occurred_at=_occurred_at(message),
            channel_type=ChannelType.DM,
            external_conversation_id=thread_root,
            reply_target={
                "kind": self.platform,
                "to": sender_address,
                "to_name": sender_name or None,
                "subject": subject,
                "message_id": message_id,
                "references": references,
                "thread_root": thread_root,
            },
            attachments=_attachments(message),
        )
        return [event], STATUS_PENDING


def _is_auto_submitted(message: Message) -> bool:
    auto_submitted = str(message.get("Auto-Submitted", "")).strip().lower()
    if auto_submitted and auto_submitted != "no":
        return True
    return message.get("X-Autoreply") is not None or message.get("X-Autorespond") is not None


def _is_bulk_or_list(message: Message) -> bool:
    precedence = str(message.get("Precedence", "")).strip().lower()
    return (
        precedence in BULK_PRECEDENCE_VALUES
        or message.get("List-Id") is not None
        or message.get("List-Unsubscribe") is not None
    )


def _has_system_return_path(message: Message) -> bool:
    value = str(message.get("Return-Path", "")).strip()
    if not value:
        return False
    if value == "<>":
        return True
    try:
        _, address = parseaddr(value)
        canonical_address = normalize_email_address(address)
    except ValueError:
        return False
    return email_address_local_part(canonical_address) in SYSTEM_SENDER_LOCAL_PARTS


def _message_id(
    message: Message,
    *,
    raw: bytes,
    uid: int,
    uidvalidity: int,
    account_id: str,
    fallback_domain: str,
) -> tuple[str, str]:
    value = str(message.get("Message-ID", "")).strip()
    if value:
        return _bounded_message_id(
            value,
            account_id=account_id,
            fallback_domain=fallback_domain,
        )

    digest = hashlib.sha256(f"{uidvalidity}:{uid}:".encode() + raw).hexdigest()
    normalized = f"email-{digest}"
    domain = _message_id_domain(fallback_domain)
    return f"<{normalized}@{domain}>", normalized


def _bounded_message_id(
    value: str,
    *,
    account_id: str,
    fallback_domain: str,
) -> tuple[str, str]:
    value = value.strip()
    normalized = _normalize_message_id(value)
    if _is_safe_message_id(normalized):
        return value, normalized

    domain = _message_id_domain(fallback_domain)
    digest = hashlib.sha256(
        f"{account_id}\0{domain}\0{value}".encode("utf-8", errors="replace")
    ).hexdigest()
    normalized = f"email-{digest}@{domain}"
    return f"<{normalized}>", normalized


def _normalize_message_id(value: str) -> str:
    value = value.strip()
    if value.startswith("<") and value.endswith(">"):
        return value[1:-1].strip()
    return value


def _is_safe_message_id(value: str) -> bool:
    return bool(
        value
        and len(value.encode("utf-8")) <= MAX_MESSAGE_ID_BYTES
        and not any(
            character.isspace() or ord(character) < 32 or character in "<>" for character in value
        )
    )


def _message_id_domain(value: str) -> str:
    value = value.strip().lower().removesuffix(".")
    return value if is_email_domain(value) else "reply-core.invalid"


def _message_id_list(
    value: str,
    *,
    account_id: str,
    fallback_domain: str,
) -> tuple[str, str | None]:
    normalized_ids: list[str] = []
    root: str | None = None
    current_length = 0
    for match in islice(_MESSAGE_ID_PATTERN.finditer(value), MAX_REFERENCE_IDS):
        reply_value, normalized = _bounded_message_id(
            match.group(0),
            account_id=account_id,
            fallback_domain=fallback_domain,
        )
        addition = len(reply_value) + (1 if normalized_ids else 0)
        if current_length + addition > MAX_REFERENCES_CHARS:
            break
        normalized_ids.append(reply_value)
        current_length += addition
        if root is None:
            root = normalized
    return " ".join(normalized_ids), root


def _occurred_at(message: Message) -> datetime | None:
    value = message.get("Date")
    if value is None:
        return None
    try:
        parsed = parsedate_to_datetime(str(value))
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _extract_body(message: Message) -> str:
    plain_parts: list[str] = []
    html_parts: list[str] = []
    for part in _body_parts(message):
        content_type = part.get_content_type().lower()
        if content_type not in {"text/plain", "text/html"}:
            continue
        content = _part_text(part)
        if not content.strip():
            continue
        if content_type == "text/plain":
            plain_parts.append(content)
        else:
            html_parts.append(content)

    if plain_parts:
        body = "\n".join(plain_parts)
    elif html_parts:
        extractor = _HTMLTextExtractor()
        extractor.feed("\n".join(html_parts))
        extractor.close()
        body = extractor.text()
    else:
        body = ""
    return _strip_quoted_history(body).strip()


def _body_parts(message: Message) -> Iterator[Message]:
    if _is_attachment(message) or message.get_content_type().lower() == "message/rfc822":
        return
    if message.is_multipart():
        payload = message.get_payload()
        if isinstance(payload, list):
            for part in payload:
                yield from _body_parts(part)
        return
    yield message


def _part_text(part: Message) -> str:
    try:
        content = part.get_content()
    except (LookupError, UnicodeDecodeError):
        payload = part.get_payload(decode=True) or b""
        return _decode_payload(payload, part.get_content_charset())
    if isinstance(content, bytes):
        return _decode_payload(content, part.get_content_charset())
    return str(content)


def _decode_payload(payload: bytes, charset: str | None) -> str:
    try:
        return payload.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _strip_quoted_history(value: str) -> str:
    boundary = len(value)
    for pattern in _QUOTED_HISTORY_PATTERNS:
        match = pattern.search(value)
        if match is not None:
            boundary = min(boundary, match.start())
    value = value[:boundary]
    return "\n".join(line for line in value.splitlines() if not line.lstrip().startswith(">"))


def _combine_subject_and_body(subject: str, body: str) -> str:
    if subject and body:
        text = f"{subject}\n\n{body}"
    else:
        text = subject or body
    return text.strip()[:MAX_INBOUND_TEXT_CHARS]


def _is_attachment(part: Message) -> bool:
    return (
        part.get_content_disposition() == "attachment"
        or part.get_filename() is not None
        or part.get_content_type().lower() == "text/calendar"
    )


def _attachment_parts(message: Message) -> Iterator[Message]:
    if _is_attachment(message):
        yield message
        return
    if not message.is_multipart():
        return
    payload = message.get_payload()
    if isinstance(payload, list):
        for part in payload:
            yield from _attachment_parts(part)


def _attachments(message: Message) -> list[dict[str, str]]:
    return [
        {
            "filename": (part.get_filename() or "")[:MAX_ATTACHMENT_FILENAME_CHARS],
            "content_type": part.get_content_type().lower()[:MAX_ATTACHMENT_CONTENT_TYPE_CHARS],
        }
        for part in islice(_attachment_parts(message), MAX_ATTACHMENTS)
    ]


class _HTMLTextExtractor(HTMLParser):
    _BLOCK_TAGS = frozenset(
        {
            "address",
            "article",
            "blockquote",
            "br",
            "div",
            "footer",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "header",
            "li",
            "p",
            "section",
            "tr",
        }
    )
    _SKIPPED_TAGS = frozenset({"head", "script", "style"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in self._SKIPPED_TAGS:
            self._skip_depth += 1
        elif self._skip_depth == 0 and tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIPPED_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif self._skip_depth == 0 and tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._chunks.append(data)

    def text(self) -> str:
        lines = [" ".join(line.split()) for line in "".join(self._chunks).splitlines()]
        return "\n".join(line for line in lines if line)
