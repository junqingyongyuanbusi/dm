"""inline secret bundles into jsonb columns

将 secret 从 file:// 引用改为内联 JSONB 存储：
Railway 部署下 api/worker 是不同容器、文件系统隔离且无持久卷，
file:// 方案两个跨容器断点（api 验签读 / worker 读 staging）全部失效。
内联 JSONB 存进 Postgres 是无持久卷环境下跨容器共享 secret 的唯一可靠位置。

Revision ID: a3f9c2e14b78
Revises: f4a82d7c6e10
Create Date: 2026-07-17
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "a3f9c2e14b78"
down_revision = "f4a82d7c6e10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # platform_apps: credential_ref(file://) -> credential_bundle(JSONB)
    op.add_column(
        "platform_apps",
        sa.Column("credential_bundle", JSONB(), nullable=False, server_default="{}"),
    )
    op.drop_column("platform_apps", "credential_ref")

    # platform_accounts: credential_ref/webhook_secret_ref -> JSONB bundles
    op.add_column(
        "platform_accounts",
        sa.Column("credential_bundle", JSONB(), nullable=False, server_default="{}"),
    )
    op.add_column(
        "platform_accounts",
        sa.Column("webhook_secret_bundle", JSONB(), nullable=True),
    )
    op.drop_column("platform_accounts", "credential_ref")
    op.drop_column("platform_accounts", "webhook_secret_ref")

    # provisioning_jobs: staging_secret_ref(file://) -> staging_secret(JSONB, 完成后置 NULL)
    op.add_column(
        "provisioning_jobs",
        sa.Column("staging_secret", JSONB(), nullable=True),
    )
    op.drop_column("provisioning_jobs", "staging_secret_ref")


def downgrade() -> None:
    op.add_column(
        "provisioning_jobs",
        sa.Column("staging_secret_ref", sa.Text(), nullable=False, server_default=""),
    )
    op.drop_column("provisioning_jobs", "staging_secret")

    op.add_column(
        "platform_accounts",
        sa.Column("webhook_secret_ref", sa.Text(), nullable=True),
    )
    op.add_column(
        "platform_accounts",
        sa.Column("credential_ref", sa.Text(), nullable=True),
    )
    op.drop_column("platform_accounts", "webhook_secret_bundle")
    op.drop_column("platform_accounts", "credential_bundle")

    op.add_column(
        "platform_apps",
        sa.Column("credential_ref", sa.Text(), nullable=False, server_default=""),
    )
    op.drop_column("platform_apps", "credential_bundle")
