"""回复模板 CSV 导入：content_hash 幂等 + 批量 embedding"""

import csv
import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from sqlalchemy import select

from social_reply.domain.knowledge.embeddings import EmbeddingClient
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.database.models import KnowledgeChunk, KnowledgeDocument

logger = logging.getLogger(__name__)

_REQUIRED_HEADERS = {"question", "reply"}
_EMBED_BATCH_SIZE = 100  # 单次 embeddings 请求上限，防超大 CSV 打爆单请求
MAX_IMPORT_ROWS = 2000


@dataclass(frozen=True)
class ImportReport:
    inserted: int
    skipped: int  # content_hash 已存在（重复模板）
    blank: int  # 空 question/reply 行
    total: int  # CSV 有效数据行数（含空行）


@dataclass(frozen=True)
class _Row:
    question: str
    reply: str
    brand_id: str
    platform: str | None
    category: str | None
    content: str  # 展示/LLM 上下文（问+答），也是幂等 content_hash 的来源
    embed_text: str  # 实际送去 embedding 的文本（非对称：仅 question）
    content_hash: str


def _parse_rows(f: TextIO, brand_id_default: str) -> tuple[list[_Row], int]:
    """解析 CSV，返回 (有效行, 空行数)；表头缺失或超行数抛 ValueError"""
    reader = csv.DictReader(f)
    headers = set(reader.fieldnames or [])
    missing = _REQUIRED_HEADERS - headers
    if missing:
        raise ValueError(
            f"CSV 表头缺少必需列: {'、'.join(sorted(missing))}（必需 question,reply）"
        )
    rows: list[_Row] = []
    blank = 0
    for raw in reader:
        question = (raw.get("question") or "").strip()
        reply = (raw.get("reply") or "").strip()
        if not question or not reply:
            blank += 1
            logger.warning("跳过空行（question/reply 为空）: %r", raw)
            continue
        content = f"问：{question}\n答：{reply}"
        rows.append(
            _Row(
                question=question,
                reply=reply,
                brand_id=(raw.get("brand_id") or "").strip() or brand_id_default,
                platform=(raw.get("platform") or "").strip() or None,
                category=(raw.get("category") or "").strip() or None,
                content=content,
                # 非对称嵌入：只 embed 问题，与用户 query 同分布；答案不参与向量
                embed_text=question,
                content_hash=hashlib.sha256(content.encode()).hexdigest(),
            )
        )
    if len(rows) + blank > MAX_IMPORT_ROWS:
        raise ValueError(f"CSV 超过上限 {MAX_IMPORT_ROWS} 行（当前 {len(rows) + blank} 行）")
    return rows, blank


async def import_knowledge_rows(
    f: TextIO,
    *,
    source_name: str,
    embedder: EmbeddingClient,
    tenant_id: str = "default",
    brand_id_default: str = "default",
) -> ImportReport:
    """导入回复模板（文本流）：同 content_hash 跳过（幂等），新行批量 embed 后落库"""
    rows, blank = _parse_rows(f, brand_id_default)
    # 版本以 embedder 自身为准（Fake 记 fake-sha256），保证入库版本与实际向量来源一致
    embedding_version = embedder.version

    async with get_session_factory()() as session:
        # 幂等：已存在的 content_hash 直接 skip，不重复扣 embedding 费
        hashes = [r.content_hash for r in rows]
        existing = set(
            (
                await session.execute(
                    select(KnowledgeChunk.content_hash).where(
                        KnowledgeChunk.tenant_id == tenant_id,
                        KnowledgeChunk.content_hash.in_(hashes),
                    )
                )
            ).scalars()
        )
        # CSV 内部重复也去重（保留首条）
        seen: set[str] = set()
        new_rows: list[_Row] = []
        skipped = 0
        for row in rows:
            if row.content_hash in existing or row.content_hash in seen:
                skipped += 1
                continue
            seen.add(row.content_hash)
            new_rows.append(row)

        # ≤100 条一批调用 embeddings，按顺序对齐（非对称：只 embed 问题）
        embeddings: list[list[float]] = []
        for i in range(0, len(new_rows), _EMBED_BATCH_SIZE):
            batch = new_rows[i : i + _EMBED_BATCH_SIZE]
            embeddings.extend(await embedder.embed([r.embed_text for r in batch]))

        for row, embedding in zip(new_rows, embeddings, strict=True):
            doc = KnowledgeDocument(
                tenant_id=tenant_id,
                brand_id=row.brand_id,
                platform=row.platform,
                category=row.category,
                question=row.question,
                reply=row.reply,
                source_file=source_name,
            )
            session.add(doc)
            await session.flush()
            session.add(
                KnowledgeChunk(
                    tenant_id=tenant_id,
                    document_id=doc.id,
                    content=row.content,
                    embed_text=row.embed_text,
                    content_hash=row.content_hash,
                    embedding_version=embedding_version,
                    embedding=embedding,
                )
            )
        await session.commit()

    return ImportReport(
        inserted=len(new_rows),
        skipped=skipped,
        blank=blank,
        total=len(rows) + blank,
    )


async def import_knowledge_csv(
    path: Path | str,
    *,
    embedder: EmbeddingClient,
    tenant_id: str = "default",
    brand_id_default: str = "default",
) -> ImportReport:
    """导入回复模板 CSV 文件：打开路径后委托 import_knowledge_rows"""
    path = Path(path)
    with path.open(encoding="utf-8-sig", newline="") as f:
        return await import_knowledge_rows(
            f,
            source_name=path.name,
            embedder=embedder,
            tenant_id=tenant_id,
            brand_id_default=brand_id_default,
        )
