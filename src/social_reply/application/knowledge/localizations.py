from __future__ import annotations

import hashlib
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.domain.reply.guard import (
    contact_values,
    factual_tokens,
    protected_entities,
)
from social_reply.domain.reply.language import reply_language_matches
from social_reply.domain.reply.localization import (
    ApprovedLocalizationArtifact,
    canonicalize_locale,
)
from social_reply.infrastructure.database import models


class LocalizationValidationError(ValueError):
    pass


@dataclass(frozen=True)
class LocalizationDraftInput:
    tenant_id: str
    document_id: uuid.UUID
    release_id: str
    locale: str
    text: str
    protected_values: tuple[str, ...] = ()
    source_file: str | None = None
    import_batch_id: uuid.UUID | None = None


def normalize_locale(value: str) -> str:
    try:
        return canonicalize_locale(value)
    except ValueError as exc:
        raise LocalizationValidationError(str(exc)) from exc


def resolve_locale(detected_language: str, available_locales: set[str]) -> str | None:
    if detected_language == "und":
        return None
    detected = normalize_locale(detected_language)
    normalized_available = {normalize_locale(locale) for locale in available_locales}
    if detected in normalized_available:
        return detected
    primary = detected.split("-", 1)[0]
    if primary in normalized_available:
        return primary
    return None


async def _source_document_and_hash(
    session: AsyncSession,
    *,
    tenant_id: str,
    document_id: uuid.UUID,
) -> tuple[models.KnowledgeDocument, str]:
    row = (
        await session.execute(
            select(models.KnowledgeDocument, models.KnowledgeChunk.content_hash)
            .join(
                models.KnowledgeChunk,
                models.KnowledgeChunk.document_id == models.KnowledgeDocument.id,
            )
            .where(
                models.KnowledgeDocument.tenant_id == tenant_id,
                models.KnowledgeDocument.id == document_id,
                models.KnowledgeChunk.tenant_id == tenant_id,
            )
            .order_by(models.KnowledgeChunk.created_at.desc())
            .limit(1)
        )
    ).one_or_none()
    if row is None:
        raise LocalizationValidationError("knowledge source document was not found")
    document, content_hash = row
    if not (
        document.status == "published"
        and document.source_language == "en"
        and document.language_verified
    ):
        raise LocalizationValidationError("knowledge source must be published verified English")
    return document, content_hash


def _validate_protected_values(
    *,
    source_text: str,
    localized_text: str,
    locale: str,
    protected_values: tuple[str, ...],
) -> tuple[str, ...]:
    source_contacts = contact_values(source_text)
    if Counter(contact_values(localized_text)) != Counter(source_contacts):
        raise LocalizationValidationError("localized contact values differ from English source")
    automatic = (*source_contacts, *protected_entities(source_text))
    normalized = tuple(
        dict.fromkeys(value.strip() for value in (*automatic, *protected_values) if value.strip())
    )
    for value in normalized:
        if source_text.count(value) == 0:
            raise LocalizationValidationError(f"protected value is absent from source: {value}")
        if localized_text.count(value) != source_text.count(value):
            raise LocalizationValidationError(f"protected value count changed: {value}")
    if Counter(factual_tokens(localized_text, language=locale)) != Counter(
        factual_tokens(source_text, language="en")
    ):
        raise LocalizationValidationError("localized facts differ from English source")
    return normalized


async def create_localization_draft(
    session: AsyncSession,
    draft: LocalizationDraftInput,
) -> models.KnowledgeLocalization:
    release_id = draft.release_id.strip()
    if not release_id or len(release_id) > 64:
        raise LocalizationValidationError("localization release id is invalid")
    locale = normalize_locale(draft.locale)
    text = draft.text.strip()
    if not text:
        raise LocalizationValidationError("localized text is required")
    document, source_content_hash = await _source_document_and_hash(
        session,
        tenant_id=draft.tenant_id,
        document_id=draft.document_id,
    )
    protected_values = _validate_protected_values(
        source_text=document.reply,
        localized_text=text,
        locale=locale,
        protected_values=draft.protected_values,
    )

    artifact = models.KnowledgeLocalization(
        tenant_id=draft.tenant_id,
        document_id=draft.document_id,
        release_id=release_id,
        locale=locale,
        localized_text=text,
        text_hash=hashlib.sha256(text.encode()).hexdigest(),
        source_content_hash=source_content_hash,
        protected_values=list(protected_values),
        official_contact_authorized=False,
        auto_reply_allowed=False,
        status="draft",
        source_file=draft.source_file,
        import_batch_id=draft.import_batch_id,
    )
    session.add(artifact)
    await session.flush()
    return artifact


async def publish_localization(
    session: AsyncSession,
    *,
    tenant_id: str,
    artifact_id: uuid.UUID,
    reviewer: str,
    approve_auto_reply: bool,
    approve_official_contact: bool,
) -> models.KnowledgeLocalization:
    reviewer = reviewer.strip()
    if not reviewer:
        raise LocalizationValidationError("reviewer is required")
    artifact = (
        await session.execute(
            select(models.KnowledgeLocalization).where(
                models.KnowledgeLocalization.tenant_id == tenant_id,
                models.KnowledgeLocalization.id == artifact_id,
            )
        )
    ).scalar_one_or_none()
    if artifact is None:
        raise LocalizationValidationError("localization artifact was not found")
    if artifact.status != "draft":
        raise LocalizationValidationError("only draft localization can be published")
    await session.execute(
        select(models.KnowledgeDocument.id)
        .where(
            models.KnowledgeDocument.tenant_id == tenant_id,
            models.KnowledgeDocument.id == artifact.document_id,
        )
        .with_for_update()
    )
    artifact = (
        await session.execute(
            select(models.KnowledgeLocalization)
            .where(
                models.KnowledgeLocalization.tenant_id == tenant_id,
                models.KnowledgeLocalization.id == artifact_id,
            )
            .with_for_update()
        )
    ).scalar_one()
    if artifact.status != "draft":
        raise LocalizationValidationError("only draft localization can be published")
    lock_key = (
        f"knowledge-localization-publish:{tenant_id}:{artifact.document_id}:"
        f"{artifact.release_id}:{artifact.locale}"
    )
    await session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(lock_key, 0))))
    document, current_source_hash = await _source_document_and_hash(
        session,
        tenant_id=tenant_id,
        document_id=artifact.document_id,
    )
    if current_source_hash != artifact.source_content_hash:
        raise LocalizationValidationError("localization source revision is stale")
    language_ok, _observed = reply_language_matches(artifact.locale, artifact.localized_text)
    if not language_ok:
        raise LocalizationValidationError("localized text does not match locale")
    protected_values = _validate_protected_values(
        source_text=document.reply,
        localized_text=artifact.localized_text,
        locale=artifact.locale,
        protected_values=tuple(artifact.protected_values or ()),
    )
    if approve_official_contact and not document.is_official_contact:
        raise LocalizationValidationError("source knowledge is not classified as official contact")
    if document.is_official_contact and approve_auto_reply:
        if not approve_official_contact or not protected_values:
            raise LocalizationValidationError("official-contact auto reply is not fully authorized")
    existing = await session.scalar(
        select(models.KnowledgeLocalization.id).where(
            models.KnowledgeLocalization.tenant_id == tenant_id,
            models.KnowledgeLocalization.document_id == artifact.document_id,
            models.KnowledgeLocalization.release_id == artifact.release_id,
            models.KnowledgeLocalization.locale == artifact.locale,
            models.KnowledgeLocalization.status == "published",
            models.KnowledgeLocalization.id != artifact.id,
        )
    )
    if existing is not None:
        raise LocalizationValidationError("a published localization already exists for locale")
    artifact.auto_reply_allowed = approve_auto_reply
    artifact.official_contact_authorized = approve_official_contact
    artifact.status = "published"
    artifact.reviewed_by = reviewer
    artifact.reviewed_at = datetime.now(UTC)
    return artifact



async def _has_sending_localization_outbox(
    session: AsyncSession, artifact_ids: tuple[uuid.UUID, ...]
) -> bool:
    if not artifact_ids:
        return False
    count = await session.scalar(
        select(func.count())
        .select_from(models.OutboxMessage)
        .join(
            models.ReplyDecision,
            models.ReplyDecision.outbox_id == models.OutboxMessage.id,
        )
        .where(
            models.ReplyDecision.knowledge_localization_id.in_(artifact_ids),
            models.OutboxMessage.origin_kind == "DECISION",
            models.OutboxMessage.actor_kind == "BOT",
            models.OutboxMessage.status == "SENDING",
        )
    )
    return bool(count)

async def revoke_localization(
    session: AsyncSession,
    *,
    tenant_id: str,
    artifact_id: uuid.UUID,
    actor: str,
    reason: str,
) -> models.KnowledgeLocalization:
    actor = actor.strip()
    if not actor:
        raise LocalizationValidationError("revoke actor is required")
    artifact = (
        await session.execute(
            select(models.KnowledgeLocalization)
            .where(
                models.KnowledgeLocalization.tenant_id == tenant_id,
                models.KnowledgeLocalization.id == artifact_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if artifact is None:
        raise LocalizationValidationError("localization artifact was not found")
    if artifact.status != "published":
        raise LocalizationValidationError("only published localization can be revoked")
    if await _has_sending_localization_outbox(session, (artifact.id,)):
        raise LocalizationValidationError("localization has a sending outbox")
    artifact.auto_reply_allowed = False
    artifact.status = "revoked"
    artifact.revoked_by = actor
    artifact.revoked_at = datetime.now(UTC)
    artifact.revoke_reason = reason.strip() or "unspecified"
    return artifact


async def revoke_document_localizations(
    session: AsyncSession,
    *,
    tenant_id: str,
    document_id: uuid.UUID,
    actor: str,
    reason: str,
) -> int:
    actor = actor.strip()
    if not actor:
        raise LocalizationValidationError("revoke actor is required")
    artifacts = (
        (
            await session.execute(
                select(models.KnowledgeLocalization)
                .where(
                    models.KnowledgeLocalization.tenant_id == tenant_id,
                    models.KnowledgeLocalization.document_id == document_id,
                    models.KnowledgeLocalization.status == "published",
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    if await _has_sending_localization_outbox(
        session, tuple(artifact.id for artifact in artifacts)
    ):
        raise LocalizationValidationError("localization has a sending outbox")
    now = datetime.now(UTC)
    for artifact in artifacts:
        artifact.auto_reply_allowed = False
        artifact.status = "revoked"
        artifact.revoked_by = actor
        artifact.revoked_at = now
        artifact.revoke_reason = reason
    return len(artifacts)


async def load_approved_localization(
    session: AsyncSession,
    *,
    tenant_id: str,
    document_id: uuid.UUID,
    source_content_hash: str,
    detected_language: str,
    pinned_release_id: str,
    live_locales: set[str],
) -> ApprovedLocalizationArtifact | None:
    artifacts = (
        (
            await session.execute(
                select(models.KnowledgeLocalization)
                .join(
                    models.KnowledgeDocument,
                    (models.KnowledgeDocument.tenant_id == models.KnowledgeLocalization.tenant_id)
                    & (models.KnowledgeDocument.id == models.KnowledgeLocalization.document_id),
                )
                .join(
                    models.KnowledgeChunk,
                    (models.KnowledgeChunk.tenant_id == models.KnowledgeDocument.tenant_id)
                    & (models.KnowledgeChunk.document_id == models.KnowledgeDocument.id),
                )
                .where(
                    models.KnowledgeLocalization.tenant_id == tenant_id,
                    models.KnowledgeLocalization.document_id == document_id,
                    models.KnowledgeLocalization.release_id == pinned_release_id,
                    models.KnowledgeLocalization.source_content_hash == source_content_hash,
                    models.KnowledgeLocalization.status == "published",
                    models.KnowledgeLocalization.auto_reply_allowed.is_(True),
                    models.KnowledgeDocument.status == "published",
                    models.KnowledgeDocument.source_language == "en",
                    models.KnowledgeDocument.language_verified.is_(True),
                    models.KnowledgeChunk.content_hash == source_content_hash,
                )
            )
        )
        .scalars()
        .all()
    )
    live = {normalize_locale(locale) for locale in live_locales}
    valid_artifacts = [
        artifact
        for artifact in artifacts
        if artifact.reviewed_by
        and artifact.reviewed_at is not None
        and hashlib.sha256(artifact.localized_text.encode()).hexdigest() == artifact.text_hash
        and artifact.source_content_hash == source_content_hash
        and isinstance(artifact.protected_values, list)
        and all(
            isinstance(value, str) and bool(value.strip()) for value in artifact.protected_values
        )
    ]
    eligible = {artifact.locale for artifact in valid_artifacts if artifact.locale in live}
    resolved = resolve_locale(detected_language, eligible)
    if resolved is None:
        return None
    artifact = next(artifact for artifact in valid_artifacts if artifact.locale == resolved)
    return ApprovedLocalizationArtifact(
        id=artifact.id,
        release_id=artifact.release_id,
        locale=artifact.locale,
        text=artifact.localized_text,
        text_hash=artifact.text_hash,
        source_content_hash=artifact.source_content_hash,
        protected_values=tuple(artifact.protected_values or ()),
        official_contact_authorized=artifact.official_contact_authorized,
        auto_reply_allowed=artifact.auto_reply_allowed,
    )
