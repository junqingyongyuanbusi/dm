"""知识库表（documents/chunks）集成测试"""

import pytest
from sqlalchemy.exc import IntegrityError

from social_reply.infrastructure.database.models import KnowledgeChunk, KnowledgeDocument


async def test_insert_document_and_chunk(session):
    doc = KnowledgeDocument(question="发货多久？", reply="一般 48 小时内发货")
    session.add(doc)
    await session.flush()

    chunk = KnowledgeChunk(
        document_id=doc.id,
        content="发货多久？\n一般 48 小时内发货",
        content_hash="a" * 64,
        embedding_version="text-embedding-3-small",
        embedding=[0.0] * 1536,
    )
    session.add(chunk)
    await session.flush()

    assert doc.brand_id == "default"
    assert doc.status == "draft"
    assert doc.is_official_contact is False
    assert chunk.id is not None


async def test_unknown_knowledge_status_is_rejected(session):
    session.add(KnowledgeDocument(question="q", reply="r", status="unknown"))
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_duplicate_content_hash_rejected(session):
    doc = KnowledgeDocument(question="q", reply="r")
    session.add(doc)
    await session.flush()

    def make_chunk() -> KnowledgeChunk:
        return KnowledgeChunk(
            document_id=doc.id,
            content="q\nr",
            content_hash="b" * 64,
            embedding_version="text-embedding-3-small",
            embedding=[0.0] * 1536,
        )

    session.add(make_chunk())
    await session.flush()
    session.add(make_chunk())
    with pytest.raises(IntegrityError):
        await session.flush()
