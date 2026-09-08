import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from tests.integration.company_permission_support import create_staff
from tests.integration.test_reauthorization_contract import (
    _REAUTH_OAUTH_CREDENTIALS,
    _fake_x_reauthorization,
    _seed_reauthorization_account,
    _staff_session,
)

from social_reply.application.account_management import jobs
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.secret_crypto import decrypt_secret_bundle

pytestmark = pytest.mark.integration


class CrashAfterApplied(BaseException):
    pass


@pytest.mark.parametrize("recovery", ["sweep", "retry"])
async def test_later_staff_reauthorization_preserves_old_applied_checkpoint_fact(
    migrated_db, monkeypatch, recovery
):
    account_id, _owner_id, grantee_id = await _seed_reauthorization_account()
    principal_a, _token = await _staff_session(grantee_id)
    async with get_session_factory()() as session:
        staff_b = await create_staff(session, tenant_id="default", role="WORKSPACE_ADMIN")
    request = {"environment": "oauth", "external_account_id": "x-42", "public_id": "x_original"}
    common = dict(
        tenant_id="default",
        brand_id="brand-a",
        platform="x",
        operation="REAUTHORIZE",
        target_account_id=account_id,
    )
    job_a = await jobs.submit_provisioning_job(
        **common,
        actor=principal_a.actor,
        admin_session_id=principal_a.session_id,
        expected_config_version=1,
        request={**request, "idempotency_key": f"applied-a-{uuid.uuid4()}"},
        secrets=dict(_REAUTH_OAUTH_CREDENTIALS),
    )
    provider_calls = []

    async def connector(**kwargs):
        provider_calls.append(kwargs["access_token"])
        return await _fake_x_reauthorization(**kwargs)

    monkeypatch.setattr(jobs, "connect_x_account", connector)
    monkeypatch.setattr(jobs, "dispatch_actor", AsyncMock())
    real_connect = jobs._connect

    async def stop_after_applied(job):
        await real_connect(job)
        if recovery == "sweep":
            raise CrashAfterApplied()
        raise RuntimeError("failure after committed credential stage")

    monkeypatch.setattr(jobs, "_connect", stop_after_applied)
    if recovery == "sweep":
        with pytest.raises(CrashAfterApplied):
            await jobs.process_provisioning_job(str(job_a))
    else:
        assert await jobs.process_provisioning_job(str(job_a)) == "FAILED"
    monkeypatch.setattr(jobs, "_connect", real_connect)
    async with get_session_factory()() as session:
        account = await session.get(models.PlatformAccount, account_id)
        assert account.config_version == 2

    credentials_b = {
        **_REAUTH_OAUTH_CREDENTIALS,
        "access_token": "later-authorized-token",
        "access_token_secret": "later-authorized-secret",
    }
    job_b = await jobs.submit_provisioning_job(
        **common,
        actor=staff_b.principal.actor,
        admin_session_id=staff_b.principal.session_id,
        expected_config_version=2,
        request={**request, "idempotency_key": f"applied-b-{uuid.uuid4()}"},
        secrets=credentials_b,
    )
    assert await jobs.process_provisioning_job(str(job_b)) == "NEEDS_ACTION"
    async with get_session_factory()() as session:
        account = await session.get(models.PlatformAccount, account_id)
        assert account.config_version == 3
        assert (
            decrypt_secret_bundle(account.credential_bundle)["access_token"]
            == "later-authorized-token"
        )
        latest_bundle = dict(account.credential_bundle)
        previous = await session.get(models.ProvisioningJob, job_a)
        if recovery == "sweep":
            previous.locked_at = datetime.now(UTC) - timedelta(minutes=10)
        # Also repair markers left by the previous, phase-insensitive cleanup.
        previous.result = {
            **dict(previous.result),
            "requires_secret_resubmission": True,
            "required_secret": "xchat_pin",
        }
        await session.commit()
    if recovery == "sweep":
        assert job_a in await jobs.sweep_provisioning_jobs()
    else:
        await jobs.retry_provisioning_job(job_a, tenant_id="default", caller=principal_a)
    assert await jobs.process_provisioning_job(str(job_a)) == "NEEDS_ACTION"
    assert len(provider_calls) == 2
    async with get_session_factory()() as session:
        account = await session.get(models.PlatformAccount, account_id)
        recovered = await session.get(models.ProvisioningJob, job_a)
        assert account.config_version == 3
        assert account.credential_bundle == latest_bundle
        assert not recovered.result.get("requires_secret_resubmission")
        assert recovered.result.get("required_secret") != "xchat_pin"
        assert not decrypt_secret_bundle(recovered.staging_secret).get("xchat_pin")
        assert recovered.result["configuration_changed_after_provisioning"] is True
        assert recovered.result["checkpoint"]["superseded_by_later_config"] is True
        assert recovered.result["checkpoint"]["applied_credentials_current"] is False
