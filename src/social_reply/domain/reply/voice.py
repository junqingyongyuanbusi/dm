"""Finite, code-owned brand voice policy for reply generation."""

import json
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

PERSONA_MAX_CHARS = 4000


class VoiceTone(StrEnum):
    PROFESSIONAL = "professional"
    WARM = "warm"
    FORMAL = "formal"


class VoiceLength(StrEnum):
    CONCISE = "concise"
    BALANCED = "balanced"


class VoiceEmpathy(StrEnum):
    STANDARD = "standard"
    HIGH = "high"


class VoiceEmoji(StrEnum):
    NEVER = "never"
    SPARINGLY = "sparingly"


class VoicePreferences(BaseModel):
    """Finite brand-voice policy. Arbitrary instructions are deliberately unsupported."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tone: VoiceTone
    length: VoiceLength
    empathy: VoiceEmpathy
    emoji: VoiceEmoji

    def to_dict(self) -> dict[str, str]:
        return self.model_dump(mode="json")

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, value: object) -> "VoicePreferences":
        return cls.model_validate(value)

    @classmethod
    def from_json(cls, value: str | bytes) -> "VoicePreferences":
        return cls.model_validate_json(value)


DEFAULT_VOICE_PREFERENCES = VoicePreferences(
    tone=VoiceTone.PROFESSIONAL,
    length=VoiceLength.CONCISE,
    empathy=VoiceEmpathy.STANDARD,
    emoji=VoiceEmoji.NEVER,
)
CANONICAL_VOICE_PREFERENCES = DEFAULT_VOICE_PREFERENCES.to_dict()
CANONICAL_VOICE_PREFERENCES_JSON = DEFAULT_VOICE_PREFERENCES.to_json()
VOICE_PREFERENCE_FIELDS = frozenset(CANONICAL_VOICE_PREFERENCES)

_TONE_CLAUSES = {
    VoiceTone.PROFESSIONAL: "Use a professional, calm, and plain-spoken tone.",
    VoiceTone.WARM: "Use a warm, approachable, and reassuring tone.",
    VoiceTone.FORMAL: "Use a formal, respectful, and precise tone.",
}
_LENGTH_CLAUSES = {
    VoiceLength.CONCISE: "Keep replies concise and focused on the customer's immediate question.",
    VoiceLength.BALANCED: "Use a balanced amount of detail while staying focused on the question.",
}
_EMPATHY_CLAUSES = {
    VoiceEmpathy.STANDARD: (
        "Acknowledge the customer's concern when relevant without overstating emotion."
    ),
    VoiceEmpathy.HIGH: (
        "Show clear empathy for the customer's concern while remaining factual and composed."
    ),
}
_EMOJI_CLAUSES = {
    VoiceEmoji.NEVER: "Do not use emoji.",
    VoiceEmoji.SPARINGLY: (
        "Use at most one simple emoji, and only when it naturally fits the locale."
    ),
}


def compile_voice_preferences(preferences: VoicePreferences) -> str:
    """Compile finite preferences into fixed English clauses owned by the domain."""
    if not isinstance(preferences, VoicePreferences):
        raise TypeError("voice_preferences_must_be_typed")
    clauses = (
        _TONE_CLAUSES[preferences.tone],
        _LENGTH_CLAUSES[preferences.length],
        _EMPATHY_CLAUSES[preferences.empathy],
        _EMOJI_CLAUSES[preferences.emoji],
    )
    compiled = "Brand voice preferences:\n" + "\n".join(f"- {clause}" for clause in clauses)
    if len(compiled) > PERSONA_MAX_CHARS:
        raise AssertionError("compiled voice preferences exceed PERSONA_MAX_CHARS")
    return compiled


DEFAULT_PERSONA = compile_voice_preferences(DEFAULT_VOICE_PREFERENCES)
