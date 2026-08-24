"""allow Feishu platform accounts

Revision ID: e4b7c2d9a610
Revises: c2f4a6d8e901
"""

import sqlalchemy as sa
from alembic import op


revision = "e4b7c2d9a610"
down_revision = "c2f4a6d8e901"
branch_labels = None
depends_on = None

_OLD_PLATFORM_CONSTRAINT = "platform IN ('telegram', 'facebook', 'instagram', 'whatsapp', 'x')"
_FEISHU_PLATFORM_CONSTRAINT = (
    "platform IN ('telegram', 'facebook', 'instagram', 'whatsapp', 'x', 'feishu')"
)


def upgrade() -> None:
    op.drop_constraint("ck_platform_accounts_platform", "platform_accounts", type_="check")
    op.create_check_constraint(
        "ck_platform_accounts_platform",
        "platform_accounts",
        _FEISHU_PLATFORM_CONSTRAINT,
    )


def downgrade() -> None:
    feishu_account_id = (
        op.get_bind()
        .execute(sa.text("SELECT id FROM platform_accounts WHERE platform = 'feishu' LIMIT 1"))
        .scalar_one_or_none()
    )
    if feishu_account_id is not None:
        raise RuntimeError(
            f"cannot downgrade while Feishu platform accounts exist: {feishu_account_id}"
        )
    op.drop_constraint("ck_platform_accounts_platform", "platform_accounts", type_="check")
    op.create_check_constraint(
        "ck_platform_accounts_platform",
        "platform_accounts",
        _OLD_PLATFORM_CONSTRAINT,
    )
