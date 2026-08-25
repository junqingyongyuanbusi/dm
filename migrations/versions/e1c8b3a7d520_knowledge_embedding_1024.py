"""add a 1024-dimension vector column so embedding models can be switched safely

Revision ID: e1c8b3a7d520
Revises: c3e7a9f1b204
Create Date: 2026-08-24
"""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision = "e1c8b3a7d520"
down_revision = "c3e7a9f1b204"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # pgvector 的列维度固定，换 embedding 模型只能另开一列。两列并存让切换可回滚：
    # 回填 embedding_1024 期间旧的 1536 检索照常服务，切换只改配置不动数据。
    op.add_column(
        "knowledge_chunks",
        sa.Column("embedding_1024", Vector(1024), nullable=True),
    )
    # 部分索引：只给已回填的行建，未回填期间不浪费索引空间。
    op.create_index(
        "ix_knowledge_chunks_embedding_1024",
        "knowledge_chunks",
        ["embedding_1024"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding_1024": "vector_cosine_ops"},
        postgresql_where=sa.text("embedding_1024 IS NOT NULL"),
    )
    # 切换到 1024 维模型后新导入的行不该被迫再算一份 1536 向量，故放开 NOT NULL；
    # 两列同时为空的行没有检索价值，改由 CHECK 兜住。
    op.alter_column("knowledge_chunks", "embedding", nullable=True)
    op.create_check_constraint(
        "ck_knowledge_chunks_embedding_present",
        "knowledge_chunks",
        "embedding IS NOT NULL OR embedding_1024 IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_knowledge_chunks_embedding_present", "knowledge_chunks", type_="check"
    )
    # 回滚前必须确认没有仅有 1024 向量的行，否则 NOT NULL 会失败——这正是期望的
    # 行为：先用 reembed_knowledge 把 1536 向量补回来，再回滚。
    op.alter_column("knowledge_chunks", "embedding", nullable=False)
    op.drop_index("ix_knowledge_chunks_embedding_1024", table_name="knowledge_chunks")
    op.drop_column("knowledge_chunks", "embedding_1024")
