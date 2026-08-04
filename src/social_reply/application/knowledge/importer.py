"""回复模板 CSV 导入：content_hash 幂等 + 批量 embedding"""

import csv
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from social_reply.application.knowledge.drafts import (
    KnowledgeDraft,
    build_knowledge_draft,
    existing_content_hashes,
    persist_knowledge_draft,
)
from social_reply.domain.knowledge.embeddings import EmbeddingClient
from social_reply.infrastructure.database.engine import get_session_factory

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


def parse_optional_bool(value: str | None) -> bool:
    """Parse an optional strict CSV boolean; blank values are false."""
    normalized = (value or "").strip().casefold()
    if normalized in {"", "false", "0", "no"}:
        return False
    if normalized in {"true", "1", "yes"}:
        return True
    raise ValueError(f"is_official_contact must be true/false, got {value!r}")


def _parse_rows(
    f: TextIO,
    *,
    tenant_id: str,
    brand_id_default: str,
    source_name: str,
) -> tuple[list[KnowledgeDraft], int]:
    """解析 CSV，返回 (有效行, 空行数)；表头缺失或超行数抛 ValueError"""
    reader = csv.DictReader(f)
    headers = set(reader.fieldnames or [])
    missing = _REQUIRED_HEADERS - headers
    if missing:
        raise ValueError(f"CSV 表头缺少必需列: {'、'.join(sorted(missing))}（必需 question,reply）")
    rows: list[KnowledgeDraft] = []
    blank = 0
    for raw in reader:
        question = (raw.get("question") or "").strip()
        reply = (raw.get("reply") or "").strip()
        if not question or not reply:
            blank += 1
            logger.warning("跳过空行（question/reply 为空）: %r", raw)
            continue
        rows.append(
            build_knowledge_draft(
                tenant_id=tenant_id,
                question=question,
                reply=reply,
                brand_id=(raw.get("brand_id") or "").strip() or brand_id_default,
                platform=(raw.get("platform") or "").strip() or None,
                category=(raw.get("category") or "").strip() or None,
                is_official_contact=parse_optional_bool(raw.get("is_official_contact")),
                source_file=source_name,
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
    actor: str = "knowledge-import",
) -> ImportReport:
    """导入回复模板（文本流）：同 content_hash 跳过（幂等），新行批量 embed 后落库"""
    rows, blank = _parse_rows(
        f,
        tenant_id=tenant_id,
        brand_id_default=brand_id_default,
        source_name=source_name,
    )
    # 版本以 embedder 自身为准（Fake 记 fake-sha256），保证入库版本与实际向量来源一致
    embedding_version = embedder.version

    async with get_session_factory()() as session:
        # 幂等：已存在的 content_hash 直接 skip，不重复扣 embedding 费
        hashes = [row.content_hash for row in rows]
        existing = await existing_content_hashes(
            session, tenant_id=tenant_id, content_hashes=hashes
        )
        # CSV 内部重复也去重（保留首条）
        seen: set[str] = set()
        new_rows: list[KnowledgeDraft] = []
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
            await persist_knowledge_draft(
                session,
                row,
                embedding_version=embedding_version,
                embedding=embedding,
                actor=actor,
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
    actor: str = "knowledge-import",
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
            actor=actor,
        )
