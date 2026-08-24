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
    document_id: uuid.UUID
    chunk_id: uuid.UUID
    content_hash: str
    verbatim_safe: bool = True  # True 才允许原文直答；RRF 词法命中项为 False
    is_official_contact: bool = False
    source_language: str = "und"
    language_verified: bool = False
    question: str = ""


@dataclass(frozen=True)
class KnowledgeRetrievalResult:
    hits: tuple[KnowledgeHit, ...] = ()
    vector_hits: tuple[KnowledgeHit, ...] = ()
    exact_match: bool = False
    exact_ambiguous: bool = False
    query_embedding: tuple[float, ...] | None = None
    error_code: str | None = None
    embedding_version: str | None = None
    retrieval_mode: str | None = None


def normalize_question(value: str) -> str:
    """用于短问句/关键词模板精确匹配，忽略大小写和首尾/连续空白。"""
    return " ".join(value.casefold().strip().split())


def canonical_answer_identity(reply: str, is_official_contact: bool) -> tuple[str, bool]:
    """Stable identity for one approved answer inside a scoped knowledge set."""
    return (" ".join(reply.casefold().split()), is_official_contact)

async def retrieve_exact_knowledge_result(
    session: AsyncSession,
    question: str,
    *,
    tenant_id: str,
    brand_id: str,
    platform: str,
    verified_english_only: bool = False,
) -> KnowledgeRetrievalResult:
    """Return a unique exact match, or explicit ambiguity when answers conflict."""
    normalized = normalize_question(question)
    if not normalized:
        return KnowledgeRetrievalResult()
    language_scope = (
        (
            KnowledgeDocument.source_language == "en",
            KnowledgeDocument.language_verified.is_(True),
        )
        if verified_english_only
        else ()
    )
    rows = (
        await session.execute(
            select(
                KnowledgeDocument.id.label("document_id"),
                KnowledgeChunk.id,
                KnowledgeChunk.content,
                KnowledgeChunk.content_hash,
                KnowledgeDocument.question,
                KnowledgeDocument.reply,
                KnowledgeDocument.is_official_contact,
                KnowledgeDocument.source_language,
                KnowledgeDocument.language_verified,
            )
            .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
            .where(
                KnowledgeDocument.status == "published",
                *language_scope,
                KnowledgeDocument.tenant_id == tenant_id,
                KnowledgeChunk.tenant_id == tenant_id,
                KnowledgeDocument.brand_id == brand_id,
                or_(
                    KnowledgeDocument.platform.is_(None),
                    KnowledgeDocument.platform == platform,
                ),
                func.regexp_replace(
                    func.lower(func.trim(KnowledgeDocument.question)),
                    r"\s+",
                    " ",
                    "g",
                )
                == normalized,
            )
            .order_by(KnowledgeChunk.content_hash)
        )
    ).all()
    rows_by_document = {row.document_id: row for row in rows}
    hits = tuple(
        KnowledgeHit(
            content=row.content,
            reply=row.reply,
            similarity=1.0,
            document_id=row.document_id,
            chunk_id=row.id,
            content_hash=row.content_hash,
            is_official_contact=row.is_official_contact,
            source_language=row.source_language,
            language_verified=row.language_verified,
            question=row.question,
        )
        for row in rows_by_document.values()
    )
    if not hits:
        return KnowledgeRetrievalResult()
    evidence = {
        canonical_answer_identity(hit.reply, hit.is_official_contact) for hit in hits
    }
    if len(evidence) > 1:
        return KnowledgeRetrievalResult(
            hits=hits,
            vector_hits=hits,
            exact_ambiguous=True,
        )
    selected = hits[0]
    return KnowledgeRetrievalResult(
        hits=(selected,),
        vector_hits=(selected,),
        exact_match=True,
    )


async def retrieve_exact_knowledge(
    session: AsyncSession,
    question: str,
    *,
    tenant_id: str,
    brand_id: str,
    platform: str,
    verified_english_only: bool = False,
) -> KnowledgeHit | None:
    result = await retrieve_exact_knowledge_result(
        session,
        question,
        tenant_id=tenant_id,
        brand_id=brand_id,
        platform=platform,
        verified_english_only=verified_english_only,
    )
    return result.hits[0] if result.exact_match else None


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
    verified_english_only: bool = False,
) -> list[KnowledgeHit]:
    """按余弦相似度检索已发布模板：过滤品牌、平台（NULL=全平台）、当前 embedding 版本。

    similarity = 1 - cosine_distance >= min_similarity；按距离升序取 top_k。
    """
    distance = KnowledgeChunk.embedding.cosine_distance(query_embedding)
    language_scope = (
        (
            KnowledgeDocument.source_language == "en",
            KnowledgeDocument.language_verified.is_(True),
        )
        if verified_english_only
        else ()
    )
    stmt = (
        select(
            KnowledgeDocument.id.label("document_id"),
            KnowledgeChunk.id,
            KnowledgeChunk.content,
            KnowledgeChunk.content_hash,
            KnowledgeDocument.question,
            KnowledgeDocument.reply,
            KnowledgeDocument.is_official_contact,
            KnowledgeDocument.source_language,
            KnowledgeDocument.language_verified,
            distance.label("distance"),
        )
        .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
        .where(
            KnowledgeDocument.status == "published",
            *language_scope,
            KnowledgeDocument.tenant_id == tenant_id,
            KnowledgeChunk.tenant_id == tenant_id,
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
            document_id=row.document_id,
            chunk_id=row.id,
            content_hash=row.content_hash,
            is_official_contact=row.is_official_contact,
            source_language=row.source_language,
            language_verified=row.language_verified,
            question=row.question,
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
    verified_english_only: bool = False,
) -> list[tuple[uuid.UUID, uuid.UUID, str, str, str, str, bool, str, bool]]:
    """Lexically retrieve published chunks and their official-contact classification.

    'simple' 分词器与建索引一致；plainto_tsquery 把用户输入按空白切词做 AND，
    专有名词（pip/broker/品牌名）等关键词命中是向量的补充。无匹配返回空。
    """
    normalized = normalize_question(question)
    if not normalized:
        return []
    language_scope = (
        (
            KnowledgeDocument.source_language == "en",
            KnowledgeDocument.language_verified.is_(True),
        )
        if verified_english_only
        else ()
    )
    tsquery = func.plainto_tsquery("simple", normalized)
    stmt = (
        select(
            KnowledgeDocument.id.label("document_id"),
            KnowledgeChunk.id,
            KnowledgeChunk.content,
            KnowledgeChunk.content_hash,
            KnowledgeDocument.question,
            KnowledgeDocument.reply,
            KnowledgeDocument.is_official_contact,
            KnowledgeDocument.source_language,
            KnowledgeDocument.language_verified,
        )
        .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
        .where(
            KnowledgeDocument.status == "published",
            *language_scope,
            KnowledgeDocument.tenant_id == tenant_id,
            KnowledgeChunk.tenant_id == tenant_id,
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
    return [
        (
            row.document_id,
            row.id,
            row.content,
            row.content_hash,
            row.question,
            row.reply,
            row.is_official_contact,
            row.source_language,
            row.language_verified,
        )
        for row in rows
    ]


async def retrieve_hybrid_knowledge_result(
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
    verified_english_only: bool = False,
    candidate_k: int = 20,
) -> KnowledgeRetrievalResult:
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
        verified_english_only=verified_english_only,
    )
    lexical = await _retrieve_lexical(
        session,
        question,
        tenant_id=tenant_id,
        brand_id=brand_id,
        platform=platform,
        limit=candidate_k,
        verified_english_only=verified_english_only,
    )

    # RRF：score(d) = Σ 1/(k + rank_in_list)，两路排名累加。同时记录每条的最佳相似度。
    scores: dict[uuid.UUID, float] = {}
    meta: dict[uuid.UUID, KnowledgeHit] = {}
    best_similarity: dict[uuid.UUID, float] = {}

    for rank, hit in enumerate(vector_hits):
        scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + 1.0 / (_RRF_K + rank)
        meta[hit.chunk_id] = hit
        best_similarity[hit.chunk_id] = max(best_similarity.get(hit.chunk_id, 0.0), hit.similarity)

    for rank, (
        document_id,
        cid,
        content,
        content_hash,
        question,
        reply,
        is_official_contact,
        source_language,
        language_verified,
    ) in enumerate(lexical):
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (_RRF_K + rank)
        if cid not in meta:
            meta[cid] = KnowledgeHit(
                content=content,
                reply=reply,
                similarity=0.0,
                document_id=document_id,
                chunk_id=cid,
                content_hash=content_hash,
                is_official_contact=is_official_contact,
                source_language=source_language,
                language_verified=language_verified,
                question=question,
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
                document_id=base.document_id,
                chunk_id=cid,
                content_hash=base.content_hash,
                verbatim_safe=sim >= min_similarity,
                is_official_contact=base.is_official_contact,
                source_language=base.source_language,
                language_verified=base.language_verified,
                question=base.question,
            )
        )
    return KnowledgeRetrievalResult(hits=tuple(results), vector_hits=tuple(vector_hits))


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
    verified_english_only: bool = False,
    candidate_k: int = 20,
) -> list[KnowledgeHit]:
    result = await retrieve_hybrid_knowledge_result(
        session,
        query_embedding,
        question,
        tenant_id=tenant_id,
        brand_id=brand_id,
        platform=platform,
        embedding_version=embedding_version,
        top_k=top_k,
        min_similarity=min_similarity,
        verified_english_only=verified_english_only,
        candidate_k=candidate_k,
    )
    return list(result.hits)
