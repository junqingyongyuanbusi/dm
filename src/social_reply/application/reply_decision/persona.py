"""Resolve code-owned brand voice preferences for reply generation."""

import logging
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.domain.reply.voice import (
    CANONICAL_VOICE_PREFERENCES,
    CANONICAL_VOICE_PREFERENCES_JSON,
    DEFAULT_PERSONA,
    DEFAULT_VOICE_PREFERENCES,
    PERSONA_MAX_CHARS,
    VOICE_PREFERENCE_FIELDS,
    VoiceEmoji,
    VoiceEmpathy,
    VoiceLength,
    VoicePreferences,
    VoiceTone,
    compile_voice_preferences,
)
from social_reply.infrastructure.database import models

logger = logging.getLogger(__name__)

__all__ = [
    "CANONICAL_VOICE_PREFERENCES",
    "CANONICAL_VOICE_PREFERENCES_JSON",
    "DEFAULT_PERSONA",
    "DEFAULT_VOICE_PREFERENCES",
    "PERSONA_MAX_CHARS",
    "ResolvedPersona",
    "VoiceEmoji",
    "VoiceEmpathy",
    "VoiceLength",
    "VoicePreferences",
    "VoiceTone",
    "compile_voice_preferences",
    "load_persona",
    "parse_voice_preferences",
    "prompt_version_label",
    "validate_persona",
]


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
    except (ValidationError, TypeError, ValueError) as exc:
        logger.warning(
            "Invalid voice preferences; using defaults: tenant=%s brand=%s revision=%s "
            "exception_type=%s",
            tenant_id,
            brand_id,
            row.revision,
            type(exc).__name__,
        )
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
