"""Resolve code-owned brand voice preferences for reply generation."""

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.infrastructure.database import models

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
    """Compile finite preferences into fixed English clauses owned by the application."""
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


@dataclass(frozen=True)
class ResolvedPersona:
    text: str
    revision: int | None
    preferences: VoicePreferences = field(default=DEFAULT_VOICE_PREFERENCES)

    @property
    def is_default(self) -> bool:
        return self.revision is None


async def load_persona(session: AsyncSession, tenant_id: str, brand_id: str) -> ResolvedPersona:
    row = (
        await session.execute(
            select(models.ReplyPrompt.voice_preferences, models.ReplyPrompt.revision).where(
                models.ReplyPrompt.tenant_id == tenant_id,
                models.ReplyPrompt.brand_id == brand_id,
            )
        )
    ).first()
    if row is None:
        return ResolvedPersona(text=DEFAULT_PERSONA, revision=None)
    try:
        preferences = VoicePreferences.from_dict(row.voice_preferences)
    except (ValidationError, TypeError, ValueError):
        preferences = DEFAULT_VOICE_PREFERENCES
    return ResolvedPersona(
        text=compile_voice_preferences(preferences),
        revision=row.revision,
        preferences=preferences,
    )


def prompt_version_label(base: str, persona: ResolvedPersona) -> str:
    """Include the effective preference revision in reply_decisions.prompt_version."""
    return base if persona.is_default else f"{base}#r{persona.revision}"


def validate_persona(text: str) -> str:
    """Backward-compatible invariant helper for code-compiled persona text."""
    cleaned = text.strip()
    if not cleaned:
        raise ValueError("persona_required")
    if len(cleaned) > PERSONA_MAX_CHARS:
        raise ValueError("persona_too_long")
    return cleaned


def parse_voice_preferences(value: dict[str, Any]) -> VoicePreferences:
    """Validate a complete policy mapping without accepting missing or extra fields."""
    if set(value) != VOICE_PREFERENCE_FIELDS:
        raise ValueError("voice_preferences_invalid")
    try:
        return VoicePreferences.from_dict(value)
    except (ValidationError, TypeError, ValueError) as exc:
        raise ValueError("voice_preferences_invalid") from exc
