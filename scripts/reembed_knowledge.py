"""一次性：按非对称嵌入重算已有知识库向量（只 embed question）。

背景：早期导入 embed 的是「问+答」合并文本，稀释了与用户 query 的匹配。
本脚本对现有 knowledge_documents 逐条用 question 重新生成向量，并回填
knowledge_chunks.embed_text + embedding。content/content_hash 不变（幂等键不动）。

用法（对生产公网库执行，embedder 走环境里的 OpenAI/OpenRouter 配置）：
    DATABASE_URL=<public-url> OPENAI_API_KEY=... OPENAI_BASE_URL=... \
        uv run python scripts/reembed_knowledge.py
"""

import asyncio
import logging

from sqlalchemy import select

from social_reply.application.reply_decision.runner import _get_embedder
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.database.models import KnowledgeChunk, KnowledgeDocument

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("reembed")

_BATCH = 100


async def main() -> None:
    embedder = _get_embedder()
    version = embedder.version
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(KnowledgeChunk.id, KnowledgeDocument.question)
                .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
                .order_by(KnowledgeChunk.id)
            )
        ).all()
        logger.info("待重嵌 chunks: %d，embedding_version=%s", len(rows), version)

        updated = 0
        for i in range(0, len(rows), _BATCH):
            batch = rows[i : i + _BATCH]
            questions = [r.question for r in batch]
            vectors = await embedder.embed(questions)
            for (chunk_id, _q), vec in zip(batch, vectors, strict=True):
                chunk = await session.get(KnowledgeChunk, chunk_id)
                chunk.embed_text = _q
                chunk.embedding = vec
                chunk.embedding_version = version
                updated += 1
            await session.commit()
            logger.info("已重嵌 %d/%d", min(i + _BATCH, len(rows)), len(rows))

    logger.info("完成，共更新 %d 条", updated)


if __name__ == "__main__":
    asyncio.run(main())
