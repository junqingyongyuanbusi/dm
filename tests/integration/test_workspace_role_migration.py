import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from tests.integration.migration_support import assert_alembic_succeeds, temporary_database

pytestmark = pytest.mark.integration


async def test_workspace_upgrade_snapshots_only_existing_same_tenant_support_grants():
    async with temporary_database("social_reply_workspace_roles") as database_url:
        await assert_alembic_succeeds(database_url, "upgrade", "b9e5f3a7d102")
        staff_id, other_staff_id, admin_id, account_id = [uuid.uuid4() for _ in range(4)]
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                for user_id, tenant_id, role in (
                    (staff_id, "default", "USER"),
                    (other_staff_id, "other", "USER"),
                    (admin_id, "default", "WORKSPACE_ADMIN"),
                ):
                    await connection.execute(
                        text("""
                        INSERT INTO admin_users
                            (id, username, password_hash, tenant_id, role,
                             must_change_password, status)
                        VALUES (:id, :username, 'not-used', :tenant, :role, false, 'active')
                    """),
                        {
                            "id": user_id,
                            "username": str(user_id),
                            "tenant": tenant_id,
                            "role": role,
                        },
                    )
                await connection.execute(
                    text("""
                    INSERT INTO platform_accounts
                        (id, tenant_id, brand_id, platform, name, config, capability,
                         config_version, automation_default, status, shared_with_support)
                    VALUES (:id, 'default', 'default', 'telegram', 'Migration fixture', '{}', '{}',
                            1, 'BOT_DRAFT_ONLY', 'active', true)
                """),
                    {"id": account_id},
                )
        finally:
            await engine.dispose()
        await assert_alembic_succeeds(database_url, "upgrade", "c6f2a9d4e810")
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                grants = (
                    await connection.execute(
                        text("""
                    SELECT user_id, platform_account_id, active FROM account_access_grants
                """)
                    )
                ).all()
                assert grants == [(staff_id, account_id, True)]
                roles = dict(
                    (await connection.execute(text("SELECT id, role FROM admin_users"))).all()
                )
                assert roles == {
                    staff_id: "AGENT",
                    other_staff_id: "AGENT",
                    admin_id: "WORKSPACE_ADMIN",
                }
                flags = (
                    await connection.execute(
                        text("""
                    SELECT operator_reply_enabled, operator_takeover_enabled FROM admin_users
                """)
                    )
                ).all()
                assert flags == [(False, False)] * 3
                await connection.execute(
                    text("""
                    INSERT INTO admin_users
                        (id, username, password_hash, tenant_id, must_change_password, status)
                    VALUES (:id, 'new-after-migration', 'not-used', 'default', false, 'active')
                """),
                    {"id": uuid.uuid4()},
                )
                assert (
                    await connection.scalar(text("SELECT count(*) FROM account_access_grants")) == 1
                )
                assert (
                    await connection.scalar(
                        text("""
                    SELECT role FROM admin_users WHERE username = 'new-after-migration'
                """)
                    )
                    == "AGENT"
                )
        finally:
            await engine.dispose()
