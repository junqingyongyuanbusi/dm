"""allow email platform accounts and IMAP sync state

Revision ID: e9a1c4f7b620
Revises: b7e4c2d9a615
"""

from alembic import op
import sqlalchemy as sa


revision = "e9a1c4f7b620"
down_revision = "b7e4c2d9a615"
branch_labels = None
depends_on = None

_OLD_PLATFORM_CONSTRAINT = (
    "platform IN ('telegram', 'facebook', 'instagram', 'whatsapp', 'x', 'feishu')"
)
_EMAIL_PLATFORM_CONSTRAINT = (
    "platform IN ('telegram', 'facebook', 'instagram', 'whatsapp', 'x', 'feishu', 'email')"
)
_OLD_STREAM_CONSTRAINT = "stream IN ('X_LEGACY_DM', 'XCHAT_DISCOVERY', 'XCHAT_CONVERSATION')"
_EMAIL_STREAM_CONSTRAINT = (
    "stream IN ('X_LEGACY_DM', 'XCHAT_DISCOVERY', 'XCHAT_CONVERSATION', 'EMAIL_IMAP')"
)
_OLD_SCOPE_CONSTRAINT = (
    "(stream = 'XCHAT_CONVERSATION' AND scope_key <> '') OR "
    "(stream <> 'XCHAT_CONVERSATION' AND scope_key = '')"
)
_EMAIL_SCOPE_CONSTRAINT = (
    "(stream = 'XCHAT_CONVERSATION' AND scope_key <> '') OR "
    "(stream IN ('X_LEGACY_DM', 'XCHAT_DISCOVERY', 'EMAIL_IMAP') AND scope_key = '')"
)
_OLD_GAP_TYPE_CONSTRAINT = "gap_type IN ('PAGE_CAP', 'PAGINATION_ERROR', 'DECRYPT_ERROR')"
_EMAIL_GAP_TYPE_CONSTRAINT = (
    "gap_type IN ('PAGE_CAP', 'PAGINATION_ERROR', 'DECRYPT_ERROR', 'EMAIL_UIDVALIDITY_CHANGED')"
)
_INDEX_NAME = "ix_outbox_email_bot_sent_account_time"
_INDEX_PREDICATE = (
    "status = 'SENT' AND destination_type = 'email_reply' "
    "AND origin_kind = 'DECISION' AND actor_kind = 'BOT'"
)


def upgrade() -> None:
    op.drop_constraint("ck_platform_accounts_platform", "platform_accounts", type_="check")
    op.create_check_constraint(
        "ck_platform_accounts_platform",
        "platform_accounts",
        _EMAIL_PLATFORM_CONSTRAINT,
    )

    op.drop_constraint("ck_platform_checkpoints_stream", "platform_checkpoints", type_="check")
    op.create_check_constraint(
        "ck_platform_checkpoints_stream",
        "platform_checkpoints",
        _EMAIL_STREAM_CONSTRAINT,
    )
    op.drop_constraint("ck_platform_checkpoints_scope", "platform_checkpoints", type_="check")
    op.create_check_constraint(
        "ck_platform_checkpoints_scope",
        "platform_checkpoints",
        _EMAIL_SCOPE_CONSTRAINT,
    )

    op.drop_constraint("ck_sync_gaps_type", "sync_gaps", type_="check")
    op.create_check_constraint(
        "ck_sync_gaps_type",
        "sync_gaps",
        _EMAIL_GAP_TYPE_CONSTRAINT,
    )

    with op.get_context().autocommit_block():
        op.execute(sa.text(f'DROP INDEX CONCURRENTLY IF EXISTS "{_INDEX_NAME}"'))
        op.create_index(
            _INDEX_NAME,
            "outbox_messages",
            ["platform_account_id", "sent_at", "conversation_id"],
            postgresql_where=sa.text(_INDEX_PREDICATE),
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    email_account_id = bind.execute(
        sa.text("SELECT id FROM platform_accounts WHERE platform = 'email' LIMIT 1")
    ).scalar_one_or_none()
    if email_account_id is not None:
        raise RuntimeError(
            f"cannot downgrade while email platform accounts exist: {email_account_id}"
        )

    email_checkpoint_id = bind.execute(
        sa.text("SELECT id FROM platform_checkpoints WHERE stream = 'EMAIL_IMAP' LIMIT 1")
    ).scalar_one_or_none()
    if email_checkpoint_id is not None:
        raise RuntimeError(
            f"cannot downgrade while EMAIL_IMAP checkpoints exist: {email_checkpoint_id}"
        )

    email_gap_id = bind.execute(
        sa.text("SELECT id FROM sync_gaps WHERE gap_type = 'EMAIL_UIDVALIDITY_CHANGED' LIMIT 1")
    ).scalar_one_or_none()
    if email_gap_id is not None:
        raise RuntimeError(
            f"cannot downgrade while EMAIL_UIDVALIDITY_CHANGED sync gaps exist: {email_gap_id}"
        )

    op.drop_constraint("ck_sync_gaps_type", "sync_gaps", type_="check")
    op.create_check_constraint(
        "ck_sync_gaps_type",
        "sync_gaps",
        _OLD_GAP_TYPE_CONSTRAINT,
    )

    op.drop_constraint("ck_platform_checkpoints_scope", "platform_checkpoints", type_="check")
    op.create_check_constraint(
        "ck_platform_checkpoints_scope",
        "platform_checkpoints",
        _OLD_SCOPE_CONSTRAINT,
    )
    op.drop_constraint("ck_platform_checkpoints_stream", "platform_checkpoints", type_="check")
    op.create_check_constraint(
        "ck_platform_checkpoints_stream",
        "platform_checkpoints",
        _OLD_STREAM_CONSTRAINT,
    )

    op.drop_constraint("ck_platform_accounts_platform", "platform_accounts", type_="check")
    op.create_check_constraint(
        "ck_platform_accounts_platform",
        "platform_accounts",
        _OLD_PLATFORM_CONSTRAINT,
    )

    with op.get_context().autocommit_block():
        op.execute(sa.text(f'DROP INDEX CONCURRENTLY IF EXISTS "{_INDEX_NAME}"'))
