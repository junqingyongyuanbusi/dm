import uuid

from social_reply.application.account_management.x_credentials import x_credentials
from social_reply.application.platform_accounts import (
    PlatformAccountRuntime,
    get_platform_account_runtime,
    get_platform_app_runtime,
)
from social_reply.connectors.base import PlatformSender
from social_reply.connectors.meta.client import MetaGraphClient
from social_reply.connectors.telegram.client import TelegramClient
from social_reply.connectors.whatsapp.client import WhatsAppClient
from social_reply.connectors.x.client import XClient
from social_reply.connectors.xchat.sender import DualXSender, XChatSender
from social_reply.shared.config import get_settings

_senders: dict[tuple[str, uuid.UUID, int, int], PlatformSender] = {}


async def close_platform_senders() -> None:
    senders = list(_senders.values())
    _senders.clear()
    for sender in senders:
        await sender.aclose()


async def get_platform_sender(account_id: uuid.UUID) -> PlatformSender:
    account = await get_platform_account_runtime(account_id)
    meta_app_secret = None
    meta_app_version = 0
    if account.platform in {"facebook", "instagram"}:
        if account.platform_app_id is None:
            raise LookupError(f"platform_app_missing:{account.id}")
        app = await get_platform_app_runtime(account.platform_app_id)
        expected_family = (
            "instagram"
            if account.platform == "instagram"
            and account.config.get("instagram_login_mode") == "instagram_login"
            else "meta"
        )
        if app.tenant_id != account.tenant_id or app.platform_family != expected_family:
            raise LookupError(f"platform_app_scope_mismatch:{account.id}")
        meta_app_secret = app.credential_bundle["app_secret"]
        meta_app_version = app.config_version
    key = (account.platform, account.id, account.config_version, meta_app_version)
    if key in _senders:
        return _senders[key]
    sender = _build_sender(account, meta_app_secret=meta_app_secret)
    stale_keys = [
        cached_key
        for cached_key in _senders
        if cached_key[0] == account.platform and cached_key[1] == account.id and cached_key != key
    ]
    for stale_key in stale_keys:
        await _senders.pop(stale_key).aclose()
    _senders[key] = sender
    return sender


def _build_sender(
    account: PlatformAccountRuntime,
    *,
    meta_app_secret: str | None = None,
) -> PlatformSender:
    credentials = account.credential_bundle
    if account.platform == "telegram":
        return TelegramClient(
            token=credentials.get("bot_token") or credentials["access_token"],
            api_base_url=account.config.get("api_base_url", "https://api.telegram.org"),
        )
    if account.platform in {"facebook", "instagram"}:
        if not account.external_account_id:
            raise LookupError(f"platform_external_account_id_missing:{account.id}")
        if not meta_app_secret:
            raise LookupError(f"platform_app_secret_missing:{account.id}")
        return MetaGraphClient(
            platform=account.platform,
            access_token=credentials["access_token"],
            app_secret=meta_app_secret,
            external_account_id=account.external_account_id,
            graph_base_url=account.config.get("graph_base_url", "https://graph.facebook.com"),
            api_version=account.config.get("api_version", "v23.0"),
            instagram_login_mode=account.config.get("instagram_login_mode", "facebook_login"),
            page_id=account.config.get("page_id"),
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
        if not get_settings().xchat_enabled:
            return legacy
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
