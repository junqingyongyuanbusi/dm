"""知识检索：pgvector 余弦相似度 top-k 命中官方回复模板"""

import uuid
from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.infrastructure.database.models import KnowledgeChunk, KnowledgeDocument


@dataclass(frozen=True)
class KnowledgeHit:
    content: str  # 参与 embedding 的模板文本（问+答）
    reply: str  # 文档标准回复
    similarity: float  # 1 - cosine_distance
    chunk_id: uuid.UUID
    content_hash: str


async def retrieve_knowledge(
    session: AsyncSession, query_embedding: list[float], *,
    brand_id: str, platform: str, embedding_version: str,
    top_k: int = 3, min_similarity: float = 0.5,
) -> list[KnowledgeHit]:
    """按余弦相似度检索已发布模板：过滤品牌、平台（NULL=全平台）、当前 embedding 版本。

    similarity = 1 - cosine_distance >= min_similarity；按距离升序取 top_k。
    """
    distance = KnowledgeChunk.embedding.cosine_distance(query_embedding)
    stmt = (
        select(
            KnowledgeChunk.id,
            KnowledgeChunk.content,
            KnowledgeChunk.content_hash,
            KnowledgeDocument.reply,
            distance.label("distance"),
        )
        .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
        .where(
            KnowledgeDocument.status == "published",
            KnowledgeDocument.brand_id == brand_id,
            or_(
                KnowledgeDocument.platform.is_(None),
                KnowledgeDocument.platform == platform,
            ),
            # 版本过滤：以查询向量的实际来源版本为准（换模型/Fake 的旧向量不可比，绝不混用）
            KnowledgeChunk.embedding_version == embedding_version,
            distance <= 1.0 - min_similarity,
        )
        .order_by(distance.asc())
        .limit(top_k)
    )
    rows = (await session.execute(stmt)).all()
    return [
        KnowledgeHit(
            content=row.content,
            reply=row.reply,
            similarity=1.0 - row.distance,
            chunk_id=row.id,
            content_hash=row.content_hash,
        )
        for row in rows
    ]
