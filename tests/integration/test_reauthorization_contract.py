import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, update

from social_reply.application.account_management import jobs, provisioning
from social_reply.application.account_management.auth import (
    authenticate,
    hash_password,
    revoke_session,
)
from social_reply.application.account_management.service import AccountConnectionResult
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.secret_crypto import decrypt_secret_bundle, encrypt_secret_bundle
from social_reply.shared.config import get_settings

pytestmark = pytest.mark.integration
_PASSWORD = "grantee-password-123"


@pytest.fixture(autouse=True)
def _allow_test_tenant(monkeypatch):
    monkeypatch.setenv("ADMIN_ALLOWED_TENANTS", "default")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _seed_reauthorization_account() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    owner_id = uuid.uuid4()
    grantee_id = uuid.uuid4()
    account_id = uuid.uuid4()
    password_hash = await hash_password(_PASSWORD)
    async with get_session_factory()() as session:
        session.add_all(
            [
                models.AdminUser(
                    id=owner_id,
                    username=f"owner-{owner_id}",
                    password_hash=password_hash,
                    tenant_id="default",
                    role="USER",
                    must_change_password=False,
                    status="active",
                ),
                models.AdminUser(
                    id=grantee_id,
                    username=f"grantee-{grantee_id}",
                    password_hash=password_hash,
                    tenant_id="default",
                    role="USER",
                    must_change_password=False,
                    status="active",
                ),
            ]
        )
        await session.flush()
        session.add(
            models.PlatformAccount(
                id=account_id,
                tenant_id="default",
                brand_id="brand-a",
                platform="x",
                owner_user_id=owner_id,
                name="Original account",
                provider_username="original",
                external_account_id="x-42",
                public_id="x_original",
                credential_bundle=encrypt_secret_bundle({"access_token": "old"}),
                config={"delivery_mode": "direct", "operator_choice": "keep"},
                capability={"dm": True, "x_chat": False},
                automation_default="BOT_ACTIVE",
                config_version=1,
                status="active",
            )
        )
        await session.flush()
        session.add(
            models.AccountReauthorizationGrant(
                tenant_id="default",
                platform_account_id=account_id,
                user_id=grantee_id,
                active=True,
            )
        )
        await session.commit()
    return account_id, owner_id, grantee_id


async def _staff_session(user_id: uuid.UUID):
    result = await authenticate(f"grantee-{user_id}", _PASSWORD)
    assert result is not None
    principal, token = result
    assert principal.user_id == user_id
    assert principal.session_id is not None
    return principal, token


_REAUTH_OAUTH_CREDENTIALS = {
    "consumer_key": "consumer-key",
    "consumer_secret": "consumer-secret",
    "access_token": "access-token-new",
    "access_token_secret": "access-token-secret-new",
    "xchat_pin": "1234",
}


async def _seed_unbound_staff_reauthorization_claim(
    *,
    account_id: uuid.UUID,
    grantee_id: uuid.UUID,
    idempotency_key: str,
    claim_immediately: bool = True,
):
    principal, _token = await _staff_session(grantee_id)
    job_id = await jobs.submit_provisioning_job(
        tenant_id="default",
        brand_id="brand-a",
        platform="x",
        actor=principal.actor,
        request={
            "environment": "oauth",
            "external_account_id": "x-42",
            "public_id": "x_original",
            "idempotency_key": idempotency_key,
        },
        secrets=dict(_REAUTH_OAUTH_CREDENTIALS),
        operation="REAUTHORIZE",
        target_account_id=account_id,
        expected_config_version=1,
        admin_session_id=principal.session_id,
    )
    if not claim_immediately:
        return job_id, 0, principal
    claimed = await jobs._claim_job(job_id)
    assert claimed is not None
    return job_id, claimed.attempt_count, principal


async def _fake_x_reauthorization(**kwargs):
    assert kwargs["xchat_pin"] == "1234"
    credentials = {
        key: kwargs[key]
        for key in (
            "consumer_key",
            "consumer_secret",
            "access_token",
            "access_token_secret",
        )
    }
    credentials.update(
        {
            "xchat_private_keys_b64": "fake-unlocked-private-keys",
            "xchat_signing_key_version": "1",
        }
    )
    await provisioning.bind_provisioning_external_identity(
        provisioning_job_id=kwargs["provisioning_job_id"],
        provisioning_attempt_count=kwargs["provisioning_attempt_count"],
        platform="x",
        external_account_id="x-42",
        credential_bundle=credentials,
        platform_app_id=None,
    )
    account_id, public_id = await provisioning.provision_direct_account(
        platform="x",
        external_account_id="x-42",
        tenant_id=kwargs["tenant_id"],
        brand_id=kwargs["brand_id"],
        name=kwargs["name"] or "Reauthorized account",
        public_id=kwargs["public_id"],
        public_id_prefix="x",
        secrets_root=kwargs["secrets_root"],
        credential_bundle=credentials,
        webhook_secret_bundle={"consumer_secret": credentials["consumer_secret"]},
        config={
            "api_base_url": kwargs.get("api_base_url", "https://api.x.com"),
            "xchat_enabled": True,
            "xchat_key_state": "READY",
            "xchat_registered": True,
            "xchat_public_key_version": "1",
        },
        capability={"dm": True, "x_chat": True, "mentions": True, "max_text_length": 280},
        automation_default=kwargs["automation_default"],
        owner_user_id=kwargs["owner_user_id"],
        provider_username="reauthorized",
        platform_app_id=None,
        operation=kwargs["operation"],
        target_account_id=kwargs["target_account_id"],
        expected_config_version=kwargs["expected_config_version"],
        initiator_user_id=kwargs["initiator_user_id"],
        initiator_session_id=kwargs["initiator_session_id"],
        authority_kind=kwargs["authority_kind"],
        authority_version=kwargs["authority_version"],
        provisioning_job_id=kwargs["provisioning_job_id"],
        provisioning_attempt_count=kwargs["provisioning_attempt_count"],
    )
    return AccountConnectionResult(
        account_id=account_id,
        platform="x",
        external_account_id="x-42",
        public_id=public_id,
        webhook_url="https://reply.example.com/webhooks/x/x_original",
        name="Reauthorized account",
        automation_default=kwargs["automation_default"],
        credential_updated=True,
        connection_ready=False,
        connection_status="NEEDS_ACTION",
    )


async def _seed_staff_claim(
    *,
    account_id: uuid.UUID,
    grantee_id: uuid.UUID,
    expected_config_version: int,
    credential_bundle: dict[str, str] | None = None,
    idempotency_key: str | None = None,
) -> tuple[uuid.UUID, int, object, str]:
    principal, token = await _staff_session(grantee_id)
    credentials = credential_bundle or {"access_token": "new"}
    job_id = await jobs.submit_provisioning_job(
        tenant_id="default",
        brand_id="brand-a",
        platform="x",
        actor=principal.actor,
        request={
            "environment": "oauth",
            "external_account_id": "x-42",
            "idempotency_key": idempotency_key or uuid.uuid4().hex,
        },
        secrets=credentials,
        operation="REAUTHORIZE",
        target_account_id=account_id,
        expected_config_version=expected_config_version,
        admin_session_id=principal.session_id,
    )
    claimed = await jobs._claim_job(job_id)
    assert claimed is not None
    await provisioning.bind_provisioning_external_identity(
        provisioning_job_id=job_id,
        provisioning_attempt_count=claimed.attempt_count,
        platform="x",
        external_account_id="x-42",
        credential_bundle=credentials,
        platform_app_id=None,
    )
    return job_id, claimed.attempt_count, principal, token


async def _seed_control_claim(
    *,
    platform: str,
    external_account_id: str,
    credential_bundle: dict[str, str],
    brand_id: str = "brand-a",
) -> tuple[uuid.UUID, int]:
    request = {"idempotency_key": uuid.uuid4().hex}
    if platform == "feishu":
        request["app_id"] = external_account_id
    elif platform == "email":
        request["email_address"] = external_account_id
    else:
        request["external_account_id"] = external_account_id
    job_id = await jobs.submit_control_provisioning_job(
        tenant_id="default",
        brand_id=brand_id,
        platform=platform,
        actor="service:control_api",
        request=request,
        secrets=credential_bundle,
    )
    claimed = await jobs._claim_job(job_id)
    assert claimed is not None
    await provisioning.bind_provisioning_external_identity(
        provisioning_job_id=job_id,
        provisioning_attempt_count=claimed.attempt_count,
        platform=platform,
        external_account_id=external_account_id,
        credential_bundle=credential_bundle,
        platform_app_id=None,
    )
    return job_id, claimed.attempt_count


async def _reauthorize_with_claim(
    *,
    account_id: uuid.UUID,
    grantee_id: uuid.UUID,
    job_id: uuid.UUID,
    attempt_count: int,
    principal,
    credential_bundle: dict[str, str] | None = None,
    **overrides,
):
    credentials = credential_bundle or {"access_token": "new"}
    values = {
        "platform": "x",
        "external_account_id": "x-42",
        "tenant_id": "default",
        "brand_id": "brand-a",
        "name": "ignored-name",
        "public_id": "x_original",
        "public_id_prefix": "x",
        "secrets_root": Path(".secrets/accounts"),
        "credential_bundle": credentials,
        "webhook_secret_bundle": None,
        "config": {"operator_choice": "must-not-change"},
        "capability": {"dm": False},
        "automation_default": "BOT_DRAFT_ONLY",
        "owner_user_id": grantee_id,
        "operation": "REAUTHORIZE",
        "target_account_id": account_id,
        "expected_config_version": 1,
        "initiator_user_id": grantee_id,
        "initiator_session_id": principal.session_id,
        "authority_kind": "STAFF_SESSION",
        "authority_version": 1,
        "provisioning_job_id": job_id,
        "provisioning_attempt_count": attempt_count,
        "trusted_control_api": False,
    }
    values.update(overrides)
    return await provisioning.provision_direct_account(**values)


@pytest.mark.asyncio
async def test_non_owner_active_grant_reauthorization_preserves_business_fields(migrated_db):
    account_id, owner_id, grantee_id = await _seed_reauthorization_account()
    job_id, attempt_count, principal, _token = await _seed_staff_claim(
        account_id=account_id,
        grantee_id=grantee_id,
        expected_config_version=1,
    )
    await _reauthorize_with_claim(
        account_id=account_id,
        grantee_id=grantee_id,
        job_id=job_id,
        attempt_count=attempt_count,
        principal=principal,
    )
    async with get_session_factory()() as session:
        account = await session.get(models.PlatformAccount, account_id)
        job = await session.get(models.ProvisioningJob, job_id)
    assert account is not None and job is not None
    assert account.owner_user_id == owner_id
    assert account.brand_id == "brand-a"
    assert account.public_id == "x_original"
    assert account.config == {"delivery_mode": "direct", "operator_choice": "keep"}
    assert account.capability == {"dm": True, "x_chat": False}
    assert account.config_version == 2
    assert decrypt_secret_bundle(account.credential_bundle) == {"access_token": "new"}
    assert job.status == "PROCESSING"
    assert job.account_id == account_id
    assert job.current_step == "CREDENTIALS_APPLIED"
    assert job.result["checkpoint"]["output_config_version"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("grant_active", [False, True])
async def test_reauthorization_uses_real_session_and_claim_for_permission_denials(
    migrated_db, grant_active
):
    account_id, _owner_id, grantee_id = await _seed_reauthorization_account()
    job_id, attempt_count, principal, token = await _seed_staff_claim(
        account_id=account_id,
        grantee_id=grantee_id,
        expected_config_version=1,
    )
    async with get_session_factory()() as session:
        grant = (
            await session.execute(
                select(models.AccountReauthorizationGrant).where(
                    models.AccountReauthorizationGrant.platform_account_id == account_id,
                    models.AccountReauthorizationGrant.user_id == grantee_id,
                )
            )
        ).scalar_one()
        grant.active = grant_active
        await session.commit()
    if grant_active:
        await revoke_session(token)
        expected = "initiator_session_invalid"
    else:
        expected = "account_reauthorization_denied"
    with pytest.raises(PermissionError, match=expected):
        await _reauthorize_with_claim(
            account_id=account_id,
            grantee_id=grantee_id,
            job_id=job_id,
            attempt_count=attempt_count,
            principal=principal,
        )


@pytest.mark.asyncio
async def test_reauthorization_checkpoint_survives_crash_and_later_admin_change(
    migrated_db, monkeypatch
):
    account_id, _owner_id, grantee_id = await _seed_reauthorization_account()
    job_id, attempt_count, principal, _token = await _seed_staff_claim(
        account_id=account_id,
        grantee_id=grantee_id,
        expected_config_version=1,
        idempotency_key="reauth-crash-stable-marker",
    )
    await _reauthorize_with_claim(
        account_id=account_id,
        grantee_id=grantee_id,
        job_id=job_id,
        attempt_count=attempt_count,
        principal=principal,
    )
    async with get_session_factory()() as session:
        account = await session.get(models.PlatformAccount, account_id, with_for_update=True)
        assert account is not None
        account.config_version = 3
        account.config = {**account.config, "admin_changed": True}
        await session.commit()
    await _reauthorize_with_claim(
        account_id=account_id,
        grantee_id=grantee_id,
        job_id=job_id,
        attempt_count=attempt_count,
        principal=principal,
    )
    async with get_session_factory()() as session:
        await session.execute(
            update(models.ProvisioningJob)
            .where(models.ProvisioningJob.id == job_id)
            .values(locked_at=datetime.now(UTC) - timedelta(minutes=10))
        )
        await session.commit()
    dispatched: list[str] = []

    async def _dispatch(_actor, dispatched_job_id: str, **_kwargs):
        dispatched.append(dispatched_job_id)

    monkeypatch.setattr(jobs, "dispatch_actor", _dispatch)
    assert job_id in await jobs.sweep_provisioning_jobs()
    assert dispatched == [str(job_id)]
    assert await jobs.process_provisioning_job(str(job_id)) == "NEEDS_ACTION"
    async with get_session_factory()() as session:
        account = await session.get(models.PlatformAccount, account_id)
    assert account is not None
    assert account.config_version == 3
    assert account.config["admin_changed"] is True



@pytest.mark.asyncio
async def test_x_reauthorization_crash_after_credentials_applied_retries_without_pin(
    migrated_db, monkeypatch
):
    account_id, _owner_id, grantee_id = await _seed_reauthorization_account()
    job_id, _attempt_count, principal = await _seed_unbound_staff_reauthorization_claim(
        account_id=account_id,
        grantee_id=grantee_id,
        idempotency_key="reauth-real-crash-recovery",
        claim_immediately=False,
    )
    provider_calls: list[dict] = []

    async def fake_connect(**kwargs):
        provider_calls.append(kwargs)
        return await _fake_x_reauthorization(**kwargs)

    monkeypatch.setattr(jobs, "connect_x_account", fake_connect)
    real_connect = jobs._connect

    async def crash_after_credentials(job):
        await real_connect(job)
        raise RuntimeError("simulated crash after credential checkpoint")

    monkeypatch.setattr(jobs, "_connect", crash_after_credentials)
    assert await jobs.process_provisioning_job(str(job_id)) == "FAILED"

    async with get_session_factory()() as session:
        failed = await session.get(models.ProvisioningJob, job_id)
        account = await session.get(models.PlatformAccount, account_id)
    assert failed is not None and account is not None
    assert failed.status == "FAILED"
    assert failed.current_step == "CREDENTIALS_APPLIED"
    assert failed.result["checkpoint"]["phase"] == "CREDENTIALS_APPLIED"
    assert "requires_secret_resubmission" not in failed.result
    assert decrypt_secret_bundle(failed.staging_secret).get("xchat_pin") is None
    assert decrypt_secret_bundle(account.credential_bundle) == {
        "consumer_key": "consumer-key",
        "consumer_secret": "consumer-secret",
        "access_token": "access-token-new",
        "access_token_secret": "access-token-secret-new",
        "xchat_private_keys_b64": "fake-unlocked-private-keys",
        "xchat_signing_key_version": "1",
    }
    async with get_session_factory()() as session:
        marker = await session.get(models.ProvisioningJob, job_id)
        assert marker is not None
        marker.result = {
            **dict(marker.result or {}),
            "requires_secret_resubmission": True,
            "required_secret": "xchat_pin",
        }
        await session.commit()

    monkeypatch.setattr(jobs, "_connect", real_connect)
    await jobs.retry_provisioning_job(
        job_id,
        tenant_id="default",
        caller=principal,
    )
    resumed_status = await jobs.process_provisioning_job(str(job_id))
    assert resumed_status in {"COMPLETED", "NEEDS_ACTION"}
    assert len(provider_calls) == 1
    async with get_session_factory()() as session:
        recovered = await session.get(models.ProvisioningJob, job_id)
        account_after = await session.get(models.PlatformAccount, account_id)
    assert recovered is not None and account_after is not None
    assert "requires_secret_resubmission" not in recovered.result
    assert decrypt_secret_bundle(recovered.staging_secret).get("xchat_pin") is None
    assert decrypt_secret_bundle(account_after.credential_bundle) == decrypt_secret_bundle(
        account.credential_bundle
    )


@pytest.mark.asyncio
async def test_x_reauthorization_normal_completion_removes_pin_from_staging(
    migrated_db, monkeypatch
):
    account_id, _owner_id, grantee_id = await _seed_reauthorization_account()
    job_id, _attempt_count, _principal = await _seed_unbound_staff_reauthorization_claim(
        account_id=account_id,
        grantee_id=grantee_id,
        idempotency_key="reauth-normal-completion",
        claim_immediately=False,
    )
    provider_calls: list[dict] = []

    async def fake_connect(**kwargs):
        provider_calls.append(kwargs)
        return await _fake_x_reauthorization(**kwargs)

    monkeypatch.setattr(jobs, "connect_x_account", fake_connect)
    assert await jobs.process_provisioning_job(str(job_id)) == "NEEDS_ACTION"
    assert len(provider_calls) == 1
    async with get_session_factory()() as session:
        row = await session.get(models.ProvisioningJob, job_id)
        account = await session.get(models.PlatformAccount, account_id)
    assert row is not None and account is not None
    assert row.status == "NEEDS_ACTION"
    assert row.current_step == "CREDENTIALS_APPLIED"
    assert "requires_secret_resubmission" not in row.result
    assert decrypt_secret_bundle(row.staging_secret).get("xchat_pin") is None
    assert decrypt_secret_bundle(account.credential_bundle)["access_token"] == "access-token-new"


@pytest.mark.asyncio
async def test_x_reauthorization_sweep_cleans_old_pin_marker_without_reapplying_credentials(
    migrated_db, monkeypatch
):
    account_id, _owner_id, grantee_id = await _seed_reauthorization_account()
    job_id, _attempt_count, _principal = await _seed_unbound_staff_reauthorization_claim(
        account_id=account_id,
        grantee_id=grantee_id,
        idempotency_key="reauth-real-sweep-recovery",
    )
    provider_calls: list[dict] = []

    async def fake_connect(**kwargs):
        provider_calls.append(kwargs)
        return await _fake_x_reauthorization(**kwargs)

    monkeypatch.setattr(jobs, "connect_x_account", fake_connect)
    async with get_session_factory()() as session:
        claimed = await session.get(models.ProvisioningJob, job_id)
    assert claimed is not None
    await jobs._connect(claimed)

    async with get_session_factory()() as session:
        row = await session.get(models.ProvisioningJob, job_id)
        assert row is not None
        staged = decrypt_secret_bundle(row.staging_secret)
        staged.pop("xchat_pin", None)
        row.staging_secret = encrypt_secret_bundle(staged)
        row.result = {
            **dict(row.result or {}),
            "requires_secret_resubmission": True,
        }
        row.locked_at = datetime.now(UTC) - timedelta(minutes=10)
        await session.commit()

    dispatched: list[str] = []

    async def _dispatch(_actor, dispatched_job_id: str, **_kwargs):
        dispatched.append(dispatched_job_id)

    monkeypatch.setattr(jobs, "dispatch_actor", _dispatch)
    assert job_id in await jobs.sweep_provisioning_jobs()
    assert dispatched == [str(job_id)]
    async with get_session_factory()() as session:
        swept = await session.get(models.ProvisioningJob, job_id)
    assert swept is not None
    assert swept.status == "FAILED"
    assert swept.current_step == "CREDENTIALS_APPLIED"
    assert "requires_secret_resubmission" not in swept.result
    assert decrypt_secret_bundle(swept.staging_secret).get("xchat_pin") is None

    resumed_status = await jobs.process_provisioning_job(str(job_id))
    assert resumed_status in {"COMPLETED", "NEEDS_ACTION"}
    assert len(provider_calls) == 1


@pytest.mark.asyncio
async def test_x_reauthorization_mismatched_checkpoint_keeps_pin_resubmission_required(
    migrated_db, monkeypatch
):
    account_id, _owner_id, grantee_id = await _seed_reauthorization_account()
    job_id, _attempt_count, _principal = await _seed_unbound_staff_reauthorization_claim(
        account_id=account_id,
        grantee_id=grantee_id,
        idempotency_key="reauth-invalid-checkpoint",
    )

    provider_calls: list[dict] = []

    async def fake_connect(**kwargs):
        provider_calls.append(kwargs)
        return await _fake_x_reauthorization(**kwargs)

    monkeypatch.setattr(jobs, "connect_x_account", fake_connect)
    async with get_session_factory()() as session:
        claimed = await session.get(models.ProvisioningJob, job_id)
    assert claimed is not None
    await jobs._connect(claimed)
    async with get_session_factory()() as session:
        row = await session.get(models.ProvisioningJob, job_id)
        assert row is not None
        result = dict(row.result or {})
        result["checkpoint"] = {
            **dict(result["checkpoint"]),
            "credential_fingerprint": "forged-checkpoint-fingerprint",
        }
        row.result = result
        row.locked_at = datetime.now(UTC) - timedelta(minutes=10)
        await session.commit()
    async with get_session_factory()() as session:
        fresh_row = (
            await session.execute(
                select(models.ProvisioningJob)
                .where(models.ProvisioningJob.id == job_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
    with pytest.raises(
        ValueError,
        match="(provisioning_checkpoint_invalid|account_reauthorization_version_conflict)",
    ):
        await jobs._connect(fresh_row)
    assert len(provider_calls) == 1

    assert await jobs.sweep_provisioning_jobs() == []
    async with get_session_factory()() as session:
        invalid = await session.get(models.ProvisioningJob, job_id)
    assert invalid is not None
    assert invalid.status == "NEEDS_ACTION"
    assert invalid.result["requires_secret_resubmission"] is True
    assert invalid.result["required_secret"] == "xchat_pin"
    assert decrypt_secret_bundle(invalid.staging_secret).get("xchat_pin") is None



@pytest.mark.asyncio
async def test_x_reauthorization_changed_oauth_input_rejects_checkpoint_resume(
    migrated_db, monkeypatch
):
    account_id, _owner_id, grantee_id = await _seed_reauthorization_account()
    job_id, _attempt_count, _principal = await _seed_unbound_staff_reauthorization_claim(
        account_id=account_id,
        grantee_id=grantee_id,
        idempotency_key="reauth-changed-oauth-input",
    )
    provider_calls: list[dict] = []

    async def fake_connect(**kwargs):
        provider_calls.append(kwargs)
        return await _fake_x_reauthorization(**kwargs)

    monkeypatch.setattr(jobs, "connect_x_account", fake_connect)
    async with get_session_factory()() as session:
        claimed = await session.get(models.ProvisioningJob, job_id)
    assert claimed is not None
    await jobs._connect(claimed)

    async with get_session_factory()() as session:
        row = await session.get(models.ProvisioningJob, job_id)
        assert row is not None
        staged = decrypt_secret_bundle(row.staging_secret)
        staged["access_token"] = "changed-after-checkpoint"
        row.staging_secret = encrypt_secret_bundle(staged)
        await session.commit()

    async with get_session_factory()() as session:
        fresh_row = (
            await session.execute(
                select(models.ProvisioningJob)
                .where(models.ProvisioningJob.id == job_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
    with pytest.raises(
        ValueError,
        match="(provisioning_checkpoint_invalid|account_reauthorization_version_conflict)",
    ):
        await jobs._connect(fresh_row)
    assert len(provider_calls) == 1


@pytest.mark.asyncio
async def test_xchat_cleanup_preserves_password_needed_action_marker(
    migrated_db, monkeypatch
):
    account_id, _owner_id, grantee_id = await _seed_reauthorization_account()
    job_id, _attempt_count, _principal = await _seed_unbound_staff_reauthorization_claim(
        account_id=account_id,
        grantee_id=grantee_id,
        idempotency_key="reauth-password-marker",
    )
    monkeypatch.setattr(jobs, "connect_x_account", _fake_x_reauthorization)
    async with get_session_factory()() as session:
        claimed = await session.get(models.ProvisioningJob, job_id)
    assert claimed is not None
    await jobs._connect(claimed)
    async with get_session_factory()() as session:
        row = await session.get(models.ProvisioningJob, job_id)
        assert row is not None
        row.result = {
            **dict(row.result or {}),
            "requires_secret_resubmission": True,
            "required_secret": "password",
            "manual_followup": True,
        }
        row.locked_at = datetime.now(UTC) - timedelta(minutes=10)
        await session.commit()

    assert await jobs.sweep_provisioning_jobs() == []
    async with get_session_factory()() as session:
        preserved = await session.get(models.ProvisioningJob, job_id)
    assert preserved is not None
    assert preserved.status == "NEEDS_ACTION"
    assert preserved.result["requires_secret_resubmission"] is True
    assert preserved.result["required_secret"] == "password"
    assert preserved.result["manual_followup"] is True
    assert decrypt_secret_bundle(preserved.staging_secret).get("xchat_pin") is None

@pytest.mark.asyncio
async def test_same_marker_reuses_applied_reauthorization_job_after_version_change(migrated_db):
    account_id, _owner_id, grantee_id = await _seed_reauthorization_account()
    job_id, attempt_count, principal, _token = await _seed_staff_claim(
        account_id=account_id,
        grantee_id=grantee_id,
        expected_config_version=1,
        idempotency_key="same-reauth-marker",
    )
    await _reauthorize_with_claim(
        account_id=account_id,
        grantee_id=grantee_id,
        job_id=job_id,
        attempt_count=attempt_count,
        principal=principal,
    )
    reused = await jobs.submit_provisioning_job(
        tenant_id="default",
        brand_id="brand-a",
        platform="x",
        actor=principal.actor,
        request={
            "environment": "oauth",
            "external_account_id": "x-42",
            "idempotency_key": "same-reauth-marker",
        },
        secrets={"access_token": "new"},
        operation="REAUTHORIZE",
        target_account_id=account_id,
        expected_config_version=2,
        admin_session_id=principal.session_id,
    )
    assert reused == job_id
    with pytest.raises(ValueError, match="idempotency_key_payload_mismatch"):
        await jobs.submit_provisioning_job(
            tenant_id="default",
            brand_id="brand-a",
            platform="x",
            actor=principal.actor,
            request={
                "environment": "oauth",
                "external_account_id": "x-42",
                "idempotency_key": "same-reauth-marker",
            },
            secrets={"access_token": "different"},
            operation="REAUTHORIZE",
            target_account_id=account_id,
            expected_config_version=2,
            admin_session_id=principal.session_id,
        )


@pytest.mark.asyncio
async def test_forged_helper_identity_and_credentials_are_rejected(migrated_db):
    account_id, _owner_id, grantee_id = await _seed_reauthorization_account()
    job_id, attempt_count, principal, _token = await _seed_staff_claim(
        account_id=account_id,
        grantee_id=grantee_id,
        expected_config_version=1,
    )
    with pytest.raises(PermissionError, match="provisioning_authority_invalid"):
        await _reauthorize_with_claim(
            account_id=account_id,
            grantee_id=grantee_id,
            job_id=job_id,
            attempt_count=attempt_count,
            principal=principal,
            credential_bundle={"access_token": "forged"},
        )
    with pytest.raises(PermissionError, match="provisioning_authority_invalid"):
        await _reauthorize_with_claim(
            account_id=account_id,
            grantee_id=grantee_id,
            job_id=job_id,
            attempt_count=attempt_count,
            principal=principal,
            external_account_id="wrong-account",
        )
    with pytest.raises(PermissionError, match="provisioning_authority_invalid"):
        await _reauthorize_with_claim(
            account_id=account_id,
            grantee_id=grantee_id,
            job_id=job_id,
            attempt_count=attempt_count,
            principal=principal,
            brand_id="forged-brand",
        )


@pytest.mark.asyncio
async def test_connect_rejects_existing_account_for_other_claim(migrated_db):
    account_id, owner_id, _grantee_id = await _seed_reauthorization_account()
    job_id, attempt_count = await _seed_control_claim(
        platform="x",
        external_account_id="x-42",
        credential_bundle={"access_token": "replacement"},
    )
    with pytest.raises(ValueError, match="platform_account_already_exists"):
        await provisioning.provision_direct_account(
            platform="x",
            external_account_id="x-42",
            tenant_id="default",
            brand_id="brand-a",
            name="account",
            public_id="x_original",
            public_id_prefix="x",
            secrets_root=Path(".secrets/accounts"),
            credential_bundle={"access_token": "replacement"},
            platform_app_id=None,
            webhook_secret_bundle=None,
            config={},
            capability={},
            automation_default="BOT_DRAFT_ONLY",
            owner_user_id=None,
            authority_kind="CONTROL_API",
            authority_version=1,
            trusted_control_api=True,
            operation="CONNECT_ACCOUNT",
            provisioning_job_id=job_id,
            provisioning_attempt_count=attempt_count,
        )
    assert account_id


@pytest.mark.asyncio
async def test_control_new_account_requires_draft_only_with_real_claim(migrated_db):
    job_id, attempt_count = await _seed_control_claim(
        platform="x",
        external_account_id="x-new",
        credential_bundle={"access_token": "new"},
    )
    with pytest.raises(ValueError, match="new_account_requires_bot_draft_only"):
        await provisioning.provision_direct_account(
            platform="x",
            external_account_id="x-new",
            tenant_id="default",
            brand_id="brand-a",
            name="new account",
            public_id=None,
            public_id_prefix="x",
            secrets_root=Path(".secrets/accounts"),
            credential_bundle={"access_token": "new"},
            platform_app_id=None,
            webhook_secret_bundle=None,
            config={},
            capability={},
            automation_default="BOT_ACTIVE",
            authority_kind="CONTROL_API",
            authority_version=1,
            trusted_control_api=True,
            provisioning_job_id=job_id,
            provisioning_attempt_count=attempt_count,
        )


@pytest.mark.asyncio
async def test_unverified_legacy_job_is_rejected(migrated_db):
    job_id = uuid.uuid4()
    async with get_session_factory()() as session:
        session.add(
            models.ProvisioningJob(
                id=job_id,
                tenant_id="default",
                brand_id="brand-a",
                platform="x",
                operation="CONNECT_ACCOUNT",
                actor="legacy",
                authority_kind="UNVERIFIED",
                authority_version=0,
                idempotency_key=f"legacy-{job_id}",
                request={},
                staging_secret=None,
                status="PENDING",
                current_step="QUEUED",
            )
        )
        await session.commit()
    assert await jobs.process_provisioning_job(str(job_id)) == "SKIPPED_NOT_CLAIMABLE"
    async with get_session_factory()() as session:
        legacy = await session.get(models.ProvisioningJob, job_id)
    assert legacy is not None
    assert legacy.status == "NEEDS_ACTION"
    assert legacy.last_error_code == "PROVISIONING_AUTHORITY_INVALID"
