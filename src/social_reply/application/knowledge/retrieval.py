"""知识检索：精确匹配 + pgvector 向量 + tsvector 词法，RRF 混合融合命中官方回复模板。

三条互补路径（安全语义不同，见各函数）：
- retrieve_exact_knowledge：question 精确相等，最可信，similarity=1.0。
- retrieve_knowledge：纯向量余弦，带 min_similarity 阈值——verbatim 直答闸门只认它，
  因为 similarity 在 [0,1] 且可比，阈值语义明确。
- retrieve_hybrid_knowledge：向量 + 词法 RRF 融合，扩大召回改善候选排序（喂 LLM），
  但 RRF 分数不在相似度量纲上，不用于 verbatim 直答闸门。
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.infrastructure.database.models import KnowledgeChunk, KnowledgeDocument


@dataclass(frozen=True)
class KnowledgeHit:
    content: str  # 展示/LLM 上下文（问+答拼接）
    reply: str  # 文档标准回复
    similarity: float  # 向量余弦相似度 1 - cosine_distance；词法-only 命中为 0.0
    chunk_id: uuid.UUID
    content_hash: str
    verbatim_safe: bool = True  # True 才允许原文直答；RRF 词法命中项为 False
    is_official_contact: bool = False


def normalize_question(value: str) -> str:
    """用于短问句/关键词模板精确匹配，忽略大小写和首尾/连续空白。"""
    return " ".join(value.casefold().strip().split())


async def retrieve_exact_knowledge(
    session: AsyncSession,
    question: str,
    *,
    tenant_id: str,
    brand_id: str,
    platform: str,
) -> KnowledgeHit | None:
    """先做确定性模板匹配，避免 True/Hi 等短词被向量模型误判。"""
    normalized = normalize_question(question)
    if not normalized:
        return None
    row = (
        await session.execute(
            select(
                KnowledgeChunk.id,
                KnowledgeChunk.content,
                KnowledgeChunk.content_hash,
                KnowledgeDocument.reply,
                KnowledgeDocument.is_official_contact,
            )
            .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
            .where(
                KnowledgeDocument.status == "published",
                KnowledgeDocument.tenant_id == tenant_id,
                KnowledgeDocument.brand_id == brand_id,
                or_(
                    KnowledgeDocument.platform.is_(None),
                    KnowledgeDocument.platform == platform,
                ),
                func.lower(func.trim(KnowledgeDocument.question)) == normalized,
            )
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    return KnowledgeHit(
        content=row.content,
        reply=row.reply,
        similarity=1.0,
        chunk_id=row.id,
        content_hash=row.content_hash,
        is_official_contact=row.is_official_contact,
    )


async def retrieve_knowledge(
    session: AsyncSession,
    query_embedding: list[float],
    *,
    tenant_id: str,
    brand_id: str,
    platform: str,
    embedding_version: str,
    top_k: int = 3,
    min_similarity: float = 0.5,
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
            KnowledgeDocument.is_official_contact,
            distance.label("distance"),
        )
        .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
        .where(
            KnowledgeDocument.status == "published",
            KnowledgeDocument.tenant_id == tenant_id,
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
            is_official_contact=row.is_official_contact,
        )
        for row in rows
    ]


# RRF 平滑常数：业界惯例 60，弱化头部名次的过度主导，融合两路排名更稳
_RRF_K = 60


async def _retrieve_lexical(
    session: AsyncSession,
    question: str,
    *,
    tenant_id: str,
    brand_id: str,
    platform: str,
    limit: int,
) -> list[tuple[uuid.UUID, str, str, str, bool]]:
    """Lexically retrieve published chunks and their official-contact classification.

    'simple' 分词器与建索引一致；plainto_tsquery 把用户输入按空白切词做 AND，
    专有名词（pip/broker/品牌名）等关键词命中是向量的补充。无匹配返回空。
    """
    normalized = normalize_question(question)
    if not normalized:
        return []
    tsquery = func.plainto_tsquery("simple", normalized)
    stmt = (
        select(
            KnowledgeChunk.id,
            KnowledgeChunk.content,
            KnowledgeChunk.content_hash,
            KnowledgeDocument.reply,
            KnowledgeDocument.is_official_contact,
        )
        .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
        .where(
            KnowledgeDocument.status == "published",
            KnowledgeDocument.tenant_id == tenant_id,
            KnowledgeDocument.brand_id == brand_id,
            or_(
                KnowledgeDocument.platform.is_(None),
                KnowledgeDocument.platform == platform,
            ),
            KnowledgeDocument.question_tsv.op("@@")(tsquery),
        )
        .order_by(func.ts_rank(KnowledgeDocument.question_tsv, tsquery).desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [(r.id, r.content, r.content_hash, r.reply, r.is_official_contact) for r in rows]


async def retrieve_hybrid_knowledge(
    session: AsyncSession,
    query_embedding: list[float],
    question: str,
    *,
    tenant_id: str,
    brand_id: str,
    platform: str,
    embedding_version: str,
    top_k: int = 3,
    min_similarity: float = 0.5,
    candidate_k: int = 20,
) -> list[KnowledgeHit]:
    """混合检索：向量 + 词法两路各取 candidate_k，用 RRF 融合后取 top_k。

    召回优先于精确排序（喂 LLM 用），故向量一路不设相似度阈值、放宽到 candidate_k。
    融合分数不在相似度量纲上——仅向量真实命中（similarity>=min_similarity）的项标
    verbatim_safe=True，其余（词法-only 或低相似度向量）标 False，保护原文直答闸门。
    """
    # 向量一路：召回放宽（min_similarity=0），保留真实相似度供 verbatim 闸门判定
    vector_hits = await retrieve_knowledge(
        session,
        query_embedding,
        tenant_id=tenant_id,
        brand_id=brand_id,
        platform=platform,
        embedding_version=embedding_version,
        top_k=candidate_k,
        min_similarity=0.0,
    )
    lexical = await _retrieve_lexical(
        session,
        question,
        tenant_id=tenant_id,
        brand_id=brand_id,
        platform=platform,
        limit=candidate_k,
    )

    # RRF：score(d) = Σ 1/(k + rank_in_list)，两路排名累加。同时记录每条的最佳相似度。
    scores: dict[uuid.UUID, float] = {}
    meta: dict[uuid.UUID, KnowledgeHit] = {}
    best_similarity: dict[uuid.UUID, float] = {}

    for rank, hit in enumerate(vector_hits):
        scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + 1.0 / (_RRF_K + rank)
        meta[hit.chunk_id] = hit
        best_similarity[hit.chunk_id] = max(best_similarity.get(hit.chunk_id, 0.0), hit.similarity)

    for rank, (cid, content, chash, reply, is_official_contact) in enumerate(lexical):
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (_RRF_K + rank)
        if cid not in meta:
            meta[cid] = KnowledgeHit(
                content=content,
                reply=reply,
                similarity=0.0,
                chunk_id=cid,
                content_hash=chash,
                is_official_contact=is_official_contact,
            )
            best_similarity.setdefault(cid, 0.0)

    ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
    results: list[KnowledgeHit] = []
    for cid, _score in ordered:
        base = meta[cid]
        sim = best_similarity.get(cid, 0.0)
        # verbatim 直答仅信任达到相似度阈值的向量命中；词法-only/低相似度不得原文外发
        results.append(
            KnowledgeHit(
                content=base.content,
                reply=base.reply,
                similarity=sim,
                chunk_id=cid,
                content_hash=base.content_hash,
                verbatim_safe=sim >= min_similarity,
                is_official_contact=base.is_official_contact,
            )
        )
    return results
