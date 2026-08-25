"""向量列按维度选取：换 embedding 模型时绝不能把两个模型的向量混起来比。"""

import pytest

from social_reply.application.knowledge.retrieval import (
    SUPPORTED_EMBEDDING_DIMENSIONS,
    UnsupportedEmbeddingDimensions,
    chunk_embedding_values,
    embedding_column,
    embedding_columns,
)
from social_reply.infrastructure.database.models import KnowledgeChunk


def test_已登记维度映射到各自的向量列() -> None:
    assert embedding_column(1536) is KnowledgeChunk.embedding
    assert embedding_column(1024) is KnowledgeChunk.embedding_1024


def test_支持的维度集合与列映射一致() -> None:
    assert SUPPORTED_EMBEDDING_DIMENSIONS == frozenset({1536, 1024})


@pytest.mark.parametrize("dimensions", [0, 1, 768, 1023, 1025, 1537, 3072])
def test_未登记维度直接报错而不静默换列(dimensions: int) -> None:
    # 静默降级到别的列会让检索在错误的向量空间里比余弦距离，返回看似合理的错答案。
    with pytest.raises(UnsupportedEmbeddingDimensions):
        embedding_column(dimensions)


def test_每个向量列配自己的版本列() -> None:
    # 共用一个版本列会让回填新模型的过程中现役模型立刻查不到任何 chunk。
    assert embedding_columns(1536) == (
        KnowledgeChunk.embedding,
        KnowledgeChunk.embedding_version,
    )
    assert embedding_columns(1024) == (
        KnowledgeChunk.embedding_1024,
        KnowledgeChunk.embedding_1024_version,
    )


def test_写入按维度只填对应的向量列与版本列() -> None:
    values_1536 = chunk_embedding_values([0.1] * 1536, "text-embedding-3-small")
    assert set(values_1536) == {"embedding", "embedding_version"}
    assert len(values_1536["embedding"]) == 1536
    assert values_1536["embedding_version"] == "text-embedding-3-small"

    values_1024 = chunk_embedding_values([0.2] * 1024, "baai/bge-m3")
    assert set(values_1024) == {"embedding_1024", "embedding_1024_version"}
    assert len(values_1024["embedding_1024"]) == 1024
    assert values_1024["embedding_1024_version"] == "baai/bge-m3"


def test_写入未登记维度同样报错() -> None:
    with pytest.raises(UnsupportedEmbeddingDimensions):
        chunk_embedding_values([0.3] * 512, "whatever")
