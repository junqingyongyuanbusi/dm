import uuid

from social_reply.application.account_management.x_credentials import x_credentials
from social_reply.application.platform_accounts import (
    PlatformAccountRuntime,
    get_platform_account_runtime,
)
from social_reply.connectors.base import PlatformSender
from social_reply.connectors.meta.client import MetaGraphClient
from social_reply.connectors.telegram.client import TelegramClient
from social_reply.connectors.whatsapp.client import WhatsAppClient
from social_reply.connectors.x.client import XClient
from social_reply.connectors.xchat.sender import DualXSender, XChatSender

_senders: dict[tuple[str, uuid.UUID, int], PlatformSender] = {}


async def close_platform_senders() -> None:
    senders = list(_senders.values())
    _senders.clear()
    for sender in senders:
        await sender.aclose()


async def get_platform_sender(account_id: uuid.UUID) -> PlatformSender:
    account = await get_platform_account_runtime(account_id)
    key = (account.platform, account.id, account.config_version)
    if key in _senders:
        return _senders[key]
    sender = _build_sender(account)
    stale_keys = [
        cached_key
        for cached_key in _senders
        if cached_key[0] == account.platform and cached_key[1] == account.id and cached_key != key
    ]
    for stale_key in stale_keys:
        await _senders.pop(stale_key).aclose()
    _senders[key] = sender
    return sender


def _build_sender(account: PlatformAccountRuntime) -> PlatformSender:
    credentials = account.credential_bundle
    if account.platform == "telegram":
        return TelegramClient(
            token=credentials.get("bot_token") or credentials["access_token"],
            api_base_url=account.config.get("api_base_url", "https://api.telegram.org"),
        )
    if account.platform in {"facebook", "instagram"}:
        if not account.external_account_id:
            raise LookupError(f"platform_external_account_id_missing:{account.id}")
        return MetaGraphClient(
            platform=account.platform,
            access_token=credentials["access_token"],
            external_account_id=account.external_account_id,
            graph_base_url=account.config.get("graph_base_url", "https://graph.facebook.com"),
            api_version=account.config.get("api_version", "v23.0"),
        )
    if account.platform == "whatsapp":
        if not account.external_account_id:
            raise LookupError(f"platform_external_account_id_missing:{account.id}")
        return WhatsAppClient(
            access_token=credentials["access_token"],
            phone_number_id=account.external_account_id,
            graph_base_url=account.config.get("graph_base_url", "https://graph.facebook.com"),
            api_version=account.config.get("api_version", "v23.0"),
        )
    if account.platform == "x":
        credentials = x_credentials(account)
        legacy = XClient(
            consumer_key=credentials["consumer_key"],
            consumer_secret=credentials["consumer_secret"],
            access_token=credentials["access_token"],
            access_token_secret=credentials["access_token_secret"],
            api_base_url=account.config.get("api_base_url", "https://api.x.com"),
        )
        private_keys = credentials.get("xchat_private_keys_b64")
        signing_version = credentials.get("xchat_signing_key_version")
        if not private_keys or not signing_version:
            return legacy
        if not account.external_account_id:
            raise LookupError(f"platform_external_account_id_missing:{account.id}")
        return DualXSender(
            legacy=legacy,
            xchat=XChatSender(
                consumer_key=credentials["consumer_key"],
                consumer_secret=credentials["consumer_secret"],
                access_token=credentials["access_token"],
                access_token_secret=credentials["access_token_secret"],
                external_account_id=account.external_account_id,
                private_keys_b64=private_keys,
                signing_key_version=signing_version,
                conversation_key_events=account.config.get("xchat_conversation_key_events"),
                api_base_url=account.config.get("api_base_url", "https://api.x.com"),
            ),
        )
    raise LookupError(f"platform_sender_not_configured:{account.platform}:{account.id}")
