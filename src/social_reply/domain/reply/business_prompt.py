import hashlib
import re
import unicodedata
from dataclasses import dataclass

from social_reply.domain.reply.guard import has_contact_like
from social_reply.domain.reply.voice import DEFAULT_PERSONA

BUSINESS_PROMPT_MAX_CHARS = 4000
BUSINESS_PROMPT_CHANGE_NOTE_MAX_CHARS = 240

_PROBABLE_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"-----BEGIN PGP PRIVATE KEY BLOCK-----"),
    re.compile(
        r"(?i)\b[a-z][a-z0-9+.-]{1,31}://[^\s/?#@]+:[^\s/?#@]+@"
    ),
    re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"(?i)\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b"),
    re.compile(r"(?i)\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"(?i)\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bAIza[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"(?i)\bxox[baprs]-[A-Za-z0-9-]{16,}\b"),
    re.compile(r"\bSK[0-9a-fA-F]{32}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)\bauthorization\s*:\s*bearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(
        r"(?i)\b(?:api[_ -]?key|access[_ -]?token|client[_ -]?secret|credential|"
        r"password|secret|token)"
        r"\s*[:=]\s*(?:['\"]?[^\n\r]{8,})"
    ),
    re.compile(
        r"(?i)\b(?:api[_ -]?key|access[_ -]?token|client[_ -]?secret|credential|"
        r"password|secret|token)\b"
        r"\s*(?:(?:is|as)\s+|[<\"'`])(?:[<\"'`]?)"
        r"[A-Za-z0-9][A-Za-z0-9._~+/=-]{7,}"
    ),
)
_OBFUSCATED_AT = re.compile(r"(?i)\s*[\[(]\s*at\s*[\])]\s*")
_OBFUSCATED_DOT = re.compile(r"(?i)\s*[\[(]\s*dot\s*[\])]\s*")


class BusinessPromptValidationError(ValueError):
    """Raised when editable business instructions violate their storage boundary."""


def _normalize_text(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


def _contains_forbidden_control_character(value: str) -> bool:
    return any(
        unicodedata.category(character) == "Cc" and character not in {"\n", "\t"}
        for character in value
    )


def _normalize_for_sensitive_detection(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    normalized = "".join(
        character
        for character in normalized
        if unicodedata.category(character) != "Cf"
    )
    normalized = _OBFUSCATED_AT.sub("@", normalized)
    return _OBFUSCATED_DOT.sub(".", normalized)


def _contains_probable_secret(value: str) -> bool:
    candidate = _normalize_for_sensitive_detection(value)
    return any(pattern.search(candidate) for pattern in _PROBABLE_SECRET_PATTERNS)


def _contains_contact_like(value: str) -> bool:
    return has_contact_like(_normalize_for_sensitive_detection(value))


def business_prompt_content_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True, init=False)
class BusinessPromptInstructions:
    """Validated, immutable instructions accepted by the primary reply model only."""

    text: str
    content_hash: str

    def __init__(self, value: str) -> None:
        if not isinstance(value, str):
            raise BusinessPromptValidationError("business_prompt_must_be_text")
        normalized = _normalize_text(value)
        if not normalized:
            raise BusinessPromptValidationError("business_prompt_required")
        if len(normalized) > BUSINESS_PROMPT_MAX_CHARS:
            raise BusinessPromptValidationError("business_prompt_too_long")
        if _contains_forbidden_control_character(normalized):
            raise BusinessPromptValidationError("business_prompt_control_character_forbidden")
        if _contains_probable_secret(normalized):
            raise BusinessPromptValidationError("business_prompt_probable_secret_forbidden")
        if _contains_contact_like(normalized):
            raise BusinessPromptValidationError("business_prompt_contact_value_forbidden")
        object.__setattr__(self, "text", normalized)
        object.__setattr__(self, "content_hash", business_prompt_content_hash(normalized))


def normalize_business_prompt_change_note(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = _normalize_text(value)
    if not normalized:
        return None
    if len(normalized) > BUSINESS_PROMPT_CHANGE_NOTE_MAX_CHARS:
        raise BusinessPromptValidationError("business_prompt_change_note_too_long")
    if _contains_forbidden_control_character(normalized):
        raise BusinessPromptValidationError(
            "business_prompt_change_note_control_character_forbidden"
        )
    if _contains_probable_secret(normalized):
        raise BusinessPromptValidationError(
            "business_prompt_change_note_probable_secret_forbidden"
        )
    if _contains_contact_like(normalized):
        raise BusinessPromptValidationError(
            "business_prompt_change_note_contact_value_forbidden"
        )
    return normalized


DEFAULT_BUSINESS_PROMPT = BusinessPromptInstructions(DEFAULT_PERSONA)
