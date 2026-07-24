from types import SimpleNamespace

from social_reply.application.event_ingestion import xchat_subscription


def test_has_received_subscription_matches_user():
    subscriptions = [
        {
            "event_type": "chat.received",
            "filter": {"user_id": "user-1"},
        }
    ]
    assert xchat_subscription._has_received_subscription(subscriptions, "user-1")
    assert not xchat_subscription._has_received_subscription(subscriptions, "user-2")


async def test_subscription_reconciliation_skips_account_without_keys(monkeypatch):
    account = SimpleNamespace(
        id="account-1",
        public_id="primary",
        external_account_id="user-1",
        credential_bundle={
            "consumer_key": "ck",
            "consumer_secret": "cs",
            "access_token": "at",
            "access_token_secret": "ats",
        },
        config={},
        capability={"x_chat": True},
    )

    async def fake_accounts(platform):
        return [account]

    class UnexpectedClient:
        def __init__(self, **kwargs):
            raise AssertionError("account without XChat keys must not be subscribed")

    monkeypatch.setattr(xchat_subscription, "list_active_accounts_by_platform", fake_accounts)
    monkeypatch.setattr(xchat_subscription, "XChatClient", UnexpectedClient)
    xchat_subscription._last_check_at = None
    assert await xchat_subscription.ensure_xchat_subscriptions() == []


async def test_subscription_reconciliation_creates_missing(monkeypatch):
    account = SimpleNamespace(
        id="account-1",
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
        config={},
        capability={"x_chat": True},
    )

    async def fake_accounts(platform):
        return [account]

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def list_subscriptions(self):
            return []

        async def list_webhooks(self):
            return [{"id": "webhook-1", "valid": True}]

        async def create_received_subscription(self, **kwargs):
            assert kwargs == {
                "user_id": "user-1",
                "webhook_id": "webhook-1",
                "tag": "reply-core:primary",
            }
            return {
                "data": {
                    "subscription": {
                        "subscription_id": "subscription-1",
                        "event_type": "chat.received",
                        "filter": {"user_id": "user-1"},
                    }
                }
            }

        async def aclose(self):
            pass

    monkeypatch.setattr(xchat_subscription, "list_active_accounts_by_platform", fake_accounts)
    monkeypatch.setattr(xchat_subscription, "XChatClient", FakeClient)
    xchat_subscription._last_check_at = None
    assert await xchat_subscription.ensure_xchat_subscriptions() == ["subscription-1"]
