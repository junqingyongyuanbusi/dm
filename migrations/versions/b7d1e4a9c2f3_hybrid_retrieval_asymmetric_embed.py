"""hybrid retrieval: asymmetric embed_text + question tsvector

检索质量升级：
1. 非对称嵌入——knowledge_chunks 增 embed_text 列，记录实际参与 embedding 的文本
   （只 question，不再拼答案，避免答案措辞稀释向量与用户 query 的匹配）。
2. 混合检索——knowledge_documents 增 question 的 tsvector 生成列 + GIN 索引，
   支撑词法（BM25 近似）一路，与向量 RRF 融合补关键词召回。

历史行 embed_text 留 NULL（旧向量 embed 的是 content，需重嵌才能享受非对称收益）。

Revision ID: b7d1e4a9c2f3
Revises: a3f9c2e14b78
Create Date: 2026-07-17
"""

import sqlalchemy as sa
from alembic import op

revision = "b7d1e4a9c2f3"
down_revision = "a3f9c2e14b78"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. 非对称嵌入：记录实际 embed 的文本（历史行 NULL）
    op.add_column("knowledge_chunks", sa.Column("embed_text", sa.Text(), nullable=True))

    # 2. 词法检索：question 的 tsvector 生成列（DB 自动维护）+ GIN 索引
    op.execute(
        "ALTER TABLE knowledge_documents "
        "ADD COLUMN question_tsv tsvector "
        "GENERATED ALWAYS AS (to_tsvector('simple', question)) STORED"
    )
    op.create_index(
        "ix_knowledge_documents_question_tsv",
        "knowledge_documents",
        ["question_tsv"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_knowledge_documents_question_tsv",
        table_name="knowledge_documents",
        postgresql_using="gin",
    )
    op.drop_column("knowledge_documents", "question_tsv")
    op.drop_column("knowledge_chunks", "embed_text")
