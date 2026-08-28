import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from social_reply.application.account_management.provisioning import (
    provision_direct_account,
)
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory

pytestmark = pytest.mark.integration


async def _seed_users() -> tuple[uuid.UUID, uuid.UUID]:
    first_user_id = uuid.uuid4()
    second_user_id = uuid.uuid4()
    async with get_session_factory()() as session:
        session.add_all(
            [
                models.AdminUser(
                    id=first_user_id,
                    username="channel-owner-a",
                    password_hash="test-hash",
                    tenant_id="tenant-a",
                    role="USER",
                    must_change_password=False,
                    status="active",
                ),
                models.AdminUser(
                    id=second_user_id,
                    username="channel-owner-b",
                    password_hash="test-hash",
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
    return await provision_direct_account(
        platform="telegram",
        external_account_id="shared-external-account",
        tenant_id="tenant-a",
        brand_id="default",
        name=name,
        public_id=None,
        public_id_prefix="tg",
        secrets_root=Path(".secrets/accounts"),
        credential_bundle={"bot_token": f"token-for-{name}"},
        webhook_secret_bundle={"secret": f"webhook-for-{name}"},
        config={"api_base_url": "https://api.telegram.org"},
        capability={"dm": True, "max_text_length": 4096},
        automation_default="BOT_DRAFT_ONLY",
        owner_user_id=owner_user_id,
        provider_username=provider_username,
        avatar_url=avatar_url,
        profile_updated_at=datetime.now(UTC),
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
                        models.PlatformAccount.external_account_id
                        == "shared-external-account",
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

    second_account_id, second_public_id = await _provision_account(
        owner_user_id=first_user_id,
        name="Reauthorized account",
        provider_username="updated_bot",
    )

    assert second_account_id == first_account_id
    assert second_public_id == first_public_id
    async with get_session_factory()() as session:
        account = await session.get(models.PlatformAccount, first_account_id)
    assert account is not None
    assert account.owner_user_id == first_user_id
    assert account.name == "Reauthorized account"
    assert account.provider_username == "updated_bot"
    assert account.config_version == 2


async def test_admin_repair_preserves_existing_user_owner(migrated_db) -> None:
    first_user_id, _second_user_id = await _seed_users()
    account_id, public_id = await _provision_account(
        owner_user_id=first_user_id,
        name="User account",
        provider_username="user_bot",
    )

    repaired_account_id, repaired_public_id = await _provision_account(
        owner_user_id=None,
        name="Admin repaired account",
        provider_username="repaired_bot",
    )

    assert repaired_account_id == account_id
    assert repaired_public_id == public_id
    async with get_session_factory()() as session:
        account = await session.get(models.PlatformAccount, account_id)
    assert account is not None
    assert account.owner_user_id == first_user_id
    assert account.name == "Admin repaired account"
    assert account.provider_username == "repaired_bot"
