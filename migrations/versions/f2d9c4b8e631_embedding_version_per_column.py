"""give each embedding column its own version so backfilling a model is invisible to the live one

Revision ID: f2d9c4b8e631
Revises: e1c8b3a7d520
Create Date: 2026-08-25
"""

import sqlalchemy as sa
from alembic import op

revision = "f2d9c4b8e631"
down_revision = "e1c8b3a7d520"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 一行现在可以同时持有两个模型的向量，单个 embedding_version 描述不了两个向量。
    # 共用一列会让回填新模型的过程中现役模型立刻查不到任何 chunk（检索按
    # embedding_version 过滤），两列并存的可回滚切换也就失去意义。
    op.add_column(
        "knowledge_chunks",
        sa.Column("embedding_1024_version", sa.String(length=32), nullable=True),
    )
    # 切到 1024 维模型后新导入的行不该被迫给 1536 列填一个无关版本。
    op.alter_column("knowledge_chunks", "embedding_version", nullable=True)
    op.create_check_constraint(
        "ck_knowledge_chunks_embedding_version_pairing",
        "knowledge_chunks",
        "(embedding IS NULL) = (embedding_version IS NULL) AND "
        "(embedding_1024 IS NULL) = (embedding_1024_version IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_knowledge_chunks_embedding_version_pairing",
        "knowledge_chunks",
        type_="check",
    )
    # 回滚前必须确认没有仅有 1024 向量的行，否则 NOT NULL 会失败——这正是期望的
    # 行为：先用 reembed_knowledge 把 1536 向量补回来，再回滚。
    op.alter_column("knowledge_chunks", "embedding_version", nullable=False)
    op.drop_column("knowledge_chunks", "embedding_1024_version")
