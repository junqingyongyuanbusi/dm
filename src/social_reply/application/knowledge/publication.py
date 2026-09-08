from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.application.account_management.auth import Principal
from social_reply.application.knowledge.authorization import (
    AuthorizedKnowledgeWrite,
    authorize_knowledge_write,
)
from social_reply.application.knowledge.commands import (
    KnowledgeConflictError,
    KnowledgeNotFoundError,
    decision_references_knowledge,
    validate_knowledge_tenant_scope,
)
from social_reply.application.knowledge.drafts import (
    audit_value_hash,
    knowledge_document_safety_lock_key,
)
from social_reply.application.knowledge.localizations import (
    LocalizationValidationError,
    revoke_document_localizations,
)
from social_reply.application.knowledge.retrieval import (
    embedding_columns,
    normalize_question,
)
from social_reply.domain.knowledge.policy import canonical_answer_identity
from social_reply.domain.reply.guard import has_contact_like
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.advisory_locks import acquire_xact_lock
from social_reply.shared.config import get_settings


@dataclass(frozen=True)
class PublishKnowledgeCommand:
    required_tenant_id: str
    principal: Principal | None
    document_id: uuid.UUID


@dataclass(frozen=True)
class UnpublishKnowledgeCommand:
    required_tenant_id: str
    principal: Principal | None
    document_id: uuid.UUID


@dataclass(frozen=True)
class BulkPublishKnowledgeCommand:
    required_tenant_id: str
    principal: Principal | None
    document_ids: tuple[uuid.UUID, ...] = ()


@dataclass(frozen=True)
class BulkPublishKnowledgeResult:
    published_count: int
    skipped_count: int
    skip_reasons: dict[str, int]


async def _locked_document(
    session: AsyncSession,
    *,
    tenant_id: str,
    document_id: uuid.UUID,
) -> models.KnowledgeDocument:
    document = (
        await session.execute(
            select(models.KnowledgeDocument)
            .where(
                models.KnowledgeDocument.tenant_id == tenant_id,
                models.KnowledgeDocument.id == document_id,
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if document is None:
        raise KnowledgeNotFoundError("knowledge_document_not_found")
    return document


async def _document(
    session: AsyncSession,
    *,
    tenant_id: str,
    document_id: uuid.UUID,
) -> models.KnowledgeDocument:
    document = await session.scalar(
        select(models.KnowledgeDocument).where(
            models.KnowledgeDocument.tenant_id == tenant_id,
            models.KnowledgeDocument.id == document_id,
        ).execution_options(populate_existing=True)
    )
    if document is None:
        raise KnowledgeNotFoundError("knowledge_document_not_found")
    return document


async def _acquire_publication_lock(
    session: AsyncSession,
    document: models.KnowledgeDocument,
) -> None:
    normalized_question = normalize_question(document.question)
    lock_key = f"knowledge-publish:{document.tenant_id}:{document.brand_id}:{normalized_question}"
    await session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(lock_key, 0))))


async def _lock_publication_document(
    session: AsyncSession,
    *,
    tenant_id: str,
    document_id: uuid.UUID,
    safety_lock_held: bool = False,
) -> models.KnowledgeDocument:
    if not safety_lock_held:
        await acquire_xact_lock(
            session,
            knowledge_document_safety_lock_key(tenant_id, document_id),
        )
    document = await _document(
        session,
        tenant_id=tenant_id,
        document_id=document_id,
    )
    await _acquire_publication_lock(session, document)
    return await _locked_document(
        session,
        tenant_id=tenant_id,
        document_id=document_id,
    )

async def _require_current_embedding(
    session: AsyncSession,
    document: models.KnowledgeDocument,
) -> None:
    settings = get_settings()
    vector_column, version_column = embedding_columns(settings.openai_embedding_dimensions)
    current_embedding = await session.scalar(
        select(models.KnowledgeChunk.id)
        .where(
            models.KnowledgeChunk.tenant_id == document.tenant_id,
            models.KnowledgeChunk.document_id == document.id,
            version_column == settings.openai_embedding_model,
            vector_column.is_not(None),
        )
        .limit(1)
    )
    if current_embedding is None:
        raise KnowledgeConflictError("knowledge_embedding_not_ready")


async def _require_no_published_conflict(
    session: AsyncSession,
    document: models.KnowledgeDocument,
) -> None:
    normalized_question = normalize_question(document.question)
    platform_overlap = (
        True
        if document.platform is None
        else or_(
            models.KnowledgeDocument.platform.is_(None),
            models.KnowledgeDocument.platform == document.platform,
        )
    )
    candidates = list(
        (
            await session.execute(
                select(models.KnowledgeDocument).where(
                    models.KnowledgeDocument.id != document.id,
                    models.KnowledgeDocument.tenant_id == document.tenant_id,
                    models.KnowledgeDocument.brand_id == document.brand_id,
                    models.KnowledgeDocument.status == "published",
                    platform_overlap,
                )
            )
        ).scalars()
    )
    for candidate in candidates:
        questions_overlap = normalize_question(candidate.question) == normalized_question
        answers_conflict = canonical_answer_identity(
            candidate.reply,
            candidate.is_official_contact,
            candidate.protected_values or (),
        ) != canonical_answer_identity(
            document.reply,
            document.is_official_contact,
            document.protected_values or (),
        )
        if questions_overlap and answers_conflict:
            raise KnowledgeConflictError("conflicting_published_knowledge")


async def _has_direct_knowledge_outbox_in_flight(
    session: AsyncSession,
    document: models.KnowledgeDocument,
) -> bool:
    chunk_rows = (
        await session.execute(
            select(models.KnowledgeChunk.id, models.KnowledgeChunk.content_hash).where(
                models.KnowledgeChunk.tenant_id == document.tenant_id,
                models.KnowledgeChunk.document_id == document.id,
            )
        )
    ).all()
    chunk_ids = {row.id for row in chunk_rows}
    content_hashes = {row.content_hash for row in chunk_rows}
    decisions = list(
        (
            await session.execute(
                select(models.ReplyDecision)
                .select_from(models.OutboxMessage)
                .join(
                    models.ReplyDecision,
                    or_(
                        models.ReplyDecision.outbox_id == models.OutboxMessage.id,
                        models.ReplyDecision.review_outbox_id == models.OutboxMessage.id,
                    ),
                )
                .where(
                    models.ReplyDecision.tenant_id == document.tenant_id,
                    models.OutboxMessage.status.not_in(("SENT", "CANCELLED")),
                )
            )
        ).scalars()
    )
    return any(
        decision_references_knowledge(
            decision,
            document_id=document.id,
            chunk_ids=chunk_ids,
            content_hashes=content_hashes,
        )
        for decision in decisions
    )


async def _require_publishable(
    session: AsyncSession,
    document: models.KnowledgeDocument,
) -> None:
    if not (
        document.source_language == "en"
        and document.language_verified
        and document.language_detection_status in {"english", "unknown"}
    ):
        raise KnowledgeConflictError("confirm_english_before_publish")
    if (
        document.is_official_contact
        or has_contact_like(document.question or "")
        or has_contact_like(document.reply or "")
    ):
        raise KnowledgeConflictError("official_contact_requires_review")
    await _require_current_embedding(session, document)
    await _require_no_published_conflict(session, document)


def _publication_audit(
    document: models.KnowledgeDocument,
    *,
    actor: str,
    action: str,
    previous_status: str,
    target_status: str,
    bulk: bool,
) -> models.AuditLog:
    return models.AuditLog(
        tenant_id=document.tenant_id,
        category="admin_action",
        actor=actor,
        action=action,
        subject_type="knowledge_document",
        subject_id=str(document.id),
        detail={
            "from": previous_status,
            "to": target_status,
            "brand_hash": audit_value_hash(document.brand_id),
            "platform_hash": audit_value_hash(document.platform),
            "is_official_contact": document.is_official_contact,
            "bulk": bulk,
        },
    )


async def _publish_authorized(
    session: AsyncSession,
    *,
    tenant_id: str,
    document_id: uuid.UUID,
    authorized: AuthorizedKnowledgeWrite,
    bulk: bool,
    safety_lock_held: bool = False,
) -> tuple[models.KnowledgeDocument, bool]:
    document = await _lock_publication_document(
        session,
        tenant_id=tenant_id,
        document_id=document_id,
        safety_lock_held=safety_lock_held,
    )
    if document.status == "published":
        return document, False
    await _require_publishable(session, document)
    previous_status = document.status
    document.status = "published"
    session.add(
        _publication_audit(
            document,
            actor=authorized.actor,
            action="PUBLISH_KNOWLEDGE",
            previous_status=previous_status,
            target_status="published",
            bulk=bulk,
        )
    )
    return document, True


async def execute_publish_knowledge(
    session: AsyncSession,
    command: PublishKnowledgeCommand,
    *,
    bulk: bool = False,
) -> models.KnowledgeDocument:
    tenant_id = validate_knowledge_tenant_scope(command.required_tenant_id)
    authorized = await authorize_knowledge_write(
        session,
        principal=command.principal,
        tenant_id=tenant_id,
    )
    document, _changed = await _publish_authorized(
        session,
        tenant_id=tenant_id,
        document_id=command.document_id,
        authorized=authorized,
        bulk=bulk,
    )
    return document


async def _unpublish_authorized(
    session: AsyncSession,
    *,
    tenant_id: str,
    document_id: uuid.UUID,
    authorized: AuthorizedKnowledgeWrite,
) -> models.KnowledgeDocument:
    document = await _lock_publication_document(
        session,
        tenant_id=tenant_id,
        document_id=document_id,
    )
    if document.status == "draft":
        return document
    if await _has_direct_knowledge_outbox_in_flight(session, document):
        raise KnowledgeConflictError("knowledge_send_in_progress")
    try:
        await revoke_document_localizations(
            session,
            tenant_id=tenant_id,
            document_id=document.id,
            actor=authorized.actor,
            reason="source knowledge unpublished",
        )
    except LocalizationValidationError as exc:
        if str(exc) == "localization has a sending outbox":
            raise KnowledgeConflictError("localization_send_in_progress") from exc
        raise KnowledgeConflictError("knowledge_localization_revoke_failed") from exc
    previous_status = document.status
    document.status = "draft"
    session.add(
        _publication_audit(
            document,
            actor=authorized.actor,
            action="UNPUBLISH_KNOWLEDGE",
            previous_status=previous_status,
            target_status="draft",
            bulk=False,
        )
    )
    return document


async def execute_unpublish_knowledge(
    session: AsyncSession,
    command: UnpublishKnowledgeCommand,
) -> models.KnowledgeDocument:
    tenant_id = validate_knowledge_tenant_scope(command.required_tenant_id)
    authorized = await authorize_knowledge_write(
        session,
        principal=command.principal,
        tenant_id=tenant_id,
    )
    return await _unpublish_authorized(
        session,
        tenant_id=tenant_id,
        document_id=command.document_id,
        authorized=authorized,
    )


async def execute_bulk_publish_knowledge(
    session: AsyncSession,
    command: BulkPublishKnowledgeCommand,
) -> BulkPublishKnowledgeResult:
    tenant_id = validate_knowledge_tenant_scope(command.required_tenant_id)
    authorized = await authorize_knowledge_write(
        session,
        principal=command.principal,
        tenant_id=tenant_id,
    )
    statement = select(models.KnowledgeDocument.id).where(
        models.KnowledgeDocument.tenant_id == tenant_id,
        models.KnowledgeDocument.status == "draft",
    )
    if command.document_ids:
        statement = statement.where(models.KnowledgeDocument.id.in_(command.document_ids))
    document_ids = tuple(
        (await session.execute(statement.order_by(models.KnowledgeDocument.id))).scalars()
    )
    for document_id in document_ids:
        await acquire_xact_lock(
            session,
            knowledge_document_safety_lock_key(tenant_id, document_id),
        )
    skipped_reasons: Counter[str] = Counter()
    published_count = 0
    for document_id in document_ids:
        try:
            _, changed = await _publish_authorized(
                session,
                tenant_id=tenant_id,
                document_id=document_id,
                authorized=authorized,
                bulk=True,
                safety_lock_held=True,
            )
        except (KnowledgeConflictError, KnowledgeNotFoundError) as exc:
            skipped_reasons[exc.code] += 1
            continue
        if changed:
            published_count += 1
    return BulkPublishKnowledgeResult(
        published_count=published_count,
        skipped_count=sum(skipped_reasons.values()),
        skip_reasons=dict(sorted(skipped_reasons.items())),
    )
