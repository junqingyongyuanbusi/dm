import asyncio
import uuid
from types import SimpleNamespace

import pytest

from social_reply.application.event_ingestion import xchat_subscription

pytestmark = pytest.mark.integration


async def test_subscription_reconciliation_uses_distributed_advisory_lock(
    migrated_db,
    monkeypatch,
):
    settings = xchat_subscription.get_settings().model_copy(
        update={"testing": False, "xchat_enabled": False, "x_legacy_dm_enabled": True}
    )
    monkeypatch.setattr(xchat_subscription, "get_settings", lambda: settings)
    account = SimpleNamespace(
        id=uuid.uuid4(),
        public_id="primary",
        external_account_id="user-1",
        credential_bundle={
            "consumer_key": "ck",
            "consumer_secret": "cs",
            "access_token": "at",
            "access_token_secret": "ats",
        },
        config={},
        capability={"dm": True, "x_chat": False},
    )
    started = asyncio.Event()
    release = asyncio.Event()
    create_calls = []

    async def fake_accounts(platform):
        return [account]

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def list_subscriptions(self):
            started.set()
            await release.wait()
            return []

        async def list_webhooks(self):
            return [{"id": "webhook-1", "valid": True}]

        async def create_activity_subscription(self, **kwargs):
            create_calls.append(kwargs)
            return {
                "data": {
                    "subscription_id": "subscription-1",
                    "event_type": kwargs["event_type"],
                    "filter": {"user_id": kwargs["user_id"]},
                    "webhook_id": kwargs["webhook_id"],
                }
            }

        async def aclose(self):
            pass

    async def fake_save_activity_state(account_id, states):
        pass

    monkeypatch.setattr(xchat_subscription, "list_active_accounts_by_platform", fake_accounts)
    monkeypatch.setattr(xchat_subscription, "XChatClient", FakeClient)
    monkeypatch.setattr(xchat_subscription, "_save_activity_state", fake_save_activity_state)
    xchat_subscription._last_check_at = None

    first = asyncio.create_task(xchat_subscription.ensure_xchat_subscriptions(force=True))
    await started.wait()
    second = await xchat_subscription.ensure_xchat_subscriptions(force=True)
    release.set()
    first_result = await first

    assert second == []
    assert first_result == ["subscription-1"]
    assert len(create_calls) == 1
