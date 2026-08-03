import asyncio
import uuid

import httpx
import pytest
from sqlalchemy import insert, select

from social_reply.application.account_management import feishu_health
from social_reply.connectors.feishu.client import FeishuBotInfo, FeishuClientError
from social_reply.infrastructure.database import models
from social_reply.infrastructure.secret_crypto import encrypt_secret_bundle
from social_reply.shared.config import get_settings

pytestmark = pytest.mark.integration


async def _account(session, *, app_id: str, bot_open_id: str = "ou_stored") -> uuid.UUID:
    account_id = uuid.uuid4()
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            tenant_id="default",
            brand_id="default",
            platform="feishu",
            name=app_id,
            external_account_id=app_id,
            public_id=f"fs_{uuid.uuid4().hex}",
            credential_bundle=encrypt_secret_bundle({"app_id": app_id, "app_secret": "app-secret"}),
            config={
                "delivery_mode": "direct",
                "feishu_health_status": "READY",
                "feishu_bot_open_id": bot_open_id,
                "feishu_bot_name": "Stored Bot",
                "feishu_bot_activate_status": 2,
            },
            capability={"dm": True, "mentions": True, "max_text_length": 4000},
            automation_default="BOT_DRAFT_ONLY",
            status="active",
        )
    )
    await session.commit()
    return account_id


async def test_feishu_health_isolates_accounts_and_persists_typed_states(session, monkeypatch):
    ready_id = await _account(session, app_id="cli_ready123")
    inactive_id = await _account(session, app_id="cli_inactive1")
    mismatch_id = await _account(session, app_id="cli_mismatch1")
    invalid_id = await _account(session, app_id="cli_invalid12")

    class FakeClient:
        def __init__(self, *, app_id, **_kwargs):
            self.app_id = app_id

        async def tenant_access_token(self):
            if self.app_id == "cli_invalid12":
                raise FeishuClientError("FEISHU_API_10003", retryable=False)
            return "tenant", 7200

        async def get_bot_info(self, _token, *, require_active=True):
            assert require_active is False
            if self.app_id == "cli_inactive1":
                return FeishuBotInfo("ou_stored", "Inactive Bot", 1)
            if self.app_id == "cli_mismatch1":
                return FeishuBotInfo("ou_changed", "Changed Bot", 2)
            return FeishuBotInfo("ou_stored", "Ready Bot", 2)

        async def aclose(self):
            return None

    settings = get_settings().model_copy(update={"feishu_enabled": True})
    monkeypatch.setattr(feishu_health, "get_settings", lambda: settings)
    monkeypatch.setattr(feishu_health, "FeishuClient", FakeClient)

    unhealthy = await feishu_health.reconcile_feishu_account_health(force=True)
    assert set(unhealthy) == {str(inactive_id), str(mismatch_id), str(invalid_id)}

    session.expire_all()
    accounts = {
        account.id: account
        for account in (await session.execute(select(models.PlatformAccount))).scalars()
        if account.id in {ready_id, inactive_id, mismatch_id, invalid_id}
    }
    assert accounts[ready_id].config["feishu_health_status"] == "READY"
    assert accounts[ready_id].config["feishu_bot_name"] == "Ready Bot"
    assert accounts[inactive_id].config["feishu_health_status"] == "BOT_NOT_ACTIVE"
    assert accounts[inactive_id].config["feishu_health_error_code"] == "FEISHU_BOT_NOT_ACTIVATED"
    assert accounts[mismatch_id].config["feishu_health_status"] == "BOT_ID_MISMATCH"
    assert accounts[mismatch_id].config["feishu_bot_open_id"] == "ou_stored"
    assert accounts[invalid_id].config["feishu_health_status"] == "CREDENTIAL_INVALID"
    assert accounts[invalid_id].config["feishu_health_error_code"] == "FEISHU_API_10003"
    assert all(account.config["feishu_health_checked_at"] for account in accounts.values())


async def test_feishu_health_discards_result_after_config_version_rotation(session, monkeypatch):
    account_id = await _account(session, app_id="cli_rotate123")
    check_started = asyncio.Event()
    release_check = asyncio.Event()

    class BlockingClient:
        def __init__(self, **_kwargs):
            pass

        async def tenant_access_token(self):
            return "tenant", 7200

        async def get_bot_info(self, _token, *, require_active=True):
            assert require_active is False
            check_started.set()
            await release_check.wait()
            return FeishuBotInfo("ou_stored", "Stale Bot", 1)

        async def aclose(self):
            return None

    monkeypatch.setattr(feishu_health, "FeishuClient", BlockingClient)
    reconciliation = asyncio.create_task(feishu_health.reconcile_feishu_account_health(force=True))
    await check_started.wait()
    rotated_config = {
        "delivery_mode": "direct",
        "feishu_health_status": "ROTATED",
        "feishu_bot_open_id": "ou_rotated",
    }
    await session.execute(
        models.PlatformAccount.__table__.update()
        .where(models.PlatformAccount.id == account_id)
        .values(config=rotated_config, config_version=2)
    )
    await session.commit()
    release_check.set()

    assert await reconciliation == []
    session.expire_all()
    account = await session.get(models.PlatformAccount, account_id)
    assert account.config_version == 2
    assert account.config == rotated_config


async def test_transient_feishu_health_failure_preserves_account_and_bot_identity(
    session, monkeypatch
):
    account_id = await _account(session, app_id="cli_network12")

    class NetworkClient:
        def __init__(self, **_kwargs):
            pass

        async def tenant_access_token(self):
            raise httpx.ConnectError("offline")

        async def aclose(self):
            return None

    monkeypatch.setattr(feishu_health, "FeishuClient", NetworkClient)
    unhealthy = await feishu_health.reconcile_feishu_account_health(force=True)
    assert unhealthy == [str(account_id)]

    session.expire_all()
    account = await session.get(models.PlatformAccount, account_id)
    assert account.status == "active"
    assert account.external_account_id == "cli_network12"
    assert account.config["feishu_health_status"] == "ERROR"
    assert account.config["feishu_bot_open_id"] == "ou_stored"
    assert account.config["feishu_bot_name"] == "Stored Bot"
