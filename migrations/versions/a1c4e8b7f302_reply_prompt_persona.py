"""tenant-editable LLM persona

Revision ID: a1c4e8b7f302
Revises: d4e7f2a9b608
"""

from alembic import op
import sqlalchemy as sa


revision = "a1c4e8b7f302"
down_revision = "d4e7f2a9b608"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reply_prompts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("brand_id", sa.String(length=64), nullable=False),
        sa.Column("persona", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_by", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "brand_id"),
    )
    op.create_index(op.f("ix_reply_prompts_tenant_id"), "reply_prompts", ["tenant_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_reply_prompts_tenant_id"), table_name="reply_prompts")
    op.drop_table("reply_prompts")
