import httpx
import pytest

from social_reply.application.account_management import xchat_activation
from social_reply.application.account_management.xchat_activation import (
    XChatActivationError,
    unlock_account_xchat_keys,
)
from social_reply.connectors.xchat.setup import (
    XChatKeyConfigurationError,
    XChatKeyUnlockError,
    _unlock_error_reason,
    unlock_xchat_private_keys,
)


async def test_xchat_activation_explains_missing_dm_permission(monkeypatch):
    async def fail(**kwargs):
        request = httpx.Request("GET", "https://api.x.com/2/users/1/public_keys")
        response = httpx.Response(
            403,
            request=request,
            json={"type": "https://api.x.com/2/problems/oauth1-permissions"},
        )
        raise httpx.HTTPStatusError("forbidden", request=request, response=response)

    monkeypatch.setattr(xchat_activation, "unlock_xchat_private_keys", fail)

    with pytest.raises(XChatActivationError) as caught:
        await unlock_account_xchat_keys(client=object(), user_id="1", pin="1234")

    assert caught.value.code == "XCHAT_DM_PERMISSION_REQUIRED"
    assert "Read and write and Direct message" in caught.value.operator_message
    assert caught.value.status_code == 422
    assert "1234" not in caught.value.operator_message


async def test_xchat_activation_requires_reauthorization_on_401(monkeypatch):
    async def fail(**kwargs):
        request = httpx.Request("GET", "https://api.x.com/2/users/1/public_keys")
        response = httpx.Response(401, request=request)
        raise httpx.HTTPStatusError("unauthorized", request=request, response=response)

    monkeypatch.setattr(xchat_activation, "unlock_xchat_private_keys", fail)

    with pytest.raises(XChatActivationError) as caught:
        await unlock_account_xchat_keys(client=object(), user_id="1", pin="1234")

    assert caught.value.code == "XCHAT_REAUTHORIZATION_REQUIRED"


async def test_xchat_activation_classifies_all_transport_errors(monkeypatch):
    async def fail(**kwargs):
        request = httpx.Request("GET", "https://api.x.com/2/users/1/public_keys")
        raise httpx.RemoteProtocolError("server disconnected", request=request)

    monkeypatch.setattr(xchat_activation, "unlock_xchat_private_keys", fail)

    with pytest.raises(XChatActivationError) as caught:
        await unlock_account_xchat_keys(client=object(), user_id="1", pin="1234")

    assert caught.value.code == "XCHAT_API_UNAVAILABLE"
    assert caught.value.status_code == 503
    assert caught.value.retryable is True


async def test_xchat_activation_explains_missing_public_keys(monkeypatch):
    async def fail(**kwargs):
        raise ValueError("xchat_public_keys_not_found")

    monkeypatch.setattr(xchat_activation, "unlock_xchat_private_keys", fail)

    with pytest.raises(XChatActivationError) as caught:
        await unlock_account_xchat_keys(client=object(), user_id="1", pin="1234")

    assert caught.value.code == "XCHAT_NOT_ENABLED"
    assert "XChat" in caught.value.operator_message


async def test_xchat_activation_sanitizes_pin_recovery_failure(monkeypatch):
    async def fail(**kwargs):
        raise XChatKeyUnlockError("recovery_failed")

    monkeypatch.setattr(xchat_activation, "unlock_xchat_private_keys", fail)

    with pytest.raises(XChatActivationError) as caught:
        await unlock_account_xchat_keys(client=object(), user_id="1", pin="1234")

    assert caught.value.code == "XCHAT_PIN_RECOVERY_FAILED"
    assert "1234" not in caught.value.operator_message
    assert str(caught.value) == "XCHAT_PIN_RECOVERY_FAILED"


@pytest.mark.parametrize(
    ("sdk_message", "reason"),
    [
        ("Juicebox error: Invalid PIN", "invalid_pin"),
        ("Juicebox error: Keys not registered", "not_registered"),
        ("Juicebox error: Invalid auth token", "invalid_auth"),
        ("Juicebox error: Upgrade required - SDK version too old", "upgrade_required"),
        ("Juicebox error: Rate limit exceeded", "rate_limited"),
        ("Juicebox error: Transient error - retry", "temporarily_unavailable"),
        ("Juicebox error: Storage failed", "temporarily_unavailable"),
    ],
)
def test_xchat_setup_classifies_stable_sdk_errors(sdk_message, reason):
    assert _unlock_error_reason(sdk_message) == reason


async def test_xchat_setup_separates_juicebox_configuration_errors(monkeypatch):
    class FakeClient:
        async def get_user_public_keys(self, user_id):
            return [
                {
                    "juicebox_config": {"token_map": {}},
                    "public_key_version": "1",
                }
            ]

    class InvalidConfigChat:
        def __init__(self, config):
            raise ValueError("Missing token_map or sdk_config")

    monkeypatch.setattr("social_reply.connectors.xchat.setup.Chat", InvalidConfigChat)

    with pytest.raises(XChatKeyConfigurationError):
        await unlock_xchat_private_keys(
            client=FakeClient(),
            user_id="1",
            pin="1234",
        )
