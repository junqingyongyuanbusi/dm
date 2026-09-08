import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from tests.integration.migration_support import (
    assert_alembic_succeeds,
    run_alembic,
    temporary_database,
)

pytestmark = pytest.mark.integration

_PREVIOUS_REVISION = "b9d5e2f7c314"
_OWNERSHIP_REVISION = "c8f1a4d7e203"
_HEAD_REVISION = "b9e5f3a7d102"


async def test_role_and_account_ownership_migration_enforces_tenant_scope():
    async with temporary_database("social_reply_user_ownership") as database_url:
        await assert_alembic_succeeds(database_url, "upgrade", _PREVIOUS_REVISION)

        existing_user_id = uuid.uuid4()
        engine = create_async_engine(database_url)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO admin_users ("
                    "id, username, password_hash, tenant_id, must_change_password, status) "
                    "VALUES (:id, 'existing-admin', 'hash', 'tenant-a', false, 'active')"
                ),
                {"id": existing_user_id},
            )
        await engine.dispose()

        await assert_alembic_succeeds(database_url, "upgrade", _OWNERSHIP_REVISION)

        second_user_id = uuid.uuid4()
        other_tenant_user_id = uuid.uuid4()
        account_id = uuid.uuid4()
        job_id = uuid.uuid4()
        engine = create_async_engine(database_url)
        async with engine.begin() as connection:
            existing_role = (
                await connection.execute(
                    text("SELECT role FROM admin_users WHERE id = :id"),
                    {"id": existing_user_id},
                )
            ).scalar_one()
            assert existing_role == "ADMIN"

            await connection.execute(
                text(
                    "INSERT INTO admin_users ("
                    "id, username, password_hash, tenant_id, role, "
                    "must_change_password, status) VALUES ("
                    ":id, 'tenant-a-user', 'hash', 'tenant-a', 'USER', false, 'active')"
                ),
                {"id": second_user_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO admin_users ("
                    "id, username, password_hash, tenant_id, role, "
                    "must_change_password, status) VALUES ("
                    ":id, 'tenant-b-user', 'hash', 'tenant-b', 'USER', false, 'active')"
                ),
                {"id": other_tenant_user_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO platform_accounts ("
                    "id, tenant_id, brand_id, platform, owner_user_id, name, config, "
                    "capability, config_version, automation_default, status) VALUES ("
                    ":id, 'tenant-a', 'default', 'telegram', :owner_user_id, "
                    "'Owned account', '{}'::jsonb, '{}'::jsonb, 1, "
                    "'BOT_DRAFT_ONLY', 'active')"
                ),
                {"id": account_id, "owner_user_id": second_user_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO provisioning_jobs ("
                    "id, tenant_id, brand_id, platform, operation, actor, owner_user_id, "
                    "idempotency_key, request, staging_secret_ref, status, current_step, "
                    "attempt_count, result) VALUES ("
                    ":id, 'tenant-a', 'default', 'telegram', 'CONNECT_ACCOUNT', "
                    "'user:tenant-a-user', :owner_user_id, 'owned-job', '{}'::jsonb, '', "
                    "'PENDING', 'QUEUED', 0, '{}'::jsonb)"
                ),
                {"id": job_id, "owner_user_id": second_user_id},
            )

        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO admin_users ("
                        "id, username, password_hash, tenant_id, role, "
                        "must_change_password, status) VALUES ("
                        ":id, 'invalid-role', 'hash', 'tenant-a', 'MANAGER', false, 'active')"
                    ),
                    {"id": uuid.uuid4()},
                )

        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO platform_accounts ("
                        "id, tenant_id, brand_id, platform, owner_user_id, name, config, "
                        "capability, config_version, automation_default, status) VALUES ("
                        ":id, 'tenant-a', 'default', 'telegram', :owner_user_id, "
                        "'Cross-Tenant account', '{}'::jsonb, '{}'::jsonb, 1, "
                        "'BOT_DRAFT_ONLY', 'active')"
                    ),
                    {"id": uuid.uuid4(), "owner_user_id": other_tenant_user_id},
                )

        async with engine.connect() as connection:
            ownership = (
                await connection.execute(
                    text(
                        "SELECT account.owner_user_id, job.owner_user_id "
                        "FROM platform_accounts AS account "
                        "JOIN provisioning_jobs AS job ON job.id = :job_id "
                        "WHERE account.id = :account_id"
                    ),
                    {"account_id": account_id, "job_id": job_id},
                )
            ).one()
        await engine.dispose()

        assert ownership == (second_user_id, second_user_id)

        blocked = await run_alembic(database_url, "downgrade", _PREVIOUS_REVISION)
        assert blocked.returncode != 0
        assert "cannot downgrade while a Tenant has multiple users" in (
            blocked.stdout + blocked.stderr
        )

        engine = create_async_engine(database_url)
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE platform_accounts SET owner_user_id = NULL WHERE id = :id"),
                {"id": account_id},
            )
            await connection.execute(
                text("UPDATE provisioning_jobs SET owner_user_id = NULL WHERE id = :id"),
                {"id": job_id},
            )
            await connection.execute(
                text("DELETE FROM admin_users WHERE id = :id"),
                {"id": second_user_id},
            )
        await engine.dispose()

        await assert_alembic_succeeds(database_url, "downgrade", _PREVIOUS_REVISION)
        await assert_alembic_succeeds(database_url, "upgrade", "head")

        engine = create_async_engine(database_url)
        async with engine.connect() as connection:
            revision = (
                await connection.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one()
            roles = (
                await connection.execute(
                    text("SELECT role FROM admin_users ORDER BY username")
                )
            ).scalars().all()
            account_owner = (
                await connection.execute(
                    text("SELECT owner_user_id FROM platform_accounts WHERE id = :id"),
                    {"id": account_id},
                )
            ).scalar_one()
        await engine.dispose()

        assert revision == _HEAD_REVISION
        assert roles == ["USER", "USER"]
        assert account_owner is None
