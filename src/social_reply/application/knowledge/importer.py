"""Compatibility wrappers for tenant-scoped knowledge CSV commands."""

from pathlib import Path
from typing import TextIO

from social_reply.application.knowledge.commands import (
    ImportKnowledgeBatchCommand,
    KnowledgeImportReport,
    execute_import_knowledge_batch,
)
from social_reply.application.knowledge.upload import MAX_IMPORT_ROWS
from social_reply.domain.knowledge.embeddings import EmbeddingClient
from social_reply.infrastructure.database.engine import get_session_factory

ImportReport = KnowledgeImportReport

__all__ = [
    "ImportReport",
    "MAX_IMPORT_ROWS",
    "import_knowledge_csv",
    "import_knowledge_rows",
]


async def import_knowledge_rows(
    f: TextIO,
    *,
    source_name: str,
    embedder: EmbeddingClient,
    tenant_id: str = "default",
    brand_id_default: str = "default",
    actor: str = "knowledge-import",
) -> ImportReport:
    """Import one UTF-8 CSV text stream through the shared typed command."""
    async with get_session_factory()() as session:
        report = await execute_import_knowledge_batch(
            session,
            ImportKnowledgeBatchCommand(
                required_tenant_id=tenant_id,
                actor=actor,
                csv_text=f.read(),
                source_name=source_name,
                brand_id_default=brand_id_default,
            ),
            embedder=embedder,
        )
        await session.commit()
        return report


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
