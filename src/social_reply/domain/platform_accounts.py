from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class AccountPlatform(StrEnum):
    TELEGRAM = "telegram"
    FACEBOOK = "facebook"
    INSTAGRAM = "instagram"
    WHATSAPP = "whatsapp"
    X = "x"
    FEISHU = "feishu"


class AccountStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "DISABLED"


class CapabilityKey(StrEnum):
    DM = "dm"
    COMMENTS = "comments"
    SESSION_MESSAGES = "session_messages"
    TEMPLATES = "templates"
    X_CHAT = "x_chat"
    MENTIONS = "mentions"
    MAX_TEXT_LENGTH = "max_text_length"
    QUALITY_RATING = "quality_rating"


@dataclass(frozen=True)
class PlatformCapabilitySpec:
    boolean_keys: frozenset[CapabilityKey]
    max_text_length: int
    metadata_keys: frozenset[CapabilityKey] = frozenset()


PLATFORM_CAPABILITY_SPECS = {
    AccountPlatform.TELEGRAM: PlatformCapabilitySpec(
        boolean_keys=frozenset({CapabilityKey.DM}),
        max_text_length=4096,
    ),
    AccountPlatform.FACEBOOK: PlatformCapabilitySpec(
        boolean_keys=frozenset({CapabilityKey.DM, CapabilityKey.COMMENTS}),
        max_text_length=2000,
    ),
    AccountPlatform.INSTAGRAM: PlatformCapabilitySpec(
        boolean_keys=frozenset({CapabilityKey.DM, CapabilityKey.COMMENTS}),
        max_text_length=1000,
    ),
    AccountPlatform.WHATSAPP: PlatformCapabilitySpec(
        boolean_keys=frozenset(
            {
                CapabilityKey.DM,
                CapabilityKey.SESSION_MESSAGES,
                CapabilityKey.TEMPLATES,
            }
        ),
        max_text_length=4096,
        metadata_keys=frozenset({CapabilityKey.QUALITY_RATING}),
    ),
    AccountPlatform.X: PlatformCapabilitySpec(
        boolean_keys=frozenset({CapabilityKey.DM, CapabilityKey.X_CHAT, CapabilityKey.MENTIONS}),
        max_text_length=280,
    ),
    AccountPlatform.FEISHU: PlatformCapabilitySpec(
        boolean_keys=frozenset({CapabilityKey.DM, CapabilityKey.MENTIONS}),
        max_text_length=4000,
    ),
}

SUPPORTED_ACCOUNT_PLATFORMS = frozenset(platform.value for platform in AccountPlatform)
PROVISIONABLE_ACCOUNT_PLATFORMS = frozenset(
    {
        AccountPlatform.TELEGRAM.value,
        AccountPlatform.FACEBOOK.value,
        AccountPlatform.INSTAGRAM.value,
        AccountPlatform.WHATSAPP.value,
        AccountPlatform.X.value,
        AccountPlatform.FEISHU.value,
    }
)
ACTIVE_ACCOUNT_STATUS = AccountStatus.ACTIVE.value
DISABLED_ACCOUNT_STATUS = AccountStatus.DISABLED.value
LEGACY_CONNECTED_ACCOUNT_STATUS = "CONNECTED"
LEGACY_ACTIVE_ACCOUNT_STATUSES = frozenset({ACTIVE_ACCOUNT_STATUS, LEGACY_CONNECTED_ACCOUNT_STATUS})


@dataclass(frozen=True)
class DestinationCapabilitySpec:
    platforms: frozenset[AccountPlatform]
    capability: CapabilityKey


DIRECT_DESTINATION_CAPABILITIES = {
    "telegram_dm": DestinationCapabilitySpec(
        platforms=frozenset({AccountPlatform.TELEGRAM}),
        capability=CapabilityKey.DM,
    ),
    "meta_messenger_dm": DestinationCapabilitySpec(
        platforms=frozenset({AccountPlatform.FACEBOOK}),
        capability=CapabilityKey.DM,
    ),
    "meta_instagram_dm": DestinationCapabilitySpec(
        platforms=frozenset({AccountPlatform.INSTAGRAM}),
        capability=CapabilityKey.DM,
    ),
    "meta_public_comment": DestinationCapabilitySpec(
        platforms=frozenset({AccountPlatform.FACEBOOK, AccountPlatform.INSTAGRAM}),
        capability=CapabilityKey.COMMENTS,
    ),
    "meta_private_reply": DestinationCapabilitySpec(
        platforms=frozenset({AccountPlatform.FACEBOOK, AccountPlatform.INSTAGRAM}),
        capability=CapabilityKey.COMMENTS,
    ),
    "whatsapp_session_message": DestinationCapabilitySpec(
        platforms=frozenset({AccountPlatform.WHATSAPP}),
        capability=CapabilityKey.SESSION_MESSAGES,
    ),
    "x_dm": DestinationCapabilitySpec(
        platforms=frozenset({AccountPlatform.X}),
        capability=CapabilityKey.DM,
    ),
    "x_chat_message": DestinationCapabilitySpec(
        platforms=frozenset({AccountPlatform.X}),
        capability=CapabilityKey.X_CHAT,
    ),
    "x_post_reply": DestinationCapabilitySpec(
        platforms=frozenset({AccountPlatform.X}),
        capability=CapabilityKey.MENTIONS,
    ),
    "feishu_p2p_reply": DestinationCapabilitySpec(
        platforms=frozenset({AccountPlatform.FEISHU}),
        capability=CapabilityKey.DM,
    ),
    "feishu_group_reply": DestinationCapabilitySpec(
        platforms=frozenset({AccountPlatform.FEISHU}),
        capability=CapabilityKey.MENTIONS,
    ),
}


def account_platform(value: str) -> AccountPlatform:
    try:
        return AccountPlatform(value)
    except ValueError as exc:
        raise ValueError(f"unsupported_platform:{value}") from exc


def canonical_account_status(status: str) -> str:
    if status == LEGACY_CONNECTED_ACCOUNT_STATUS:
        return ACTIVE_ACCOUNT_STATUS
    try:
        return AccountStatus(status).value
    except ValueError as exc:
        raise ValueError(f"unsupported_account_status:{status}") from exc


def is_active_account_status(status: str) -> bool:
    return status in LEGACY_ACTIVE_ACCOUNT_STATUSES


def normalize_account_capability(platform: str, capability: dict[str, Any]) -> dict[str, Any]:
    platform_value = account_platform(platform)
    if not isinstance(capability, dict):
        raise ValueError("invalid_account_capability:not_object")
    spec = PLATFORM_CAPABILITY_SPECS[platform_value]
    allowed_keys = {
        key.value
        for key in spec.boolean_keys | spec.metadata_keys | {CapabilityKey.MAX_TEXT_LENGTH}
    }
    unknown_keys = set(capability) - allowed_keys
    if unknown_keys:
        raise ValueError(
            f"invalid_account_capability:unknown_keys:{','.join(sorted(unknown_keys))}"
        )

    normalized = dict(capability)
    for key in spec.boolean_keys:
        value = normalized.get(key.value, False)
        if type(value) is not bool:
            raise ValueError(f"invalid_account_capability:{key.value}_not_boolean")
        normalized[key.value] = value

    limit = normalized.get(CapabilityKey.MAX_TEXT_LENGTH.value, spec.max_text_length)
    if type(limit) is not int or not 0 < limit <= spec.max_text_length:
        raise ValueError("invalid_account_capability:max_text_length_out_of_range")
    normalized[CapabilityKey.MAX_TEXT_LENGTH.value] = limit
    return normalized


def capability_enabled(capability: dict[str, Any], key: CapabilityKey) -> bool:
    return capability.get(key.value) is True


def capability_text_limit(platform: str, capability: dict[str, Any]) -> int | None:
    try:
        normalized = normalize_account_capability(platform, capability)
    except ValueError:
        return None
    return int(normalized[CapabilityKey.MAX_TEXT_LENGTH.value])
