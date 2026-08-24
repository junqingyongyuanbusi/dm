"""Shared protocol constants and validation for the email connector."""

import re
import unicodedata

PLATFORM = "email"

STATUS_PENDING = "PENDING"
STATUS_SCHEMA_UNSUPPORTED = "EMAIL_SCHEMA_UNSUPPORTED"
STATUS_IGNORED_AUTO_SUBMITTED = "IGNORED_AUTO_SUBMITTED"
STATUS_IGNORED_BULK_LIST = "IGNORED_BULK_LIST"
STATUS_IGNORED_SYSTEM_SENDER = "IGNORED_SYSTEM_SENDER"
STATUS_IGNORED_SELF = "IGNORED_SELF"
STATUS_IGNORED_INTERNAL = "IGNORED_INTERNAL"
STATUS_IGNORED_TOO_LARGE = "IGNORED_TOO_LARGE"

AUTO_SUBMITTED_HEADER = "Auto-Submitted"
AUTO_SUBMITTED_REPLY_VALUE = "auto-replied"
AUTO_RESPONSE_SUPPRESS_HEADER = "X-Auto-Response-Suppress"
AUTO_RESPONSE_SUPPRESS_VALUE = "OOF, AutoReply"

BULK_PRECEDENCE_VALUES = frozenset({"bulk", "junk", "list"})
SYSTEM_SENDER_LOCAL_PARTS = frozenset(
    {
        "bounce",
        "bounces",
        "do-not-reply",
        "donotreply",
        "mailer-daemon",
        "no-reply",
        "noreply",
        "postmaster",
    }
)

MAX_INBOUND_TEXT_CHARS = 4000
MAX_INBOUND_MESSAGE_BYTES = 25 * 1024 * 1024
MAX_SENDER_ADDRESS_BYTES = 254
MAX_SENDER_NAME_CHARS = 255
MAX_EMAIL_CREDENTIAL_CHARS = 512
MAX_EMAIL_MAILBOX_CHARS = 255
MAX_SUBJECT_CHARS = 998
MAX_MESSAGE_ID_BYTES = 998
MAX_REFERENCE_IDS = 20
MAX_REFERENCES_CHARS = 4096
MAX_ATTACHMENTS = 20
MAX_ATTACHMENT_FILENAME_CHARS = 255
MAX_ATTACHMENT_CONTENT_TYPE_CHARS = 255
SMTP_TIMEOUT_SECONDS = 10.0

_LOCAL_PART_PATTERN = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+")
_DOMAIN_LABEL_PATTERN = re.compile(r"[A-Za-z0-9-]+")


def normalize_email_address(value: str) -> str:
    """Return a trimmed address with a canonical lowercase domain."""

    if not isinstance(value, str):
        raise ValueError("email_address_invalid")
    candidate = value.strip()
    if candidate.count("@") != 1:
        raise ValueError("email_address_invalid")
    local_part, domain = candidate.split("@", 1)
    normalized = f"{local_part}@{domain.removesuffix('.').lower()}"
    if not is_email_address(normalized):
        raise ValueError("email_address_invalid")
    return normalized


def email_address_identity_key(value: str) -> str:
    """Return the case-insensitive identity key for a validated delivery address."""

    return normalize_email_address(value).casefold()


def validate_email_account_text(value: object, *, maximum: int) -> str:
    """Validate an account credential or mailbox without changing its exact bytes."""

    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError("email_account_text_invalid")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ValueError("email_account_text_invalid")
    return value


def is_email_address(value: str) -> bool:
    if not value or len(value.encode("utf-8")) > MAX_SENDER_ADDRESS_BYTES:
        return False
    if any(character.isspace() or ord(character) < 32 or character in "<>" for character in value):
        return False
    if value.count("@") != 1:
        return False
    local_part, domain = value.split("@", 1)
    if (
        not local_part
        or len(local_part.encode("utf-8")) > 64
        or _LOCAL_PART_PATTERN.fullmatch(local_part) is None
        or local_part.startswith(".")
        or local_part.endswith(".")
        or ".." in local_part
    ):
        return False
    return is_email_domain(domain)


def email_address_local_part(value: str) -> str:
    return value.rpartition("@")[0].split("+", 1)[0].lower()


def email_address_domain(value: str) -> str:
    return value.rpartition("@")[2].lower()


def is_email_domain(value: str) -> bool:
    if not value or len(value.encode("utf-8")) > 255:
        return False
    labels = value.removesuffix(".").split(".")
    return bool(
        labels
        and all(
            _DOMAIN_LABEL_PATTERN.fullmatch(label) is not None
            and len(label) <= 63
            and not label.startswith("-")
            and not label.endswith("-")
            for label in labels
        )
    )
