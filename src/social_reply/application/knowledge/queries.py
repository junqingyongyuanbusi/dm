from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.application.knowledge.commands import (
    KnowledgeNotFoundError,
    KnowledgeValidationError,
    validate_knowledge_actor_scope,
)
from social_reply.domain.reply.guard import has_contact_like
from social_reply.infrastructure.database import models

KNOWLEDGE_STATUS_FILTERS = frozenset({"all", "review", "draft", "published"})
KNOWLEDGE_REVIEW_DETECTION_STATUSES = frozenset({"english", "mixed", "non_english", "unknown"})


@dataclass(frozen=True)
class ListKnowledgeDocumentsQuery:
    required_tenant_id: str
    actor: str
    status_filter: str = "all"
    brand_id: str | None = None
    platform: str | None = None
    category: str | None = None
    limit: int = 200


@dataclass(frozen=True)
class GetKnowledgeDocumentQuery:
    required_tenant_id: str
    actor: str
    document_id: uuid.UUID


@dataclass(frozen=True)
class SearchPublishedKnowledgeQuery:
    required_tenant_id: str
    actor: str
    search_text: str
    allowed_brand_ids: tuple[str, ...] | None
    limit: int = 20


def knowledge_review_condition():
    return and_(
        models.KnowledgeDocument.status == "draft",
        models.KnowledgeDocument.language_verified.is_(False),
        models.KnowledgeDocument.language_detection_status.in_(KNOWLEDGE_REVIEW_DETECTION_STATUSES),
    )


def _escaped_like_contains_pattern(value: str) -> str:
    escaped_value = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped_value}%"


async def execute_search_published_knowledge(
    session: AsyncSession,
    query: SearchPublishedKnowledgeQuery,
) -> list[models.KnowledgeDocument]:
    tenant_id, _actor = validate_knowledge_actor_scope(
        query.required_tenant_id,
        query.actor,
    )
    search_text = query.search_text.strip()
    if not search_text:
        return []
    if len(search_text) > 500:
        raise KnowledgeValidationError("knowledge_query_too_long")
    if not 1 <= query.limit <= 100:
        raise KnowledgeValidationError("invalid_knowledge_query_limit")
    search_pattern = _escaped_like_contains_pattern(search_text)
    statement = select(models.KnowledgeDocument).where(
        models.KnowledgeDocument.tenant_id == tenant_id,
        models.KnowledgeDocument.status == "published",
        or_(
            models.KnowledgeDocument.question.ilike(search_pattern, escape="\\"),
            models.KnowledgeDocument.reply.ilike(search_pattern, escape="\\"),
        ),
    )
    if query.allowed_brand_ids is not None:
        statement = statement.where(
            models.KnowledgeDocument.brand_id.in_(query.allowed_brand_ids)
        )
    return list(
        (
            await session.execute(
                statement.order_by(models.KnowledgeDocument.updated_at.desc()).limit(query.limit)
            )
        ).scalars()
    )


async def execute_list_knowledge_documents(
    session: AsyncSession,
    query: ListKnowledgeDocumentsQuery,
) -> list[models.KnowledgeDocument]:
    tenant_id, _actor = validate_knowledge_actor_scope(
        query.required_tenant_id,
        query.actor,
    )
    if query.status_filter not in KNOWLEDGE_STATUS_FILTERS:
        raise KnowledgeValidationError("invalid_knowledge_status_filter")
    if not 1 <= query.limit <= 500:
        raise KnowledgeValidationError("invalid_knowledge_query_limit")
    statement = select(models.KnowledgeDocument).where(
        models.KnowledgeDocument.tenant_id == tenant_id
    )
    if query.status_filter in {"draft", "published"}:
        statement = statement.where(models.KnowledgeDocument.status == query.status_filter)
    elif query.status_filter == "review":
        statement = statement.where(knowledge_review_condition())
    if query.brand_id:
        statement = statement.where(models.KnowledgeDocument.brand_id == query.brand_id)
    if query.platform:
        statement = statement.where(models.KnowledgeDocument.platform == query.platform)
    if query.category:
        statement = statement.where(models.KnowledgeDocument.category == query.category)
    return list(
        (
            await session.execute(
                statement.order_by(models.KnowledgeDocument.updated_at.desc()).limit(query.limit)
            )
        ).scalars()
    )


async def execute_get_knowledge_document(
    session: AsyncSession,
    query: GetKnowledgeDocumentQuery,
) -> models.KnowledgeDocument:
    tenant_id, _actor = validate_knowledge_actor_scope(
        query.required_tenant_id,
        query.actor,
    )
    document = await session.scalar(
        select(models.KnowledgeDocument).where(
            models.KnowledgeDocument.tenant_id == tenant_id,
            models.KnowledgeDocument.id == query.document_id,
        )
    )
    if document is None:
        raise KnowledgeNotFoundError("knowledge_document_not_found")
    return document


async def load_knowledge_filter_values(
    session: AsyncSession,
    *,
    required_tenant_id: str,
    actor: str,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    tenant_id, _actor = validate_knowledge_actor_scope(required_tenant_id, actor)
    documents = list(
        (
            await session.execute(
                select(models.KnowledgeDocument).where(
                    models.KnowledgeDocument.tenant_id == tenant_id
                )
            )
        ).scalars()
    )
    safe_documents = [
        document
        for document in documents
        if not (
            document.is_official_contact
            or document.protected_values
            or has_contact_like(document.question or "")
            or has_contact_like(document.reply or "")
        )
    ]

    def distinct_values(attribute_name: str) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    str(value)
                    for document in safe_documents
                    if (value := getattr(document, attribute_name)) not in (None, "")
                    and not has_contact_like(str(value))
                }
            )
        )

    return (
        distinct_values("brand_id"),
        distinct_values("platform"),
        distinct_values("category"),
    )
