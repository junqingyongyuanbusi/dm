"""Production readiness checks for the verified English knowledge corpus."""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.domain.platform_accounts import LEGACY_ACTIVE_ACCOUNT_STATUSES
from social_reply.infrastructure.database import models


async def knowledge_readiness_report(
    session: AsyncSession,
    *,
    expected_embedding_version: str,
) -> dict:
    eligible_document = (
        models.KnowledgeDocument.status == "published",
        models.KnowledgeDocument.source_language == "en",
        models.KnowledgeDocument.language_verified.is_(True),
    )
    total_published = await session.scalar(
        select(func.count())
        .select_from(models.KnowledgeDocument)
        .where(models.KnowledgeDocument.status == "published")
    )
    invalid_published = await session.scalar(
        select(func.count())
        .select_from(models.KnowledgeDocument)
        .where(
            models.KnowledgeDocument.status == "published",
            (
                (models.KnowledgeDocument.source_language != "en")
                | models.KnowledgeDocument.language_verified.is_(False)
            ),
        )
    )
    missing_or_stale_embeddings = await session.scalar(
        select(func.count())
        .select_from(models.KnowledgeDocument)
        .where(
            *eligible_document,
            ~exists(
                select(models.KnowledgeChunk.id).where(
                    models.KnowledgeChunk.document_id == models.KnowledgeDocument.id,
                    models.KnowledgeChunk.embedding_version == expected_embedding_version,
                )
            ),
        )
    )
    corpus_rows = (
        await session.execute(
            select(
                models.KnowledgeDocument.id,
                models.KnowledgeDocument.tenant_id,
                models.KnowledgeDocument.brand_id,
                models.KnowledgeDocument.platform,
                models.KnowledgeDocument.question,
                models.KnowledgeDocument.reply,
                models.KnowledgeDocument.is_official_contact,
                models.KnowledgeDocument.source_language,
                models.KnowledgeDocument.language_verified,
                models.KnowledgeChunk.content_hash,
                models.KnowledgeChunk.embedding_version,
            )
            .join(
                models.KnowledgeDocument,
                models.KnowledgeChunk.document_id == models.KnowledgeDocument.id,
            )
            .where(
                *eligible_document,
                models.KnowledgeChunk.embedding_version == expected_embedding_version,
            )
            .order_by(models.KnowledgeDocument.id, models.KnowledgeChunk.content_hash)
        )
    ).all()
    corpus_payload = [
        {
            "document_id": str(row.id),
            "tenant_id": row.tenant_id,
            "brand_id": row.brand_id,
            "platform": row.platform,
            "question": row.question,
            "reply": row.reply,
            "is_official_contact": row.is_official_contact,
            "source_language": row.source_language,
            "language_verified": row.language_verified,
            "content_hash": row.content_hash,
            "embedding_version": row.embedding_version,
        }
        for row in corpus_rows
    ]
    corpus_fingerprint = hashlib.sha256(
        json.dumps(
            corpus_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    required_scopes = (
        select(
            models.PlatformAccount.tenant_id.label("tenant_id"),
            models.PlatformAccount.brand_id.label("brand_id"),
            models.PlatformAccount.platform.label("platform"),
        )
        .where(models.PlatformAccount.status.in_(LEGACY_ACTIVE_ACCOUNT_STATUSES))
        .distinct()
        .subquery()
    )
    coverage = (
        await session.execute(
            select(
                required_scopes.c.tenant_id,
                required_scopes.c.brand_id,
                required_scopes.c.platform,
                func.count(func.distinct(models.KnowledgeDocument.id)).label("eligible"),
            )
            .select_from(required_scopes)
            .outerjoin(
                models.KnowledgeDocument,
                and_(
                    models.KnowledgeDocument.tenant_id == required_scopes.c.tenant_id,
                    models.KnowledgeDocument.brand_id == required_scopes.c.brand_id,
                    *eligible_document,
                    or_(
                        models.KnowledgeDocument.platform.is_(None),
                        models.KnowledgeDocument.platform == required_scopes.c.platform,
                    ),
                ),
            )
            .outerjoin(
                models.KnowledgeChunk,
                and_(
                    models.KnowledgeChunk.document_id == models.KnowledgeDocument.id,
                    models.KnowledgeChunk.embedding_version == expected_embedding_version,
                ),
            )
            .where(
                or_(
                    models.KnowledgeDocument.id.is_(None),
                    models.KnowledgeChunk.id.isnot(None),
                )
            )
            .group_by(
                required_scopes.c.tenant_id,
                required_scopes.c.brand_id,
                required_scopes.c.platform,
            )
            .order_by(
                required_scopes.c.tenant_id,
                required_scopes.c.brand_id,
                required_scopes.c.platform,
            )
        )
    ).all()
    missing_scopes = [
        {
            "tenant_id": row.tenant_id,
            "brand_id": row.brand_id,
            "platform": row.platform,
        }
        for row in coverage
        if row.eligible == 0
    ]
    return {
        "expected_embedding_version": expected_embedding_version,
        "corpus_fingerprint": corpus_fingerprint,
        "total_published": total_published or 0,
        "invalid_published": invalid_published or 0,
        "missing_or_stale_embeddings": missing_or_stale_embeddings or 0,
        "missing_active_scopes": missing_scopes,
        "coverage": [
            {
                "tenant_id": row.tenant_id,
                "brand_id": row.brand_id,
                "platform": row.platform,
                "eligible": row.eligible,
            }
            for row in coverage
        ],
    }


def assert_knowledge_readiness(report: dict) -> None:
    if not report["total_published"]:
        raise RuntimeError("multilingual_knowledge_not_ready:no_published_knowledge")
    if report["invalid_published"]:
        raise RuntimeError(
            f"multilingual_knowledge_not_ready:unverified_published={report['invalid_published']}"
        )
    if report["missing_or_stale_embeddings"]:
        raise RuntimeError(
            "multilingual_knowledge_not_ready:"
            f"missing_or_stale_embeddings={report['missing_or_stale_embeddings']}"
        )
    if report["missing_active_scopes"]:
        raise RuntimeError(
            "multilingual_knowledge_not_ready:missing_active_scopes="
            f"{report['missing_active_scopes']}"
        )
