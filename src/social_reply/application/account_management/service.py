import secrets
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx

from social_reply.application.account_management.meta_app import provision_meta_app
from social_reply.application.account_management.meta_subscription import subscribe_meta_account
from social_reply.application.account_management.provisioning import provision_direct_account
from social_reply.application.account_management.x_app import ensure_x_platform_app
from social_reply.application.account_management.x_credentials import x_credentials
from social_reply.application.account_management.xchat_activation import (
    unlock_account_xchat_keys,
)
from social_reply.application.platform_accounts import (
    get_platform_account_runtime,
    get_platform_account_runtime_by_external_id,
)
from social_reply.connectors.meta.client import MetaGraphClient
from social_reply.connectors.telegram.client import TelegramClient
from social_reply.connectors.x.client import XClient
from social_reply.connectors.xchat.client import XChatClient
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.secret_crypto import encrypt_secret_bundle
from social_reply.shared.config import get_settings

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
    graph_base_url: str | None = None,
    api_version: str = "v23.0",
    instagram_login_mode: str = "facebook_login",
    page_id: str | None = None,
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
    graph_base_url = graph_base_url or (
        "https://graph.instagram.com"
        if platform == "instagram" and instagram_login_mode == "instagram_login"
        else "https://graph.facebook.com"
    )
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
        instagram_login_mode=instagram_login_mode,
        page_id=page_id,
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
        platform_family=(
            "instagram"
            if platform == "instagram" and instagram_login_mode == "instagram_login"
            else "meta"
        ),
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
            **({"page_id": page_id} if page_id else {}),
        },
        capability={
            "dm": enable_dm,
            "comments": enable_comments,
            "max_text_length": 2000 if platform == "facebook" else 1000,
        },
        automation_default=automation_default,
        platform_app_id=platform_app_id,
    )
    subscribed_fields = await subscribe_meta_account(
        platform=platform,
        access_token=access_token,
        external_account_id=(
            page_id
            if platform == "instagram" and instagram_login_mode == "facebook_login" and page_id
            else external_account_id
        ),
        instagram_login_mode=instagram_login_mode,
        graph_base_url=graph_base_url,
        api_version=api_version,
        enable_dm=enable_dm,
        enable_comments=enable_comments,
        transport=transport,
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
            f"账号已自动订阅 webhook 字段：{', '.join(subscribed_fields)}。",
            "上线前完成 App Review / Advanced Access，并将 App 切换为 Live。",
        ),
    )


async def enable_xchat_for_account(*, account_id: uuid.UUID, pin: str) -> None:
    account = await get_platform_account_runtime(account_id)
    if account.platform != "x" or not account.external_account_id:
        raise ValueError("x_account_not_found")
    credentials = x_credentials(account)
    client = XChatClient(
        consumer_key=credentials["consumer_key"],
        consumer_secret=credentials["consumer_secret"],
        access_token=credentials["access_token"],
        access_token_secret=credentials["access_token_secret"],
        api_base_url=(account.config or {}).get("api_base_url", "https://api.x.com"),
    )
    try:
        private_keys, key_version = await unlock_account_xchat_keys(
            client=client,
            user_id=account.external_account_id,
            pin=_require_secret(pin, "xchat_pin"),
        )
    finally:
        await client.aclose()
    credentials["xchat_private_keys_b64"] = private_keys
    credentials["xchat_signing_key_version"] = key_version
    async with get_session_factory()() as session:
        await session.execute(
            models.PlatformAccount.__table__.update()
            .where(models.PlatformAccount.id == account_id)
            .values(
                credential_bundle=encrypt_secret_bundle(credentials),
                config=models.PlatformAccount.config.op("||")({"xchat_enabled": True}),
                capability=models.PlatformAccount.capability.op("||")({"x_chat": True}),
                config_version=models.PlatformAccount.config_version + 1,
            )
        )
        await session.commit()


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
    settings = get_settings()
    if not settings.x_integration_enabled:
        raise ValueError("x_integration_disabled")
    if xchat_pin and xchat_pin.strip() and not settings.xchat_enabled:
        raise ValueError("xchat_disabled")
    environment = _require_secret(environment, "x_environment")
    credentials = {
        "consumer_key": _require_secret(consumer_key, "x_consumer_key"),
        "consumer_secret": _require_secret(consumer_secret, "x_consumer_secret"),
        "access_token": _require_secret(access_token, "x_access_token"),
        "access_token_secret": _require_secret(access_token_secret, "x_access_token_secret"),
    }
    client = XClient(**credentials, api_base_url=api_base_url, transport=transport)
    dm_capable = False
    try:
        me = await client.get_me()
        if settings.x_legacy_dm_enabled or settings.xchat_enabled:
            try:
                await client.read_dm_events(max_results=10)
                dm_capable = True
            except httpx.HTTPStatusError as exc:
                error_type = ""
                try:
                    error_type = str(exc.response.json().get("type") or "")
                except ValueError:
                    pass
                if exc.response.status_code == 403 and error_type.endswith("/oauth1-permissions"):
                    raise ValueError(
                        "x_direct_message_permission_missing: set X App permissions to "
                        "Read and write and Direct message, then re-authorize the account"
                    ) from exc
                raise
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
            private_keys, key_version = await unlock_account_xchat_keys(
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
        credentials["xchat_private_keys_b64"] = existing_xchat_credentials["xchat_private_keys_b64"]
        credentials["xchat_signing_key_version"] = existing_xchat_credentials[
            "xchat_signing_key_version"
        ]
    platform_app_id, app_public_id = await ensure_x_platform_app(
        tenant_id=tenant_id,
        consumer_key=credentials["consumer_key"],
        consumer_secret=credentials["consumer_secret"],
    )
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
            "dm": dm_capable,
            "x_chat": xchat_enabled,
            "mentions": True,
            "max_text_length": 280,
        },
        automation_default=automation_default,
        platform_app_id=platform_app_id,
    )
    return AccountConnectionResult(
        account_id=account_id,
        platform="x",
        external_account_id=external_account_id,
        public_id=resolved_public_id,
        webhook_url=_webhook_url(public_base_url, f"/webhooks/x/{app_public_id}"),
        name=name or me.get("username") or me.get("name") or external_account_id,
        automation_default=automation_default,
        platform_app_id=platform_app_id,
        app_public_id=app_public_id,
        manual_steps=(
            (
                "在 X Developer Portal 注册返回的共享 webhook_url。"
                if settings.x_activity_enabled
                else "X Activity webhook 已关闭；当前仅使用启用中的轮询栈。"
            ),
            (
                "XChat 全局开关已关闭，密钥材料会保留但不会补拉或发送。"
                if not settings.xchat_enabled
                else "XChat 已解锁：scheduler 将通过 Chat API 补拉加密消息。"
                if xchat_enabled
                else "尚未提供 XChat PIN：已迁移到 XChat 的私信仍无法解密；请重新接入并填写 PIN。"
            ),
        ),
    )
