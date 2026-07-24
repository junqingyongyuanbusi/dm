import pytest

from social_reply.domain.platform_accounts import (
    ACTIVE_ACCOUNT_STATUS,
    DIRECT_DESTINATION_CAPABILITIES,
    AccountPlatform,
    CapabilityKey,
    account_platform,
    canonical_account_status,
    capability_enabled,
    normalize_account_capability,
)


def test_account_platform_and_legacy_status_are_canonicalized():
    assert account_platform("telegram") is AccountPlatform.TELEGRAM
    assert canonical_account_status("CONNECTED") == ACTIVE_ACCOUNT_STATUS
    assert canonical_account_status("DISABLED") == "DISABLED"


@pytest.mark.parametrize("value", ["meta", "", "TELEGRAM"])
def test_unknown_account_platform_is_rejected(value):
    with pytest.raises(ValueError, match="unsupported_platform"):
        account_platform(value)


def test_capability_defaults_are_platform_specific_and_strict():
    capability = normalize_account_capability("x", {"dm": True})

    assert capability == {
        "dm": True,
        "x_chat": False,
        "mentions": False,
        "max_text_length": 280,
    }
    assert capability_enabled(capability, CapabilityKey.DM) is True
    assert capability_enabled(capability, CapabilityKey.X_CHAT) is False


@pytest.mark.parametrize(
    "capability,error",
    [
        ({"dm": "false"}, "dm_not_boolean"),
        ({"dm": True, "max_text_length": 281}, "max_text_length_out_of_range"),
        ({"dm": True, "unknown": True}, "unknown_keys"),
    ],
)
def test_invalid_capability_is_rejected(capability, error):
    with pytest.raises(ValueError, match=error):
        normalize_account_capability("x", capability)


def test_destination_capabilities_bind_routes_to_platforms():
    x_dm = DIRECT_DESTINATION_CAPABILITIES["x_dm"]

    assert x_dm.platforms == frozenset({AccountPlatform.X})
    assert x_dm.capability is CapabilityKey.DM
