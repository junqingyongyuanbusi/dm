import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from tests.integration.migration_support import (
    assert_alembic_succeeds,
    assert_upgrades_to_current_head,
    temporary_database,
)

pytestmark = pytest.mark.integration

_PREVIOUS_REVISION = "d4e9a2f6b710"
_USER_ONLY_REVISION = "f3a7c9e1b5d2"


async def test_database_admin_roles_are_converted_to_user() -> None:
    async with temporary_database("social_reply_user_only_roles") as database_url:
        await assert_alembic_succeeds(database_url, "upgrade", _PREVIOUS_REVISION)

        legacy_admin_id = uuid.uuid4()
        existing_user_id = uuid.uuid4()
        engine = create_async_engine(database_url)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO admin_users ("
                    "id, username, password_hash, tenant_id, role, "
                    "must_change_password, status) VALUES "
                    "(:legacy_admin_id, 'legacy-admin', 'hash', 'default', "
                    "'ADMIN', false, 'active'), "
                    "(:existing_user_id, 'existing-user', 'hash', 'default', "
                    "'USER', false, 'active')"
                ),
                {
                    "legacy_admin_id": legacy_admin_id,
                    "existing_user_id": existing_user_id,
                },
            )
        await engine.dispose()

        await assert_alembic_succeeds(database_url, "upgrade", _USER_ONLY_REVISION)

        engine = create_async_engine(database_url)
        async with engine.connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            roles = (
                await connection.execute(
                    text("SELECT role FROM admin_users ORDER BY username")
                )
            ).scalars().all()
        await engine.dispose()

        assert revision == _USER_ONLY_REVISION
        assert roles == ["USER", "USER"]
        await assert_upgrades_to_current_head(database_url)


async def test_database_role_constraint_allows_only_user() -> None:
    async with temporary_database("social_reply_user_only_role_check") as database_url:
        # USER-only was this historical revision's contract, not the workspace head's.
        await assert_alembic_succeeds(database_url, "upgrade", _USER_ONLY_REVISION)

        engine = create_async_engine(database_url)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO admin_users ("
                    "id, username, password_hash, tenant_id, role, "
                    "must_change_password, status) VALUES "
                    "(:id, 'valid-user', 'hash', 'default', 'USER', false, 'active')"
                ),
                {"id": uuid.uuid4()},
            )

        for forbidden_role in ("ADMIN", "SUPERADMIN"):
            with pytest.raises(IntegrityError):
                async with engine.begin() as connection:
                    await connection.execute(
                        text(
                            "INSERT INTO admin_users ("
                            "id, username, password_hash, tenant_id, role, "
                            "must_change_password, status) VALUES ("
                            ":id, :username, 'hash', 'default', :role, false, 'active')"
                        ),
                        {
                            "id": uuid.uuid4(),
                            "username": f"forbidden-{forbidden_role.lower()}",
                            "role": forbidden_role,
                        },
                    )
        await engine.dispose()
        await assert_upgrades_to_current_head(database_url)
