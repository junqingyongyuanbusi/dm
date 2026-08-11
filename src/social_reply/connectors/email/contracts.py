"""Shared protocol constants for the email connector."""

PLATFORM = "email"

STATUS_PENDING = "PENDING"
STATUS_SCHEMA_UNSUPPORTED = "EMAIL_SCHEMA_UNSUPPORTED"
STATUS_IGNORED_AUTO_SUBMITTED = "IGNORED_AUTO_SUBMITTED"
STATUS_IGNORED_BULK_LIST = "IGNORED_BULK_LIST"
STATUS_IGNORED_SYSTEM_SENDER = "IGNORED_SYSTEM_SENDER"
STATUS_IGNORED_SELF = "IGNORED_SELF"
STATUS_IGNORED_INTERNAL = "IGNORED_INTERNAL"

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
SMTP_TIMEOUT_SECONDS = 10.0
