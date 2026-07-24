"""make Meta webhook route ids globally unambiguous

Revision ID: d4e7f2a9b608
Revises: c5a8e2f4d901
"""

from alembic import op
import sqlalchemy as sa


revision = "d4e7f2a9b608"
down_revision = "c5a8e2f4d901"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT public_id
                FROM platform_apps
                WHERE platform_family IN ('meta', 'instagram')
                GROUP BY public_id
                HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION
                    'cross-family Meta webhook public_id collision must be resolved before upgrade';
            END IF;
        END
        $$
        """
    )
    op.create_index(
        "uq_platform_apps_meta_route_public_id",
        "platform_apps",
        ["public_id"],
        unique=True,
        postgresql_where=sa.text("platform_family IN ('meta', 'instagram')"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_platform_apps_meta_route_public_id",
        table_name="platform_apps",
    )
