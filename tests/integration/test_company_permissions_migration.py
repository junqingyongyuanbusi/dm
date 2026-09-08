import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from tests.integration.migration_support import assert_alembic_succeeds, temporary_database

pytestmark = pytest.mark.integration


async def test_permissions_upgrade_quarantines_unknown_jobs_and_releases_legacy_work():
    async with temporary_database("social_reply_company_permissions") as database_url:
        await assert_alembic_succeeds(database_url, "upgrade", "a8f4d2c6e901")
        account_id, contact_id, conversation_id, work_id, staff_id = [
            uuid.uuid4() for _ in range(5)
        ]
        old_nonce = uuid.uuid4()
        jobs = {
            status: uuid.uuid4() for status in ("PENDING", "PROCESSING", "RUNNING", "COMPLETED")
        }
        engine = create_async_engine(database_url)
        async with engine.begin() as connection:
            await connection.execute(
                text("""
                INSERT INTO admin_users
                    (id, username, password_hash, tenant_id, role, must_change_password, status)
                VALUES (:id, 'migration-support', 'not-used', 'default', 'USER', false, 'active')
            """),
                {"id": staff_id},
            )
            await connection.execute(
                text("""
                INSERT INTO platform_accounts
                    (id, tenant_id, brand_id, platform, owner_user_id, name, config,
                     capability, config_version, automation_default, status)
                VALUES (:id, 'default', 'default', 'telegram', :owner, 'Company account',
                        '{}'::jsonb, '{}'::jsonb, 3, 'BOT_DRAFT_ONLY', 'active')
            """),
                {"id": account_id, "owner": staff_id},
            )
            await connection.execute(
                text("""
                INSERT INTO contacts
                    (id, tenant_id, platform, platform_account_id, external_user_id)
                VALUES (:id, 'default', 'telegram', :account, 'customer')
            """),
                {"id": contact_id, "account": account_id},
            )
            await connection.execute(
                text("""
                INSERT INTO conversations
                    (id, tenant_id, brand_id, platform, platform_account_id, contact_id,
                     conversation_key, channel_type)
                VALUES (:id, 'default', 'default', 'telegram', :account, :contact, 'legacy', 'dm')
            """),
                {"id": conversation_id, "account": account_id, "contact": contact_id},
            )
            await connection.execute(
                text("""
                INSERT INTO automation_states
                    (conversation_id, state, state_version, human_agent_id, resume_policy)
                VALUES (:id, 'HUMAN_ACTIVE', 4, 'user:old-bootstrap', 'MANUAL')
            """),
                {"id": conversation_id},
            )
            await connection.execute(
                text("""
                INSERT INTO human_work_items
                    (id, tenant_id, conversation_id, status, reason_code, priority,
                     assigned_actor, assigned_user_id, claimed_at, version)
                VALUES (:id, 'default', :conversation, 'CLAIMED', 'HUMAN_REQUEST', 0,
                        'user:old-bootstrap', NULL, now(), 5)
            """),
                {"id": work_id, "conversation": conversation_id},
            )
            await connection.execute(
                text("""
                INSERT INTO handoff_notification_intents
                    (id, public_id, tenant_id, human_work_item_id, conversation_id, provider_uuid,
                     status, desired_card_state, desired_revision, delivered_revision,
                     action_nonce, attempt_count)
                VALUES (:id, :public_id, 'default', :work, :conversation, :provider_uuid,
                        'BLOCKED_CONFIG', 'CLAIMED', 2, 2, :nonce, 0)
            """),
                {
                    "id": uuid.uuid4(),
                    "public_id": uuid.uuid4(),
                    "work": work_id,
                    "conversation": conversation_id,
                    "provider_uuid": uuid.uuid4(),
                    "nonce": old_nonce,
                },
            )
            for status, job_id in jobs.items():
                await connection.execute(
                    text("""
                    INSERT INTO provisioning_jobs
                        (id, tenant_id, brand_id, platform, operation, actor, owner_user_id,
                         idempotency_key, request, staging_secret_ref, staging_secret,
                         status, current_step, attempt_count, result, locked_by, locked_at)
                    VALUES (:id, 'default', 'default', 'telegram',
                            'CONNECT_ACCOUNT', :actor, :owner,
                            :key, '{}'::jsonb, '', '{"opaque":"old-envelope"}'::jsonb,
                            :status, 'CONNECTING', 2, '{"prior_result":"retained"}'::jsonb,
                            'old-worker', now())
                """),
                    {
                        "id": job_id,
                        "key": str(job_id),
                        "status": status,
                        "owner": staff_id if status == "PENDING" else None,
                        "actor": "user:migration-support"
                        if status == "PENDING"
                        else "user:old-bootstrap",
                    },
                )
        await engine.dispose()
        await assert_alembic_succeeds(database_url, "upgrade", "b9e5f3a7d102")
        engine = create_async_engine(database_url)
        async with engine.connect() as connection:
            account = (
                await connection.execute(
                    text("""
                SELECT owner_user_id, shared_with_support, automation_default
                FROM platform_accounts WHERE id = :id
            """),
                    {"id": account_id},
                )
            ).one()
            assert account == (staff_id, False, "BOT_DRAFT_ONLY")
            work = (
                await connection.execute(
                    text("""
                SELECT status, assigned_actor, assigned_user_id, claimed_at, version
                FROM human_work_items WHERE id = :id
            """),
                    {"id": work_id},
                )
            ).one()
            assert work == ("WAITING", None, None, None, 6)
            state = (
                await connection.execute(
                    text("""
                SELECT state, state_version, human_agent_id FROM automation_states
                WHERE conversation_id = :id
            """),
                    {"id": conversation_id},
                )
            ).one()
            assert state == ("HANDOFF_PENDING", 5, None)
            card = (
                await connection.execute(
                    text("""
                SELECT desired_card_state, desired_revision, action_nonce
                FROM handoff_notification_intents WHERE human_work_item_id = :id
            """),
                    {"id": work_id},
                )
            ).one()
            assert card[:2] == ("WAITING", 3)
            assert card[2] != old_nonce
            for old_status, job_id in jobs.items():
                job = (
                    (
                        await connection.execute(
                            text("""
                    SELECT status, attempt_count, authority_kind, authority_version,
                           staging_secret, locked_at, locked_by, result
                    FROM provisioning_jobs WHERE id = :id
                """),
                            {"id": job_id},
                        )
                    )
                    .mappings()
                    .one()
                )
                if old_status == "COMPLETED":
                    assert job["status"] == "COMPLETED"
                    assert job["attempt_count"] == 2
                    assert job["result"] == {"prior_result": "retained"}
                else:
                    assert job["status"] == "NEEDS_ACTION"
                    assert job["attempt_count"] == 3
                    assert job["authority_kind"] == "UNVERIFIED"
                    assert job["authority_version"] == 0
                    assert job["staging_secret"] is None
                    assert job["locked_at"] is None
                    assert job["locked_by"] is None
                    assert job["result"]["prior_result"] == "retained"
                    assert job["result"]["authority_quarantined"] is True
            assert (
                await connection.execute(
                    text("""
                SELECT count(*) FROM audit_logs WHERE action = 'RELEASE_LEGACY_HUMAN_WORK'
                AND subject_id = :id
            """),
                    {"id": str(work_id)},
                )
            ).scalar_one() == 1
        await engine.dispose()
