"""回复模板 CSV 导入 CLI：uv run python -m apps.cli.import_knowledge 模板.csv [--brand default]

CLI 是独立短命进程：asyncio.run 即可（engine 单例在本进程首次使用，无共享 loop 顾虑）。
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from social_reply.application.knowledge.importer import import_knowledge_csv
from social_reply.domain.knowledge.embeddings import (
    EmbeddingClient,
    FakeEmbeddingClient,
    OpenAIEmbeddingClient,
)
from social_reply.shared.config import get_settings


def _build_embedder(allow_fake: bool) -> EmbeddingClient:
    settings = get_settings()
    if settings.openai_api_key and not settings.testing:
        return OpenAIEmbeddingClient(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            model=settings.openai_embedding_model,
            timeout=settings.openai_timeout_seconds,
        )
    # 无 key/测试环境：伪向量版本记 fake-sha256，与真实向量按版本隔离绝不混检（终审 I4）。
    # 非测试环境漏配 key 时必须显式 --allow-fake，防止误导入不可用向量还以为成功。
    if not settings.testing and not allow_fake:
        print(
            "错误：未配置 OPENAI_API_KEY。真实导入请先在 .env 填写；"
            "仅试跑请加 --allow-fake（伪向量，正式检索不可用）",
            file=sys.stderr,
        )
        raise SystemExit(1)
    print("提示：使用 FakeEmbeddingClient（伪向量，version=fake-sha256，正式检索不可用）")
    return FakeEmbeddingClient()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="回复模板 CSV 导入知识库")
    parser.add_argument("csv_path", type=Path, help="CSV 文件路径（表头必需 question,reply）")
    parser.add_argument("--brand", default="default", help="默认 brand_id（CSV 未提供时使用）")
    parser.add_argument("--allow-fake", action="store_true",
                        help="无 OPENAI_API_KEY 时允许伪向量试跑（正式检索不可用）")
    args = parser.parse_args()

    if not args.csv_path.is_file():
        print(f"错误：文件不存在 {args.csv_path}", file=sys.stderr)
        raise SystemExit(1)

    report = asyncio.run(import_knowledge_csv(
        args.csv_path, embedder=_build_embedder(args.allow_fake), brand_id_default=args.brand,
    ))
    print(
        f"导入完成：新增 {report.inserted} 条 / 跳过 {report.skipped} 条重复 / "
        f"空行 {report.blank} 条 / 共 {report.total} 行"
    )


if __name__ == "__main__":
    main()
