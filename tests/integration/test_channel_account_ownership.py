import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select

from social_reply.application.account_management import jobs, provisioning
from social_reply.application.account_management.auth import (
    authenticate,
    hash_password,
    issue_session,
    principal_from_session_id,
)
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory

pytestmark = pytest.mark.integration


async def _seed_users() -> tuple[uuid.UUID, uuid.UUID]:
    first_user_id = uuid.uuid4()
    second_user_id = uuid.uuid4()
    password_hash = await hash_password("channel-owner-password-123")
    async with get_session_factory()() as session:
        session.add_all(
            [
                models.AdminUser(
                    id=first_user_id,
                    username="channel-owner-a",
                    password_hash=password_hash,
                    tenant_id="tenant-a",
                    role="USER",
                    must_change_password=False,
                    status="active",
                ),
                models.AdminUser(
                    id=second_user_id,
                    username="channel-owner-b",
                    password_hash=password_hash,
                    tenant_id="tenant-a",
                    role="USER",
                    must_change_password=False,
                    status="active",
                ),
            ]
        )
        await session.commit()
    return first_user_id, second_user_id


async def _provision_account(
    *,
    owner_user_id: uuid.UUID | None,
    name: str,
    provider_username: str | None = None,
    avatar_url: str | None = None,
) -> tuple[uuid.UUID, str]:
    credentials = {"bot_token": f"token-for-{name}"}
    async with get_session_factory()() as session:
        user = await session.get(models.AdminUser, owner_user_id)
    assert user is not None
    authenticated = await authenticate(user.username, "channel-owner-password-123")
    assert authenticated is not None
    principal, _token = authenticated
    job_id = await jobs.submit_provisioning_job(
        tenant_id="tenant-a",
        brand_id="default",
        platform="telegram",
        actor=principal.actor,
        request={"name": name, "idempotency_key": f"ownership-{uuid.uuid4()}"},
        secrets=credentials,
        admin_session_id=principal.session_id,
    )
    claimed = await jobs._claim_job(job_id)
    assert claimed is not None
    assert claimed is not None
    await provisioning.bind_provisioning_external_identity(
        provisioning_job_id=job_id,
        provisioning_attempt_count=claimed.attempt_count,
        platform="telegram",
        external_account_id="shared-external-account",
        credential_bundle=credentials,
        platform_app_id=None,
    )
    return await provisioning.provision_direct_account(
        platform="telegram",
        external_account_id="shared-external-account",
        tenant_id="tenant-a",
        brand_id="default",
        name=name,
        public_id=None,
        public_id_prefix="tg",
        secrets_root=Path(".secrets/accounts"),
        credential_bundle=credentials,
        webhook_secret_bundle={"secret": f"webhook-for-{name}"},
        config={"api_base_url": "https://api.telegram.org"},
        capability={"dm": True, "max_text_length": 4096},
        automation_default="BOT_DRAFT_ONLY",
        owner_user_id=owner_user_id,
        provider_username=provider_username,
        avatar_url=avatar_url,
        profile_updated_at=datetime.now(UTC),
        authority_kind="STAFF_SESSION",
        authority_version=1,
        trusted_control_api=False,
        initiator_user_id=principal.user_id,
        initiator_session_id=principal.session_id,
        provisioning_job_id=job_id,
        provisioning_attempt_count=claimed.attempt_count,
    )


async def _reauthorize_account(
    *,
    account_id: uuid.UUID,
    user_id: uuid.UUID | None,
    name: str,
    provider_username: str,
    bootstrap: bool = False,
) -> tuple[uuid.UUID, str]:
    if bootstrap:
        _token, session_id = await issue_session()
        principal = await principal_from_session_id(session_id)
        assert principal is not None
        authority_kind = "BOOTSTRAP_SESSION"
        owner_user_id = None
    else:
        authenticated = await authenticate("channel-owner-a", "channel-owner-password-123")
        assert authenticated is not None
        principal, token = authenticated
        session_id = principal.session_id
        owner_user_id = user_id
        authority_kind = "STAFF_SESSION"
    assert session_id is not None
    credentials = {"bot_token": f"token-for-{name}"}
    async with get_session_factory()() as session:
        if not bootstrap:
            session.add(
                models.AccountReauthorizationGrant(
                    tenant_id="tenant-a",
                    platform_account_id=account_id,
                    user_id=user_id,
                    active=True,
                )
            )
            await session.commit()
    job_id = await jobs.submit_provisioning_job(
        tenant_id="tenant-a",
        brand_id="default",
        platform="telegram",
        actor=principal.actor,
        request={"name": name, "idempotency_key": f"reauth-{uuid.uuid4()}"},
        secrets=credentials,
        operation="REAUTHORIZE",
        target_account_id=account_id,
        expected_config_version=1,
        admin_session_id=session_id,
    )
    claimed = await jobs._claim_job(job_id)
    assert claimed is not None
    await provisioning.bind_provisioning_external_identity(
        provisioning_job_id=job_id,
        provisioning_attempt_count=claimed.attempt_count,
        platform="telegram",
        external_account_id="shared-external-account",
        credential_bundle=credentials,
        platform_app_id=None,
    )
    return await provisioning.provision_direct_account(
        platform="telegram",
        external_account_id="shared-external-account",
        tenant_id="tenant-a",
        brand_id="default",
        name=name,
        public_id="tg_ownership",
        public_id_prefix="tg",
        secrets_root=Path(".secrets/accounts"),
        credential_bundle=credentials,
        webhook_secret_bundle={"secret": f"webhook-for-{name}"},
        config={"api_base_url": "https://api.telegram.org"},
        capability={"dm": True, "max_text_length": 4096},
        automation_default="BOT_DRAFT_ONLY",
        owner_user_id=owner_user_id,
        provider_username=provider_username,
        profile_updated_at=datetime.now(UTC),
        operation="REAUTHORIZE",
        target_account_id=account_id,
        expected_config_version=1,
        initiator_user_id=principal.user_id,
        initiator_session_id=session_id,
        authority_kind=authority_kind,
        authority_version=1,
        trusted_control_api=False,
        provisioning_job_id=job_id,
        provisioning_attempt_count=claimed.attempt_count,
    )


async def test_concurrent_users_cannot_both_claim_same_external_account(
    migrated_db,
) -> None:
    first_user_id, second_user_id = await _seed_users()

    results = await asyncio.gather(
        _provision_account(owner_user_id=first_user_id, name="First owner"),
        _provision_account(owner_user_id=second_user_id, name="Second owner"),
        return_exceptions=True,
    )

    successful_results = [result for result in results if isinstance(result, tuple)]
    conflicts = [result for result in results if isinstance(result, PermissionError)]
    assert len(successful_results) == 1
    assert len(conflicts) == 1
    assert str(conflicts[0]) == "platform_account_owner_conflict"

    async with get_session_factory()() as session:
        accounts = list(
            (
                await session.execute(
                    select(models.PlatformAccount).where(
                        models.PlatformAccount.tenant_id == "tenant-a",
                        models.PlatformAccount.platform == "telegram",
                        models.PlatformAccount.external_account_id == "shared-external-account",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(accounts) == 1
    assert accounts[0].owner_user_id in {first_user_id, second_user_id}


async def test_same_user_reauthorization_preserves_owner_and_public_id(
    migrated_db,
) -> None:
    first_user_id, _second_user_id = await _seed_users()
    first_account_id, first_public_id = await _provision_account(
        owner_user_id=first_user_id,
        name="Initial account",
        provider_username="initial_bot",
    )

    second_account_id, second_public_id = await _reauthorize_account(
        account_id=first_account_id,
        user_id=first_user_id,
        name="Reauthorized account",
        provider_username="updated_bot",
    )

    assert second_account_id == first_account_id
    assert second_public_id == first_public_id
    async with get_session_factory()() as session:
        account = await session.get(models.PlatformAccount, first_account_id)
        agent = await session.scalar(
            select(models.Agent).where(
                models.Agent.tenant_id == "tenant-a",
                models.Agent.legacy_brand_id == "default",
            )
        )
        version_count = await session.scalar(
            select(func.count())
            .select_from(models.AgentVersion)
            .where(models.AgentVersion.agent_id == agent.id)
        )
        deployment_count = await session.scalar(
            select(func.count())
            .select_from(models.AgentDeployment)
            .where(models.AgentDeployment.agent_id == agent.id)
        )
    assert account is not None
    assert account.owner_user_id == first_user_id
    assert account.name == "Reauthorized account"
    assert account.provider_username == "updated_bot"
    assert account.config_version == 2
    assert version_count == 1
    assert deployment_count == 1


async def test_admin_repair_preserves_existing_user_owner(migrated_db) -> None:
    first_user_id, _second_user_id = await _seed_users()
    account_id, public_id = await _provision_account(
        owner_user_id=first_user_id,
        name="User account",
        provider_username="user_bot",
    )

    repaired_account_id, repaired_public_id = await _reauthorize_account(
        account_id=account_id,
        user_id=None,
        name="Admin repaired account",
        provider_username="repaired_bot",
        bootstrap=True,
    )

    assert repaired_account_id == account_id
    assert repaired_public_id == public_id
    async with get_session_factory()() as session:
        account = await session.get(models.PlatformAccount, account_id)
    assert account is not None
    assert account.owner_user_id == first_user_id
    assert account.name == "Admin repaired account"
    assert account.provider_username == "repaired_bot"
