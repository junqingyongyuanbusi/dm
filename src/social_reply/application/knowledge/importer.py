"""Trusted local-system CSV import facade; HTTP paths must not call it."""

from __future__ import annotations

import io
from pathlib import Path
from typing import TextIO

from social_reply.application.knowledge.authorization import _trusted_system_import_capability
from social_reply.application.knowledge.commands import (
    ImportKnowledgeBatchCommand,
    KnowledgeAuthorizationError,
    KnowledgeImportReport,
    KnowledgeValidationError,
    execute_import_knowledge_batch,
)
from social_reply.application.knowledge.upload import MAX_IMPORT_ROWS, MAX_KNOWLEDGE_UPLOAD_BYTES
from social_reply.domain.knowledge.embeddings import EmbeddingClient
from social_reply.infrastructure.database.engine import get_session_factory

ImportReport = KnowledgeImportReport
_CSV_READ_CHUNK_CHARACTERS = 64 * 1024

__all__ = [
    "ImportReport",
    "MAX_IMPORT_ROWS",
]


def _read_bounded_csv(stream: TextIO) -> str:
    total_bytes = 0
    with io.StringIO() as buffer:
        while True:
            read_size = min(
                _CSV_READ_CHUNK_CHARACTERS,
                MAX_KNOWLEDGE_UPLOAD_BYTES - total_bytes + 1,
            )
            chunk = stream.read(read_size)
            if not chunk:
                return buffer.getvalue()
            total_bytes += len(chunk.encode("utf-8"))
            if total_bytes > MAX_KNOWLEDGE_UPLOAD_BYTES:
                raise KnowledgeValidationError("knowledge_csv_too_large")
            # A nonempty short read is not EOF; keep reading within the remaining budget.
            buffer.write(chunk)


async def _import_knowledge_rows_system(
    f: TextIO,
    *,
    source_name: str,
    embedder: EmbeddingClient,
    tenant_id: str = "default",
    brand_id_default: str = "default",
) -> ImportReport:
    """Import a UTF-8 CSV stream through the trusted system capability facade."""
    csv_text = _read_bounded_csv(f)
    async with get_session_factory()() as session:
        report = await execute_import_knowledge_batch(
            session,
            ImportKnowledgeBatchCommand(
                required_tenant_id=tenant_id,
                principal=None,
                system_import_capability=_trusted_system_import_capability(),
                csv_text=csv_text,
                source_name=source_name,
                brand_id_default=brand_id_default,
            ),
            embedder=embedder,
        )
        await session.commit()
        return report


async def _import_knowledge_csv_system(
    path: Path | str,
    *,
    embedder: EmbeddingClient,
    tenant_id: str = "default",
    brand_id_default: str = "default",
) -> ImportReport:
    """Import a CSV path through the trusted local-system facade."""
    path = Path(path)
    with path.open(encoding="utf-8-sig", newline="") as f:
        return await _import_knowledge_rows_system(
            f,
            source_name=path.name,
            embedder=embedder,
            tenant_id=tenant_id,
            brand_id_default=brand_id_default,
        )


async def import_knowledge_rows(*_args: object, **_kwargs: object):
    """Reject ambient callers; use the private CLI facade instead."""

    raise KnowledgeAuthorizationError("knowledge_system_import_facade_private")


async def import_knowledge_csv(*_args: object, **_kwargs: object):
    """Reject ambient callers; use the private CLI facade instead."""

    raise KnowledgeAuthorizationError("knowledge_system_import_facade_private")
