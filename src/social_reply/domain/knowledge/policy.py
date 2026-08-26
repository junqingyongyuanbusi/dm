"""Stable identity helpers for knowledge content and deterministic reply policy."""

import hashlib
import json
from collections.abc import Iterable

type KnowledgeAnswerIdentity = tuple[str, bool, tuple[str, ...]]


def normalize_protected_values(values: Iterable[str]) -> tuple[str, ...]:
    """Normalize storage order while preserving distinct case-sensitive values."""
    return tuple(dict.fromkeys(value.strip() for value in values if value.strip()))


def canonical_protected_values(values: Iterable[str]) -> tuple[str, ...]:
    """Return a stable set-like identity for deterministic protected values."""
    return tuple(
        sorted(
            normalize_protected_values(values),
            key=lambda value: (value.casefold(), value),
        )
    )


def knowledge_revision_hash(content: str, protected_values: Iterable[str]) -> str:
    """Hash content plus policy metadata without invalidating legacy policy-free rows."""
    normalized = canonical_protected_values(protected_values)
    if not normalized:
        payload = content
    else:
        policy = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
        payload = f"knowledge-policy-v1\0{content}\0{policy}"
    return hashlib.sha256(payload.encode()).hexdigest()


def canonical_answer_identity(
    reply: str,
    is_official_contact: bool,
    protected_values: Iterable[str] = (),
) -> KnowledgeAnswerIdentity:
    """Stable identity for one approved answer and its deterministic policy."""
    return (
        " ".join(reply.casefold().split()),
        is_official_contact,
        canonical_protected_values(protected_values),
    )
