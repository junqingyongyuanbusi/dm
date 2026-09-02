from __future__ import annotations

import hashlib
import io
import re
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.application.knowledge.drafts import (
    KnowledgeDraft,
    audit_safe_category,
    audit_value_hash,
    build_knowledge_draft,
    existing_content_hashes,
    knowledge_content_hash_lock_key,
    persist_knowledge_draft,
)
from social_reply.application.knowledge.upload import (
    MAX_KNOWLEDGE_UPLOAD_BYTES,
    parse_knowledge_csv_rows,
)
from social_reply.domain.knowledge.embeddings import EmbeddingClient
from social_reply.domain.reply.language import assess_knowledge_language
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.advisory_locks import acquire_xact_lock

MAX_KNOWLEDGE_QUESTION_LENGTH = 2000
MAX_KNOWLEDGE_REPLY_LENGTH = 10000
MAX_CONFIRMATION_REASON_LENGTH = 500
MIN_UNKNOWN_CONFIRMATION_REASON_LENGTH = 10

_EMBED_BATCH_SIZE = 100
_SCOPE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
_PLATFORM_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_CATEGORY_IDENTIFIER = re.compile(r"^[\w .:-]+$", re.UNICODE)
_CONTENT_HASH_UNIQUE_CONSTRAINTS = frozenset(
    {
        "knowledge_chunks_tenant_id_content_hash_key",
        "uq_knowledge_chunks_tenant_content_hash",
    }
)


class KnowledgeApplicationError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class KnowledgeValidationError(KnowledgeApplicationError):
    pass


class KnowledgeNotFoundError(KnowledgeApplicationError):
    pass


class KnowledgeConflictError(KnowledgeApplicationError):
    pass


def knowledge_evidence_contains_hash(value: object, content_hashes: set[str]) -> bool:
    if isinstance(value, str):
        return value in content_hashes
    if isinstance(value, dict):
        return any(
            knowledge_evidence_contains_hash(item, content_hashes) for item in value.values()
        )
    if isinstance(value, list | tuple):
        return any(knowledge_evidence_contains_hash(item, content_hashes) for item in value)
    return False


def decision_references_knowledge(
    decision: models.ReplyDecision,
    *,
    document_id: uuid.UUID,
    chunk_ids: set[uuid.UUID],
    content_hashes: set[str],
) -> bool:
    return bool(
        decision.knowledge_document_id == document_id
        or decision.knowledge_chunk_id in chunk_ids
        or decision.knowledge_content_hash in content_hashes
        or decision.knowledge_top2_content_hash in content_hashes
        or knowledge_evidence_contains_hash(decision.rag_evidence, content_hashes)
    )


@dataclass(frozen=True)
class CreateKnowledgeDocumentCommand:
    required_tenant_id: str
    actor: str
    question: str
    reply: str
    brand_id: str = "default"
    platform: str | None = None
    category: str | None = None
    is_official_contact: bool = False
    protected_values: tuple[str, ...] = ()
    source_name: str = "manual"


@dataclass(frozen=True)
class ImportKnowledgeBatchCommand:
    required_tenant_id: str
    actor: str
    csv_text: str
    source_name: str
    brand_id_default: str = "default"


@dataclass(frozen=True)
class KnowledgeImportReport:
    inserted: int
    skipped: int
    blank: int
    total: int
    batch_id: uuid.UUID


@dataclass(frozen=True)
class ConfirmKnowledgeEnglishCommand:
    required_tenant_id: str
    actor: str
    document_id: uuid.UUID
    confirmation_reason: str = ""


@dataclass(frozen=True)
class ConfirmKnowledgeEnglishBatchCommand:
    required_tenant_id: str
    actor: str
    import_batch_id: uuid.UUID


@dataclass(frozen=True)
class SetKnowledgeOfficialContactCommand:
    required_tenant_id: str
    actor: str
    document_id: uuid.UUID
    is_official_contact: bool


@dataclass(frozen=True)
class DeleteKnowledgeDraftCommand:
    required_tenant_id: str
    actor: str
    document_id: uuid.UUID


def _required_text(value: str, *, field_name: str, maximum: int) -> str:
    normalized = value.strip()
    if not normalized:
        raise KnowledgeValidationError(f"{field_name}_required")
    if len(normalized) > maximum:
        raise KnowledgeValidationError(f"{field_name}_too_long")
    if any(ord(character) < 32 and character not in "\n\r\t" for character in normalized):
        raise KnowledgeValidationError(f"{field_name}_contains_control_characters")
    return normalized


def validate_knowledge_actor_scope(required_tenant_id: str, actor: str) -> tuple[str, str]:
    tenant_id = _required_text(
        required_tenant_id,
        field_name="required_tenant_id",
        maximum=64,
    )
    normalized_actor = _required_text(actor, field_name="actor", maximum=256)
    if not _SCOPE_IDENTIFIER.fullmatch(tenant_id):
        raise KnowledgeValidationError("required_tenant_id_invalid")
    return tenant_id, normalized_actor


def _validate_brand_id(value: str) -> str:
    brand_id = _required_text(value, field_name="brand_id", maximum=64)
    if not _SCOPE_IDENTIFIER.fullmatch(brand_id):
        raise KnowledgeValidationError("brand_id_invalid")
    return brand_id


def _validate_platform(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    platform = _required_text(value, field_name="platform", maximum=32)
    if not _PLATFORM_IDENTIFIER.fullmatch(platform):
        raise KnowledgeValidationError("platform_invalid")
    return platform


def _validate_category(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    category = _required_text(value, field_name="category", maximum=64)
    if not _CATEGORY_IDENTIFIER.fullmatch(category):
        raise KnowledgeValidationError("category_invalid")
    return category


def _validated_create_values(command: CreateKnowledgeDocumentCommand) -> dict[str, object]:
    tenant_id, actor = validate_knowledge_actor_scope(
        command.required_tenant_id,
        command.actor,
    )
    return {
        "tenant_id": tenant_id,
        "actor": actor,
        "question": _required_text(
            command.question,
            field_name="question",
            maximum=MAX_KNOWLEDGE_QUESTION_LENGTH,
        ),
        "reply": _required_text(
            command.reply,
            field_name="reply",
            maximum=MAX_KNOWLEDGE_REPLY_LENGTH,
        ),
        "brand_id": _validate_brand_id(command.brand_id),
        "platform": _validate_platform(command.platform),
        "category": _validate_category(command.category),
        "source_name": _required_text(
            command.source_name,
            field_name="source_name",
            maximum=256,
        ),
    }


def _integrity_constraint_name(exc: IntegrityError) -> str | None:
    current: BaseException | None = exc.orig
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        constraint_name = getattr(current, "constraint_name", None)
        if isinstance(constraint_name, str):
            return constraint_name
        current = current.__cause__ or current.__context__
    return None


def _is_content_hash_duplicate(exc: IntegrityError) -> bool:
    return _integrity_constraint_name(exc) in _CONTENT_HASH_UNIQUE_CONSTRAINTS


async def _acquire_content_hash_locks(
    session: AsyncSession,
    *,
    tenant_id: str,
    content_hashes: list[str],
) -> None:
    for content_hash in sorted(set(content_hashes)):
        await acquire_xact_lock(
            session,
            knowledge_content_hash_lock_key(tenant_id, content_hash),
        )


async def _persist_knowledge_draft_with_duplicate_fallback(
    session: AsyncSession,
    draft: KnowledgeDraft,
    *,
    embedding_version: str,
    embedding: list[float],
    actor: str,
) -> models.KnowledgeDocument:
    try:
        async with session.begin_nested():
            document = await persist_knowledge_draft(
                session,
                draft,
                embedding_version=embedding_version,
                embedding=embedding,
                actor=actor,
            )
            await session.flush()
    except IntegrityError as exc:
        if _is_content_hash_duplicate(exc):
            raise KnowledgeConflictError("knowledge_document_duplicate") from exc
        raise
    return document


async def execute_create_knowledge_document(
    session: AsyncSession,
    command: CreateKnowledgeDocumentCommand,
    *,
    embedder: EmbeddingClient,
) -> models.KnowledgeDocument:
    values = _validated_create_values(command)
    detected_language, detection_status = assess_knowledge_language(
        str(values["question"]),
        str(values["reply"]),
    )
    try:
        draft = build_knowledge_draft(
            tenant_id=str(values["tenant_id"]),
            question=str(values["question"]),
            reply=str(values["reply"]),
            brand_id=str(values["brand_id"]),
            platform=values["platform"] if isinstance(values["platform"], str) else None,
            category=values["category"] if isinstance(values["category"], str) else None,
            is_official_contact=command.is_official_contact,
            protected_values=command.protected_values,
            detected_language=detected_language,
            language_detection_status=detection_status,
            source_file=str(values["source_name"]),
        )
    except ValueError as exc:
        raise KnowledgeValidationError(str(exc)) from exc
    existing = await existing_content_hashes(
        session,
        tenant_id=draft.tenant_id,
        content_hashes=[draft.content_hash],
    )
    if existing:
        raise KnowledgeConflictError("knowledge_document_duplicate")
    embeddings = await embedder.embed([draft.embed_text])
    if len(embeddings) != 1:
        raise KnowledgeValidationError("knowledge_embedding_count_invalid")
    await _acquire_content_hash_locks(
        session,
        tenant_id=draft.tenant_id,
        content_hashes=[draft.content_hash],
    )
    concurrent_existing = await existing_content_hashes(
        session,
        tenant_id=draft.tenant_id,
        content_hashes=[draft.content_hash],
    )
    if concurrent_existing:
        raise KnowledgeConflictError("knowledge_document_duplicate")
    return await _persist_knowledge_draft_with_duplicate_fallback(
        session,
        draft,
        embedding_version=embedder.version,
        embedding=embeddings[0],
        actor=str(values["actor"]),
    )


async def execute_import_knowledge_batch(
    session: AsyncSession,
    command: ImportKnowledgeBatchCommand,
    *,
    embedder: EmbeddingClient,
) -> KnowledgeImportReport:
    tenant_id, actor = validate_knowledge_actor_scope(
        command.required_tenant_id,
        command.actor,
    )
    brand_id_default = _validate_brand_id(command.brand_id_default)
    source_name = _required_text(
        command.source_name,
        field_name="source_name",
        maximum=256,
    )
    if len(command.csv_text.encode("utf-8")) > MAX_KNOWLEDGE_UPLOAD_BYTES:
        raise KnowledgeValidationError("knowledge_csv_too_large")
    batch_id = uuid.uuid4()
    try:
        parsed = parse_knowledge_csv_rows(
            io.StringIO(command.csv_text),
            tenant_id=tenant_id,
            brand_id_default=brand_id_default,
            source_name=source_name,
            batch_id=batch_id,
        )
        for draft in parsed.rows:
            _validated_create_values(
                CreateKnowledgeDocumentCommand(
                    required_tenant_id=tenant_id,
                    actor=actor,
                    question=draft.question,
                    reply=draft.reply,
                    brand_id=draft.brand_id,
                    platform=draft.platform,
                    category=draft.category,
                    is_official_contact=draft.is_official_contact,
                    protected_values=draft.protected_values,
                    source_name=source_name,
                )
            )
    except ValueError as exc:
        raise KnowledgeValidationError(str(exc)) from exc

    existing = await existing_content_hashes(
        session,
        tenant_id=tenant_id,
        content_hashes=[draft.content_hash for draft in parsed.rows],
    )
    seen: set[str] = set()
    new_rows = []
    skipped = 0
    for draft in parsed.rows:
        if draft.content_hash in existing or draft.content_hash in seen:
            skipped += 1
            continue
        seen.add(draft.content_hash)
        new_rows.append(draft)

    embeddings: list[list[float]] = []
    for offset in range(0, len(new_rows), _EMBED_BATCH_SIZE):
        batch = new_rows[offset : offset + _EMBED_BATCH_SIZE]
        embeddings.extend(await embedder.embed([draft.embed_text for draft in batch]))
    if len(embeddings) != len(new_rows):
        raise KnowledgeValidationError("knowledge_embedding_count_invalid")

    await _acquire_content_hash_locks(
        session,
        tenant_id=tenant_id,
        content_hashes=[draft.content_hash for draft in new_rows],
    )
    concurrent_existing = await existing_content_hashes(
        session,
        tenant_id=tenant_id,
        content_hashes=[draft.content_hash for draft in new_rows],
    )
    inserted = 0
    for draft, embedding in zip(new_rows, embeddings, strict=True):
        if draft.content_hash in concurrent_existing:
            skipped += 1
            continue
        try:
            await _persist_knowledge_draft_with_duplicate_fallback(
                session,
                draft,
                embedding_version=embedder.version,
                embedding=embedding,
                actor=actor,
            )
        except KnowledgeConflictError as exc:
            if exc.code != "knowledge_document_duplicate":
                raise
            skipped += 1
            continue
        inserted += 1
    session.add(
        models.AuditLog(
            tenant_id=tenant_id,
            category="admin_action",
            actor=actor,
            action="IMPORT_KNOWLEDGE_BATCH",
            subject_type="knowledge_import_batch",
            subject_id=str(batch_id),
            detail={
                "batch_id": str(batch_id),
                "inserted_count": inserted,
                "skipped_count": skipped,
                "blank_count": parsed.blank_count,
                "total_count": parsed.total_count,
            },
        )
    )
    return KnowledgeImportReport(
        inserted=inserted,
        skipped=skipped,
        blank=parsed.blank_count,
        total=parsed.total_count,
        batch_id=batch_id,
    )


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
            .with_for_update()
        )
    ).scalar_one_or_none()
    if document is None:
        raise KnowledgeNotFoundError("knowledge_document_not_found")
    return document


def _validated_confirmation_reason(document: models.KnowledgeDocument, value: str) -> str:
    allowed_detection_statuses = {"english", "mixed", "non_english", "unknown"}
    if document.language_detection_status not in allowed_detection_statuses:
        raise KnowledgeConflictError("knowledge_language_detection_status_invalid")
    reason = value.strip()
    if len(reason) > MAX_CONFIRMATION_REASON_LENGTH:
        raise KnowledgeValidationError("confirmation_reason_too_long")
    if (
        document.language_detection_status == "unknown"
        and len(reason) < MIN_UNKNOWN_CONFIRMATION_REASON_LENGTH
    ):
        raise KnowledgeValidationError("confirmation_reason_required_for_unknown_language")
    return reason


async def execute_confirm_knowledge_english(
    session: AsyncSession,
    command: ConfirmKnowledgeEnglishCommand,
) -> models.KnowledgeDocument:
    tenant_id, actor = validate_knowledge_actor_scope(
        command.required_tenant_id,
        command.actor,
    )
    document = await _locked_document(
        session,
        tenant_id=tenant_id,
        document_id=command.document_id,
    )
    if document.status != "draft":
        raise KnowledgeConflictError("unpublish_before_language_confirmation")
    if document.language_detection_status in {"mixed", "non_english"}:
        raise KnowledgeConflictError("english_replacement_required")
    reason = _validated_confirmation_reason(document, command.confirmation_reason)
    if document.source_language == "en" and document.language_verified:
        return document
    document.source_language = "en"
    document.language_verified = True
    reason_hash = hashlib.sha256(reason.encode()).hexdigest() if reason else None
    session.add(
        models.AuditLog(
            tenant_id=tenant_id,
            category="admin_action",
            actor=actor,
            action="CONFIRM_KNOWLEDGE_ENGLISH",
            subject_type="knowledge_document",
            subject_id=str(document.id),
            detail={
                "detected_language": document.detected_language,
                "detection_status": document.language_detection_status,
                "import_batch_id": str(document.import_batch_id)
                if document.import_batch_id
                else None,
                "confirmation_reason_hash": reason_hash,
                "confirmation_reason_length": len(reason),
                "bulk": False,
            },
        )
    )
    return document


async def execute_confirm_knowledge_english_batch(
    session: AsyncSession,
    command: ConfirmKnowledgeEnglishBatchCommand,
) -> int:
    tenant_id, actor = validate_knowledge_actor_scope(
        command.required_tenant_id,
        command.actor,
    )
    documents = list(
        (
            await session.execute(
                select(models.KnowledgeDocument)
                .where(
                    models.KnowledgeDocument.tenant_id == tenant_id,
                    models.KnowledgeDocument.import_batch_id == command.import_batch_id,
                    models.KnowledgeDocument.status == "draft",
                    models.KnowledgeDocument.language_detection_status == "english",
                    models.KnowledgeDocument.language_verified.is_(False),
                )
                .with_for_update()
            )
        ).scalars()
    )
    if not documents:
        batch_exists = await session.scalar(
            select(models.KnowledgeDocument.id)
            .where(
                models.KnowledgeDocument.tenant_id == tenant_id,
                models.KnowledgeDocument.import_batch_id == command.import_batch_id,
            )
            .limit(1)
        )
        if batch_exists is None:
            raise KnowledgeNotFoundError("knowledge_import_batch_not_found")
        return 0
    confirmation_batch_id = uuid.uuid4()
    for document in documents:
        document.source_language = "en"
        document.language_verified = True
        session.add(
            models.AuditLog(
                tenant_id=tenant_id,
                category="admin_action",
                actor=actor,
                action="CONFIRM_KNOWLEDGE_ENGLISH",
                subject_type="knowledge_document",
                subject_id=str(document.id),
                detail={
                    "detected_language": document.detected_language,
                    "detection_status": document.language_detection_status,
                    "import_batch_id": str(command.import_batch_id),
                    "confirmation_batch_id": str(confirmation_batch_id),
                    "bulk": True,
                },
            )
        )
    return len(documents)


async def execute_set_knowledge_official_contact(
    session: AsyncSession,
    command: SetKnowledgeOfficialContactCommand,
) -> models.KnowledgeDocument:
    tenant_id, actor = validate_knowledge_actor_scope(
        command.required_tenant_id,
        command.actor,
    )
    document = await _locked_document(
        session,
        tenant_id=tenant_id,
        document_id=command.document_id,
    )
    if document.status != "draft":
        raise KnowledgeConflictError("unpublish_before_classification")
    previous = document.is_official_contact
    if previous == command.is_official_contact:
        return document
    content_hash = await session.scalar(
        select(models.KnowledgeChunk.content_hash)
        .where(
            models.KnowledgeChunk.tenant_id == tenant_id,
            models.KnowledgeChunk.document_id == document.id,
        )
        .limit(1)
    )
    document.is_official_contact = command.is_official_contact
    session.add(
        models.AuditLog(
            tenant_id=tenant_id,
            category="admin_action",
            actor=actor,
            action="SET_KNOWLEDGE_OFFICIAL_CONTACT",
            subject_type="knowledge_document",
            subject_id=str(document.id),
            detail={
                "from": previous,
                "to": command.is_official_contact,
                "brand_hash": audit_value_hash(document.brand_id),
                "platform_hash": audit_value_hash(document.platform),
                "status": document.status,
                "content_hash": content_hash,
            },
        )
    )
    return document


async def execute_delete_knowledge_draft(
    session: AsyncSession,
    command: DeleteKnowledgeDraftCommand,
) -> None:
    tenant_id, actor = validate_knowledge_actor_scope(
        command.required_tenant_id,
        command.actor,
    )
    document = await _locked_document(
        session,
        tenant_id=tenant_id,
        document_id=command.document_id,
    )
    if document.status == "published":
        raise KnowledgeConflictError("unpublish_knowledge_before_delete")
    localization_exists = await session.scalar(
        select(models.KnowledgeLocalization.id)
        .where(
            models.KnowledgeLocalization.tenant_id == tenant_id,
            models.KnowledgeLocalization.document_id == document.id,
        )
        .limit(1)
    )
    if localization_exists is not None:
        raise KnowledgeConflictError("knowledge_with_localization_history_is_immutable")
    chunk_rows = (
        await session.execute(
            select(models.KnowledgeChunk.id, models.KnowledgeChunk.content_hash).where(
                models.KnowledgeChunk.tenant_id == tenant_id,
                models.KnowledgeChunk.document_id == document.id,
            )
        )
    ).all()
    chunk_ids = {row.id for row in chunk_rows}
    content_hashes = {row.content_hash for row in chunk_rows}
    decisions = list(
        (
            await session.execute(
                select(models.ReplyDecision).where(models.ReplyDecision.tenant_id == tenant_id)
            )
        ).scalars()
    )
    if any(
        decision_references_knowledge(
            decision,
            document_id=document.id,
            chunk_ids=chunk_ids,
            content_hashes=content_hashes,
        )
        for decision in decisions
    ):
        raise KnowledgeConflictError("knowledge_with_decision_history_is_immutable")
    content_hash = await session.scalar(
        select(models.KnowledgeChunk.content_hash)
        .where(
            models.KnowledgeChunk.tenant_id == tenant_id,
            models.KnowledgeChunk.document_id == document.id,
        )
        .limit(1)
    )
    session.add(
        models.AuditLog(
            tenant_id=tenant_id,
            category="admin_action",
            actor=actor,
            action="DELETE_KNOWLEDGE_DOCUMENT",
            subject_type="knowledge_document",
            subject_id=str(document.id),
            detail={
                "content_hash": content_hash,
                "question_length": len(document.question),
                "reply_length": len(document.reply),
                "category": audit_safe_category(
                    document.category,
                    document.protected_values or (),
                ),
                "category_hash": audit_value_hash(document.category),
                "import_batch_id": str(document.import_batch_id)
                if document.import_batch_id
                else None,
            },
        )
    )
    await session.delete(document)
