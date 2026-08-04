import hashlib
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.infrastructure.database.models import AuditLog, KnowledgeChunk, KnowledgeDocument


@dataclass(frozen=True)
class KnowledgeDraft:
    tenant_id: str
    question: str
    reply: str
    brand_id: str
    platform: str | None
    category: str | None
    is_official_contact: bool
    source_file: str
    content: str
    embed_text: str
    content_hash: str


def build_knowledge_draft(
    *,
    tenant_id: str,
    question: str,
    reply: str,
    brand_id: str,
    platform: str | None = None,
    category: str | None = None,
    is_official_contact: bool = False,
    source_file: str,
) -> KnowledgeDraft:
    content = f"问：{question}\n答：{reply}"
    return KnowledgeDraft(
        tenant_id=tenant_id,
        question=question,
        reply=reply,
        brand_id=brand_id,
        platform=platform,
        category=category,
        is_official_contact=is_official_contact,
        source_file=source_file,
        content=content,
        embed_text=question,
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
    )


async def existing_content_hashes(
    session: AsyncSession, *, tenant_id: str, content_hashes: list[str]
) -> set[str]:
    if not content_hashes:
        return set()
    return set(
        (
            await session.execute(
                select(KnowledgeChunk.content_hash).where(
                    KnowledgeChunk.tenant_id == tenant_id,
                    KnowledgeChunk.content_hash.in_(content_hashes),
                )
            )
        ).scalars()
    )


async def persist_knowledge_draft(
    session: AsyncSession,
    draft: KnowledgeDraft,
    *,
    embedding_version: str,
    embedding: list[float],
    actor: str,
) -> KnowledgeDocument:
    document = KnowledgeDocument(
        tenant_id=draft.tenant_id,
        brand_id=draft.brand_id,
        platform=draft.platform,
        category=draft.category,
        question=draft.question,
        reply=draft.reply,
        status="draft",
        is_official_contact=draft.is_official_contact,
        source_file=draft.source_file,
    )
    session.add(document)
    await session.flush()
    session.add(
        KnowledgeChunk(
            tenant_id=draft.tenant_id,
            document_id=document.id,
            content=draft.content,
            embed_text=draft.embed_text,
            content_hash=draft.content_hash,
            embedding_version=embedding_version,
            embedding=embedding,
        )
    )
    if draft.is_official_contact:
        session.add(
            AuditLog(
                tenant_id=draft.tenant_id,
                category="admin_action",
                actor=actor,
                action="SET_KNOWLEDGE_OFFICIAL_CONTACT",
                subject_type="knowledge_document",
                subject_id=str(document.id),
                detail={
                    "from": False,
                    "to": True,
                    "brand": draft.brand_id,
                    "platform": draft.platform,
                    "status": "draft",
                    "content_hash": draft.content_hash,
                },
            )
        )
    return document
