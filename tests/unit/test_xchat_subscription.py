import uuid
from types import SimpleNamespace

from social_reply.application.event_ingestion import xchat_subscription


def test_created_subscription_requires_full_expected_contract():
    valid = {
        "data": {
            "subscription_id": "sub-1",
            "event_type": "dm.received",
            "filter": {"user_id": "user-1"},
            "webhook_id": "webhook-1",
        }
    }
    assert (
        xchat_subscription._created_subscription(
            valid,
            event_type="dm.received",
            user_id="user-1",
            webhook_id="webhook-1",
        )["subscription_id"]
        == "sub-1"
    )
    assert (
        xchat_subscription._created_subscription(
            {"data": {**valid["data"], "webhook_id": "wrong"}},
            event_type="dm.received",
            user_id="user-1",
            webhook_id="webhook-1",
        )
        == {}
    )
    assert (
        xchat_subscription._created_subscription(
            {"data": {"event_type": "dm.received"}},
            event_type="dm.received",
            user_id="user-1",
            webhook_id="webhook-1",
        )
        == {}
    )


def test_has_received_subscription_matches_user():
    subscriptions = [
        {
            "event_type": "chat.received",
            "filter": {"user_id": "user-1"},
        }
    ]
    assert xchat_subscription._has_received_subscription(subscriptions, "user-1")
    assert not xchat_subscription._has_received_subscription(subscriptions, "user-2")


async def test_subscription_reconciliation_keeps_legacy_when_xchat_not_registered(monkeypatch):
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
    created_event_types = []
    saved_states = []

    async def fake_accounts(platform):
        return [account]

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def list_subscriptions(self):
            return []

        async def list_webhooks(self):
            return [{"id": "webhook-1", "valid": True}]

        async def get_user_public_keys(self, user_id):
            return []

        async def create_activity_subscription(self, **kwargs):
            created_event_types.append(kwargs["event_type"])
            return {
                "data": {
                    "subscription_id": f"subscription-{kwargs['event_type']}",
                    "event_type": kwargs["event_type"],
                    "filter": {"user_id": kwargs["user_id"]},
                    "webhook_id": kwargs["webhook_id"],
                }
            }

        async def aclose(self):
            pass

    async def fake_save_key_state(account_id, state):
        assert state.registered is False

    async def fake_save_activity_state(account_id, states):
        saved_states.append(states)

    monkeypatch.setattr(xchat_subscription, "list_active_accounts_by_platform", fake_accounts)
    monkeypatch.setattr(xchat_subscription, "XChatClient", FakeClient)
    monkeypatch.setattr(xchat_subscription, "_save_xchat_key_state", fake_save_key_state)
    monkeypatch.setattr(xchat_subscription, "_save_activity_state", fake_save_activity_state)
    xchat_subscription._last_check_at = None

    created = await xchat_subscription.ensure_xchat_subscriptions(force=True)

    assert created_event_types == ["dm.received"]
    assert created == ["subscription-dm.received"]
    assert saved_states[0]["dm.received"]["status"] == "ACTIVE"
    assert saved_states[0]["chat.received"]["status"] == "NOT_REQUIRED"


async def test_subscription_reconciliation_subscribes_registered_xchat_without_private_key(
    monkeypatch,
):
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
    created_event_types = []
    key_states = []

    async def fake_accounts(platform):
        return [account]

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def list_subscriptions(self):
            return []

        async def list_webhooks(self):
            return [{"id": "webhook-1", "valid": True}]

        async def get_user_public_keys(self, user_id):
            return [
                {
                    "public_key_version": "7",
                    "public_key": "identity",
                    "signing_public_key": "signing",
                    "juicebox_config": {"tokens": {}},
                }
            ]

        async def create_activity_subscription(self, **kwargs):
            created_event_types.append(kwargs["event_type"])
            return {
                "data": {
                    "subscription": {
                        "subscription_id": f"subscription-{kwargs['event_type']}",
                        "event_type": kwargs["event_type"],
                        "filter": {"user_id": kwargs["user_id"]},
                        "webhook_id": kwargs["webhook_id"],
                    }
                }
            }

        async def aclose(self):
            pass

    async def fake_save_key_state(account_id, state):
        key_states.append(state)

    async def fake_save_activity_state(account_id, states):
        pass

    monkeypatch.setattr(xchat_subscription, "list_active_accounts_by_platform", fake_accounts)
    monkeypatch.setattr(xchat_subscription, "XChatClient", FakeClient)
    monkeypatch.setattr(xchat_subscription, "_save_xchat_key_state", fake_save_key_state)
    monkeypatch.setattr(xchat_subscription, "_save_activity_state", fake_save_activity_state)
    xchat_subscription._last_check_at = None

    created = await xchat_subscription.ensure_xchat_subscriptions(force=True)

    assert created_event_types == ["dm.received", "chat.received"]
    assert created == ["subscription-dm.received", "subscription-chat.received"]
    assert key_states[0].key_state.value == "RECOVERY_REQUIRED"


async def test_subscription_reconciliation_does_not_duplicate_existing_transports(monkeypatch):
    account = SimpleNamespace(
        id=uuid.uuid4(),
        public_id="primary",
        external_account_id="user-1",
        credential_bundle={
            "consumer_key": "ck",
            "consumer_secret": "cs",
            "access_token": "at",
            "access_token_secret": "ats",
            "xchat_private_keys_b64": "private",
            "xchat_signing_key_version": "7",
        },
        config={
            "xchat_registered": True,
            "xchat_key_state": "READY",
            "xchat_last_probed_at": "2099-01-01T00:00:00+00:00",
        },
        capability={"dm": True, "x_chat": True},
    )

    async def fake_accounts(platform):
        return [account]

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def list_subscriptions(self):
            return [
                {
                    "subscription_id": "dm-sub",
                    "event_type": "dm.received",
                    "filter": {"user_id": "user-1"},
                    "webhook_id": "webhook-1",
                },
                {
                    "subscription_id": "chat-sub",
                    "event_type": "chat.received",
                    "filter": {"user_id": "user-1"},
                    "webhook_id": "webhook-1",
                },
            ]

        async def list_webhooks(self):
            return [{"id": "webhook-1", "valid": True}]

        async def create_activity_subscription(self, **kwargs):
            raise AssertionError("existing subscription must not be recreated")

        async def aclose(self):
            pass

    async def fake_save_activity_state(account_id, states):
        assert states["dm.received"]["subscription_id"] == "dm-sub"
        assert states["chat.received"]["subscription_id"] == "chat-sub"

    monkeypatch.setattr(xchat_subscription, "list_active_accounts_by_platform", fake_accounts)
    monkeypatch.setattr(xchat_subscription, "XChatClient", FakeClient)
    monkeypatch.setattr(xchat_subscription, "_save_activity_state", fake_save_activity_state)
    xchat_subscription._last_check_at = None

    assert await xchat_subscription.ensure_xchat_subscriptions() == []
