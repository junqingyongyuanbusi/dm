"""按当前 OPENAI_EMBEDDING_MODEL 重算 knowledge_chunks 的向量（换 embedding 模型用）。

  # 试跑：只报告要回填多少行，不写库、不调 API
  uv run python -m apps.cli.reembed_knowledge --dry-run

  # 用当前配置的模型回填（模型/维度由 OPENAI_EMBEDDING_MODEL / OPENAI_EMBEDDING_DIMENSIONS 决定）
  OPENAI_EMBEDDING_MODEL=baai/bge-m3 OPENAI_EMBEDDING_DIMENSIONS=1024 \
    uv run python -m apps.cli.reembed_knowledge --tenant default

切换 embedding 模型的正确顺序：
  1. alembic upgrade head            —— 建好目标维度的向量列
  2. 本脚本回填                       —— 旧列与旧 embedding_version 不动，线上检索照常
  3. 改 OPENAI_EMBEDDING_MODEL / OPENAI_EMBEDDING_DIMENSIONS 并重启   —— 检索切到新列
回滚就是把第 3 步的两个变量改回去；数据仍在，无需再回填。

CLI 是独立短命进程：asyncio.run 即可。
"""

import argparse
import asyncio
import sys

from sqlalchemy import func, select, update

from social_reply.application.knowledge.retrieval import (
    UnsupportedEmbeddingDimensions,
    embedding_column,
)
from social_reply.domain.knowledge.embeddings import EmbeddingClient, OpenAIEmbeddingClient
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.database.models import KnowledgeChunk
from social_reply.shared.config import get_settings

_BATCH = 100  # 单次 embeddings 请求上限，与 importer 保持一致


def _build_embedder() -> EmbeddingClient:
    settings = get_settings()
    api_key = settings.openai_api_key.get_secret_value()
    if not api_key:
        print("错误：未配置 OPENAI_API_KEY，无法重算向量。", file=sys.stderr)
        raise SystemExit(1)
    return OpenAIEmbeddingClient(
        api_key=api_key,
        base_url=settings.openai_base_url,
        model=settings.openai_embedding_model,
        timeout=settings.openai_timeout_seconds,
        expected_dimensions=settings.openai_embedding_dimensions,
    )


async def _run(tenant_id: str, dry_run: bool, limit: int | None) -> int:
    settings = get_settings()
    dimensions = settings.openai_embedding_dimensions
    version = settings.openai_embedding_model
    try:
        column = embedding_column(dimensions)
    except UnsupportedEmbeddingDimensions as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    factory = get_session_factory()
    # 待回填 = 该维度的列还是空，或 embedding_version 还不是目标模型。
    # 两个条件缺一不可：换回旧模型再换回来时，列有值但版本已被改写过。
    pending = (column.is_(None)) | (KnowledgeChunk.embedding_version != version)
    async with factory() as session:
        total = await session.scalar(
            select(func.count())
            .select_from(KnowledgeChunk)
            .where(KnowledgeChunk.tenant_id == tenant_id, pending)
        )
    total = int(total or 0)
    print(f"模型={version} 维度={dimensions} 目标列={column.key}")
    print(f"tenant={tenant_id} 待回填 {total} 行")
    if dry_run:
        print("--dry-run：未调用 API、未写库")
        return 0
    if total == 0:
        return 0

    embedder = _build_embedder()
    done = 0
    try:
        while True:
            async with factory() as session:
                rows = (
                    (
                        await session.execute(
                            select(
                                KnowledgeChunk.id,
                                KnowledgeChunk.embed_text,
                                KnowledgeChunk.content,
                            )
                            .where(KnowledgeChunk.tenant_id == tenant_id, pending)
                            .order_by(KnowledgeChunk.id)
                            .limit(_BATCH)
                        )
                    )
                    .all()
                )
                if not rows:
                    break
                # embed_text 为空的历史行回落到 content——与 importer 的非对称检索约定一致：
                # 有 embed_text 就只 embed question，避免 answer 措辞稀释向量。
                texts = [row.embed_text or row.content for row in rows]
                vectors = await embedder.embed(texts)
                for row, vector in zip(rows, vectors, strict=True):
                    await session.execute(
                        update(KnowledgeChunk)
                        .where(KnowledgeChunk.id == row.id)
                        .values({column.key: vector, "embedding_version": version})
                    )
                await session.commit()
            done += len(rows)
            print(f"  {done}/{total}")
            if limit is not None and done >= limit:
                print(f"达到 --limit {limit}，提前停止")
                break
    finally:
        close = getattr(embedder, "aclose", None)
        if close is not None:
            await close()
    print(f"完成：回填 {done} 行到 {column.key}，embedding_version={version}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="按当前配置的模型重算知识库向量")
    parser.add_argument("--tenant", default="default")
    parser.add_argument("--dry-run", action="store_true", help="只统计待回填行数")
    parser.add_argument("--limit", type=int, default=None, help="最多回填多少行（分批验证用）")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args.tenant, args.dry_run, args.limit)))


if __name__ == "__main__":
    main()
