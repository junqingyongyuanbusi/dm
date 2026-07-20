import secrets
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx

from social_reply.application.account_management.meta_app import provision_meta_app
from social_reply.application.account_management.provisioning import provision_direct_account
from social_reply.application.platform_accounts import (
    get_platform_account_runtime,
    get_platform_account_runtime_by_external_id,
)
from social_reply.connectors.meta.client import MetaGraphClient
from social_reply.connectors.telegram.client import TelegramClient
from social_reply.connectors.x.client import XClient
from social_reply.connectors.xchat.client import XChatClient
from social_reply.connectors.xchat.setup import unlock_xchat_private_keys

_AUTOMATION_DEFAULTS = {"BOT_ACTIVE", "BOT_DRAFT_ONLY"}
_META_PLATFORMS = {"facebook", "instagram"}


@dataclass(frozen=True)
class AccountConnectionResult:
    account_id: uuid.UUID
    platform: str
    external_account_id: str
    public_id: str
    webhook_url: str
    name: str
    automation_default: str
    platform_app_id: uuid.UUID | None = None
    app_public_id: str | None = None
    verify_token: str | None = None
    pending_update_count: int | None = None
    last_webhook_error: str | None = None
    manual_steps: tuple[str, ...] = ()


def _validate_automation_default(value: str) -> None:
    if value not in _AUTOMATION_DEFAULTS:
        raise ValueError(f"unsupported_automation_default:{value}")


def _require_secret(value: str, name: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"missing_{name}")
    return cleaned


def _webhook_url(public_base_url: str, path: str) -> str:
    base_url = public_base_url.strip().rstrip("/")
    if not base_url:
        raise ValueError("missing_public_base_url")
    return f"{base_url}{path}"


async def connect_telegram_account(
    *,
    token: str,
    public_base_url: str,
    tenant_id: str = "default",
    brand_id: str = "default",
    name: str | None = None,
    public_id: str | None = None,
    secrets_root: Path = Path(".secrets/accounts"),
    api_base_url: str = "https://api.telegram.org",
    automation_default: str = "BOT_DRAFT_ONLY",
    rotate_webhook_secret: bool = False,
    drop_pending_updates: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AccountConnectionResult:
    """验证 Telegram Bot、幂等落库并将平台 webhook 切到 Reply Core。"""
    _validate_automation_default(automation_default)
    token = _require_secret(token, "telegram_bot_token")
    client = TelegramClient(token=token, api_base_url=api_base_url, transport=transport)
    try:
        me = await client.get_me()
        external_account_id = str(me["id"])
        webhook_secret = secrets.token_urlsafe(32).replace("-", "_")
        account_id, resolved_public_id = await provision_direct_account(
            platform="telegram",
            external_account_id=external_account_id,
            tenant_id=tenant_id,
            brand_id=brand_id,
            name=name or me.get("username") or me.get("first_name") or external_account_id,
            public_id=public_id,
            public_id_prefix="tg",
            secrets_root=secrets_root,
            credential_bundle={"bot_token": token},
            webhook_secret_bundle={"secret": webhook_secret},
            config={"api_base_url": api_base_url},
            capability={"dm": True, "max_text_length": 4096},
            automation_default=automation_default,
            preserve_existing_webhook_secret=not rotate_webhook_secret,
        )
        if not rotate_webhook_secret:
            runtime = await get_platform_account_runtime(account_id)
            webhook_secret = runtime.webhook_secret_bundle["secret"]
        webhook_url = _webhook_url(public_base_url, f"/webhooks/telegram/{resolved_public_id}")
        await client.set_webhook(
            url=webhook_url,
            secret_token=webhook_secret,
            drop_pending_updates=drop_pending_updates,
        )
        webhook_info = await client.get_webhook_info()
    finally:
        await client.aclose()

    return AccountConnectionResult(
        account_id=account_id,
        platform="telegram",
        external_account_id=external_account_id,
        public_id=resolved_public_id,
        webhook_url=webhook_url,
        name=name or me.get("username") or me.get("first_name") or external_account_id,
        automation_default=automation_default,
        pending_update_count=webhook_info.get("pending_update_count", 0),
        last_webhook_error=webhook_info.get("last_error_message"),
    )


async def connect_meta_account(
    *,
    platform: str,
    external_account_id: str,
    access_token: str,
    app_secret: str,
    public_base_url: str,
    verify_token: str,
    app_id: str | None = None,
    app_public_id: str | None = None,
    tenant_id: str = "default",
    brand_id: str = "default",
    name: str | None = None,
    app_name: str | None = None,
    public_id: str | None = None,
    secrets_root: Path = Path(".secrets/accounts"),
    graph_base_url: str = "https://graph.facebook.com",
    api_version: str = "v23.0",
    instagram_login_mode: str = "facebook_login",
    enable_dm: bool = True,
    enable_comments: bool = True,
    automation_default: str = "BOT_DRAFT_ONLY",
    transport: httpx.AsyncBaseTransport | None = None,
) -> AccountConnectionResult:
    """连接 Facebook Page 或 Instagram 账号，并复用账号所属的 Meta App。"""
    if platform not in _META_PLATFORMS:
        raise ValueError(f"unsupported_meta_platform:{platform}")
    if instagram_login_mode not in {"facebook_login", "instagram_login"}:
        raise ValueError(f"unsupported_instagram_login_mode:{instagram_login_mode}")
    if not enable_dm and not enable_comments:
        raise ValueError("meta_account_requires_dm_or_comments")
    _validate_automation_default(automation_default)
    external_account_id = _require_secret(external_account_id, "external_account_id")
    access_token = _require_secret(access_token, "meta_access_token")
    app_secret = _require_secret(app_secret, "meta_app_secret")
    client = MetaGraphClient(
        platform=platform,
        access_token=access_token,
        external_account_id=external_account_id,
        graph_base_url=graph_base_url,
        api_version=api_version,
        transport=transport,
    )
    try:
        profile = await client.get_account()
    finally:
        await client.aclose()
    if str(profile.get("id")) != external_account_id:
        raise ValueError("meta_token_account_mismatch")

    platform_app_id, resolved_app_public_id, resolved_verify_token = await provision_meta_app(
        tenant_id=tenant_id,
        app_id=app_id,
        app_public_id=app_public_id,
        app_name=app_name,
        app_secret=app_secret,
        verify_token=verify_token,
        secrets_root=secrets_root,
        graph_base_url=graph_base_url,
        api_version=api_version,
    )
    # Meta App 与账号分两次幂等 upsert：账号落库失败时 App 仍可安全复用，
    # 但不向调用方返回成功；下次请求会使用相同 app_id/public_id 收敛。
    account_id, resolved_public_id = await provision_direct_account(
        platform=platform,
        external_account_id=external_account_id,
        tenant_id=tenant_id,
        brand_id=brand_id,
        name=name or profile.get("name") or external_account_id,
        public_id=public_id,
        public_id_prefix="fb" if platform == "facebook" else "ig",
        secrets_root=secrets_root,
        credential_bundle={"access_token": access_token},
        webhook_secret_bundle=None,
        config={
            "graph_base_url": graph_base_url,
            "api_version": api_version,
            "instagram_login_mode": instagram_login_mode,
        },
        capability={
            "dm": enable_dm,
            "comments": enable_comments,
            "max_text_length": 2000 if platform == "facebook" else 1000,
        },
        automation_default=automation_default,
        platform_app_id=platform_app_id,
    )
    return AccountConnectionResult(
        account_id=account_id,
        platform=platform,
        external_account_id=external_account_id,
        public_id=resolved_public_id,
        webhook_url=_webhook_url(public_base_url, f"/webhooks/meta/{resolved_app_public_id}"),
        name=name or profile.get("name") or external_account_id,
        automation_default=automation_default,
        platform_app_id=platform_app_id,
        app_public_id=resolved_app_public_id,
        verify_token=resolved_verify_token,
        manual_steps=(
            "在 Meta App Dashboard 配置返回的 webhook_url 与 verify_token。",
            "为账号订阅 messages/comments 等所需字段，并完成 App Review/Advanced Access。",
        ),
    )


async def connect_x_account(
    *,
    consumer_key: str,
    consumer_secret: str,
    access_token: str,
    access_token_secret: str,
    public_base_url: str,
    environment: str,
    xchat_pin: str | None = None,
    tenant_id: str = "default",
    brand_id: str = "default",
    name: str | None = None,
    public_id: str | None = None,
    secrets_root: Path = Path(".secrets/accounts"),
    api_base_url: str = "https://api.x.com",
    automation_default: str = "BOT_DRAFT_ONLY",
    transport: httpx.AsyncBaseTransport | None = None,
) -> AccountConnectionResult:
    """验证 X OAuth 1.0a 凭证并登记 Account Activity webhook 路由。"""
    _validate_automation_default(automation_default)
    environment = _require_secret(environment, "x_environment")
    credentials = {
        "consumer_key": _require_secret(consumer_key, "x_consumer_key"),
        "consumer_secret": _require_secret(consumer_secret, "x_consumer_secret"),
        "access_token": _require_secret(access_token, "x_access_token"),
        "access_token_secret": _require_secret(access_token_secret, "x_access_token_secret"),
    }
    client = XClient(**credentials, api_base_url=api_base_url, transport=transport)
    try:
        me = await client.get_me()
    finally:
        await client.aclose()
    external_account_id = str(me["id"])
    existing_runtime = None
    try:
        existing_runtime = await get_platform_account_runtime_by_external_id(
            tenant_id=tenant_id,
            platform="x",
            external_account_id=external_account_id,
        )
    except LookupError:
        pass
    existing_xchat_credentials = (
        existing_runtime.credential_bundle if existing_runtime is not None else {}
    )
    xchat_enabled = bool(existing_xchat_credentials.get("xchat_private_keys_b64"))
    if xchat_pin and xchat_pin.strip():
        xchat = XChatClient(**credentials, api_base_url=api_base_url, transport=transport)
        try:
            private_keys, key_version = await unlock_xchat_private_keys(
                client=xchat,
                user_id=external_account_id,
                pin=xchat_pin.strip(),
            )
        finally:
            await xchat.aclose()
        credentials["xchat_private_keys_b64"] = private_keys
        credentials["xchat_signing_key_version"] = key_version
        xchat_enabled = True
    elif xchat_enabled:
        credentials["xchat_private_keys_b64"] = existing_xchat_credentials[
            "xchat_private_keys_b64"
        ]
        credentials["xchat_signing_key_version"] = existing_xchat_credentials[
            "xchat_signing_key_version"
        ]
    account_id, resolved_public_id = await provision_direct_account(
        platform="x",
        external_account_id=external_account_id,
        tenant_id=tenant_id,
        brand_id=brand_id,
        name=name or me.get("username") or me.get("name") or external_account_id,
        public_id=public_id,
        public_id_prefix="x",
        secrets_root=secrets_root,
        credential_bundle=credentials,
        webhook_secret_bundle={"consumer_secret": credentials["consumer_secret"]},
        config={
            **(existing_runtime.config if existing_runtime is not None else {}),
            "api_base_url": api_base_url,
            "environment": environment,
            "xchat_enabled": xchat_enabled,
        },
        capability={
            "dm": True,
            "x_chat": xchat_enabled,
            "mentions": True,
            "max_text_length": 280,
        },
        automation_default=automation_default,
    )
    return AccountConnectionResult(
        account_id=account_id,
        platform="x",
        external_account_id=external_account_id,
        public_id=resolved_public_id,
        webhook_url=_webhook_url(public_base_url, f"/webhooks/x/{resolved_public_id}"),
        name=name or me.get("username") or me.get("name") or external_account_id,
        automation_default=automation_default,
        manual_steps=(
            "在 X Developer Portal 注册返回的 webhook_url，并保留 legacy 订阅。",
            (
                "XChat 已解锁：scheduler 将通过 Chat API 补拉加密消息。"
                if xchat_enabled
                else "尚未提供 XChat PIN：已迁移到 XChat 的私信仍无法解密；请重新接入并填写 PIN。"
            ),
        ),
    )
