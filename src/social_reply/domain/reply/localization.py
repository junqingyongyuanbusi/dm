from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass


def canonicalize_locale(value: str) -> str:
    parts = [part for part in value.strip().replace("_", "-").split("-") if part]
    if not parts:
        raise ValueError("locale is required")
    primary = parts[0].casefold()
    if not primary.isalpha() or len(primary) not in {2, 3}:
        raise ValueError("locale primary language is invalid")
    normalized = [primary]
    for part in parts[1:]:
        if len(part) == 4 and part.isalpha():
            normalized.append(part.title())
        elif len(part) == 2 and part.isalpha():
            normalized.append(part.upper())
        elif len(part) == 3 and part.isdigit():
            normalized.append(part)
        else:
            raise ValueError("locale subtag is invalid")
    return "-".join(normalized)

@dataclass(frozen=True)
class ApprovedLocalizationArtifact:
    """Immutable reviewed text bound to one English knowledge revision and locale."""

    id: uuid.UUID
    release_id: str
    locale: str
    text: str
    text_hash: str
    source_content_hash: str
    protected_values: tuple[str, ...] = ()
    official_contact_authorized: bool = False
    auto_reply_allowed: bool = False

    def has_valid_text_hash(self) -> bool:
        return hashlib.sha256(self.text.encode()).hexdigest() == self.text_hash
