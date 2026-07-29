"""normalize platform account status and capability shape

Revision ID: 92a6e3f1c4d8
Revises: f3a6c1d8e250
"""

from alembic import op
import sqlalchemy as sa


revision = "92a6e3f1c4d8"
down_revision = "f3a6c1d8e250"
branch_labels = None
depends_on = None

_PLATFORMS = ("telegram", "facebook", "instagram", "whatsapp", "x")
_STATUSES = ("active", "DISABLED")


def _first_invalid(column: str, allowed: tuple[str, ...]) -> str | None:
    bind = op.get_bind()
    return bind.execute(
        sa.text(
            f"SELECT {column} FROM platform_accounts WHERE {column} NOT IN :allowed LIMIT 1"
        ).bindparams(sa.bindparam("allowed", expanding=True)),
        {"allowed": allowed},
    ).scalar_one_or_none()


def _first_incompatible_capability():
    return (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT id FROM platform_accounts WHERE "
                "CASE platform "
                "WHEN 'telegram' THEN capability - ARRAY['dm', 'max_text_length']::text[] "
                "WHEN 'facebook' THEN capability - ARRAY['dm', 'comments', 'max_text_length']::text[] "
                "WHEN 'instagram' THEN capability - ARRAY['dm', 'comments', 'max_text_length']::text[] "
                "WHEN 'whatsapp' THEN capability - ARRAY['dm', 'session_messages', "
                "  'templates', 'max_text_length', 'quality_rating']::text[] "
                "WHEN 'x' THEN capability - ARRAY['dm', 'x_chat', 'mentions', "
                "  'max_text_length']::text[] END <> '{}'::jsonb "
                "OR EXISTS ("
                "  SELECT 1 FROM jsonb_each(capability) AS item(key, value) "
                "  WHERE item.key = ANY(ARRAY['dm', 'comments', 'session_messages', "
                "    'templates', 'x_chat', 'mentions']::text[]) "
                "  AND jsonb_typeof(item.value) <> 'boolean'"
                ") "
                "OR CASE "
                "  WHEN jsonb_typeof(capability->'max_text_length') <> 'number' THEN true "
                "  WHEN capability->>'max_text_length' !~ '^[1-9][0-9]*$' THEN true "
                "  ELSE (capability->>'max_text_length')::numeric > CASE platform "
                "    WHEN 'telegram' THEN 4096 WHEN 'facebook' THEN 2000 "
                "    WHEN 'instagram' THEN 1000 WHEN 'whatsapp' THEN 4096 "
                "    WHEN 'x' THEN 280 END "
                "END LIMIT 1"
            )
        )
        .scalar_one_or_none()
    )


def upgrade() -> None:
    op.execute(sa.text("UPDATE platform_accounts SET status = 'active' WHERE status = 'CONNECTED'"))
    invalid_platform = _first_invalid("platform", _PLATFORMS)
    if invalid_platform is not None:
        raise RuntimeError(f"unsupported platform_accounts.platform: {invalid_platform}")
    invalid_status = _first_invalid("status", _STATUSES)
    if invalid_status is not None:
        raise RuntimeError(f"unsupported platform_accounts.status: {invalid_status}")
    invalid_capability = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT id FROM platform_accounts "
                "WHERE jsonb_typeof(capability) IS DISTINCT FROM 'object' LIMIT 1"
            )
        )
        .scalar_one_or_none()
    )
    if invalid_capability is not None:
        raise RuntimeError(f"platform_accounts.capability must be an object: {invalid_capability}")

    op.execute(
        sa.text(
            "UPDATE platform_accounts SET capability = CASE platform "
            "WHEN 'telegram' THEN "
            '  \'{"dm": false, "max_text_length": 4096}\'::jsonb '
            "WHEN 'facebook' THEN "
            '  \'{"dm": false, "comments": false, "max_text_length": 2000}\'::jsonb '
            "WHEN 'instagram' THEN "
            '  \'{"dm": false, "comments": false, "max_text_length": 1000}\'::jsonb '
            "WHEN 'whatsapp' THEN "
            '  \'{"dm": false, "session_messages": false, "templates": false, '
            '    "max_text_length": 4096}\'::jsonb '
            "WHEN 'x' THEN "
            '  \'{"dm": false, "x_chat": false, "mentions": false, '
            '    "max_text_length": 280}\'::jsonb '
            "END || COALESCE(capability, '{}'::jsonb)"
        )
    )
    incompatible_capability = _first_incompatible_capability()
    if incompatible_capability is not None:
        raise RuntimeError(
            "platform_accounts.capability violates the application contract: "
            f"{incompatible_capability}"
        )
    op.create_check_constraint(
        "ck_platform_accounts_platform",
        "platform_accounts",
        "platform IN ('telegram', 'facebook', 'instagram', 'whatsapp', 'x')",
    )
    op.create_check_constraint(
        "ck_platform_accounts_status",
        "platform_accounts",
        "status IN ('active', 'DISABLED')",
    )
    op.create_check_constraint(
        "ck_platform_accounts_capability_object",
        "platform_accounts",
        "jsonb_typeof(capability) = 'object'",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_platform_accounts_capability_object",
        "platform_accounts",
        type_="check",
    )
    op.drop_constraint("ck_platform_accounts_status", "platform_accounts", type_="check")
    op.drop_constraint("ck_platform_accounts_platform", "platform_accounts", type_="check")
