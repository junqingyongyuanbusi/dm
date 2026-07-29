import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

from social_reply.application.account_management.meta_app import provision_meta_app
from social_reply.application.account_management.meta_subscription import (
    meta_app_subscription_fields,
    meta_app_subscription_object,
    meta_subscription_fields,
    reconcile_meta_app_subscription,
    subscribe_meta_account,
)
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
from social_reply.connectors.xchat.state import (
    XChatKeyState,
    XChatState,
    classify_xchat_state,
    xchat_state_config,
)
from social_reply.domain.platform_accounts import (
    ACTIVE_ACCOUNT_STATUS,
    DISABLED_ACCOUNT_STATUS,
)
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.queue.dispatch import dispatch_actor
from social_reply.infrastructure.secret_crypto import encrypt_secret_bundle
from social_reply.shared.config import get_settings

logger = logging.getLogger(__name__)

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


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _meta_provider_error_code(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            payload = exc.response.json()
        except ValueError:
            payload = {}
        error = payload.get("error") if isinstance(payload, dict) else None
        code = error.get("code") if isinstance(error, dict) else None
        return f"META_HTTP_{exc.response.status_code}_{code or 'UNKNOWN'}"
    if isinstance(exc, httpx.TimeoutException):
        return "META_TIMEOUT"
    if isinstance(exc, httpx.TransportError):
        return "META_TRANSPORT_ERROR"
    return f"META_{exc.__class__.__name__.upper()}"


async def _xchat_public_keys(client: XChatClient, user_id: str) -> list[dict]:
    try:
        return await client.get_user_public_keys(user_id)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return []
        raise


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
    enable_comments: bool = False,
    automation_default: str = "BOT_DRAFT_ONLY",
    transport: httpx.AsyncBaseTransport | None = None,
) -> AccountConnectionResult:
    """连接 Facebook Page 或 Instagram 账号，并复用账号所属的 Meta App。"""
    if platform not in _META_PLATFORMS:
        raise ValueError(f"unsupported_meta_platform:{platform}")
    if not get_settings().platform_integration_enabled(platform):
        raise ValueError(f"{platform}_integration_disabled")
    if instagram_login_mode not in {"facebook_login", "instagram_login"}:
        raise ValueError(f"unsupported_instagram_login_mode:{instagram_login_mode}")
    if platform == "facebook" and instagram_login_mode != "facebook_login":
        raise ValueError("facebook_requires_facebook_login")
    if platform == "instagram" and instagram_login_mode == "facebook_login" and not page_id:
        raise ValueError("instagram_facebook_login_requires_page_id")
    if platform == "instagram" and instagram_login_mode == "instagram_login" and page_id:
        raise ValueError("instagram_login_forbids_page_id")
    graph_base_url = graph_base_url or (
        "https://graph.instagram.com"
        if platform == "instagram" and instagram_login_mode == "instagram_login"
        else "https://graph.facebook.com"
    )
    if not enable_dm:
        raise ValueError("meta_dm_required")
    if enable_comments and not get_settings().meta_comment_reply_enabled:
        raise ValueError("meta_comment_reply_disabled")
    if not get_settings().meta_automation_default_allowed(platform, automation_default):
        raise ValueError("meta_requires_bot_draft_only")
    _validate_automation_default(automation_default)
    external_account_id = _require_secret(external_account_id, "external_account_id")
    access_token = _require_secret(access_token, "meta_access_token")
    app_secret = _require_secret(app_secret, "meta_app_secret")
    client = MetaGraphClient(
        platform=platform,
        access_token=access_token,
        app_secret=app_secret,
        external_account_id=external_account_id,
        graph_base_url=graph_base_url,
        api_version=api_version,
        instagram_login_mode=instagram_login_mode,
        page_id=page_id,
        transport=transport,
    )
    try:
        profile = await client.get_account()
        if str(profile.get("id")) != external_account_id:
            raise ValueError("meta_token_account_mismatch")
        (
            platform_app_id,
            resolved_app_public_id,
            resolved_verify_token,
            external_app_id,
        ) = await provision_meta_app(
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
        if platform == "facebook" and enable_comments:
            await client.require_facebook_comment_permissions(app_id=external_app_id)
        if platform == "instagram" and enable_comments:
            await client.require_instagram_comment_permissions(app_id=external_app_id)
    finally:
        await client.aclose()
    desired_fields = meta_subscription_fields(
        platform=platform,
        enable_dm=enable_dm,
        enable_comments=enable_comments,
        instagram_login_mode=instagram_login_mode,
    )
    desired_app_fields = meta_app_subscription_fields(
        platform=platform,
        enable_dm=enable_dm,
        enable_comments=enable_comments,
    )
    webhook_url = _webhook_url(public_base_url, f"/webhooks/meta/{resolved_app_public_id}")
    account_config = {
        "graph_base_url": graph_base_url,
        "api_version": api_version,
        "instagram_login_mode": instagram_login_mode,
        **({"page_id": page_id} if page_id else {}),
        "meta_desired_subscribed_fields": list(desired_fields),
        "meta_desired_app_subscribed_fields": list(desired_app_fields),
        "meta_subscribed_fields": [],
        "meta_health_status": "PROVISIONING",
        "meta_health_checked_at": _utc_now_iso(),
        "meta_health_error_code": None,
    }
    # Route inbound occurrences immediately; delivery remains blocked while provisioning.
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
        config=account_config,
        capability={
            "dm": enable_dm,
            "comments": enable_comments,
            "max_text_length": 2000 if platform == "facebook" else 1000,
        },
        automation_default=automation_default,
        platform_app_id=platform_app_id,
        status=ACTIVE_ACCOUNT_STATUS,
    )
    subscription_account_id = (
        page_id
        if platform == "instagram" and instagram_login_mode == "facebook_login"
        else external_account_id
    )
    try:
        # App-level first: without it Meta drops every event, so a failure here means the
        # account would look connected while staying deaf.
        app_subscribed_fields = await reconcile_meta_app_subscription(
            app_id=external_app_id,
            app_secret=app_secret,
            object_type=meta_app_subscription_object(platform),
            desired_fields=desired_app_fields,
            callback_url=webhook_url,
            verify_token=resolved_verify_token,
            api_version=api_version,
            transport=transport,
        )
        subscribed_fields = await subscribe_meta_account(
            platform=platform,
            access_token=access_token,
            app_secret=app_secret,
            external_account_id=subscription_account_id,
            instagram_login_mode=instagram_login_mode,
            graph_base_url=graph_base_url,
            api_version=api_version,
            enable_dm=enable_dm,
            enable_comments=enable_comments,
            transport=transport,
        )
    except Exception as exc:
        async with get_session_factory()() as session:
            await session.execute(
                models.PlatformAccount.__table__.update()
                .where(models.PlatformAccount.id == account_id)
                .values(
                    status=DISABLED_ACCOUNT_STATUS,
                    config=models.PlatformAccount.config.op("||")(
                        {
                            "meta_health_status": "ERROR",
                            "meta_health_checked_at": _utc_now_iso(),
                            "meta_health_error_code": _meta_provider_error_code(exc),
                        }
                    ),
                    config_version=models.PlatformAccount.config_version + 1,
                )
            )
            await session.commit()
        raise
    async with get_session_factory()() as session:
        await session.execute(
            models.PlatformAccount.__table__.update()
            .where(models.PlatformAccount.id == account_id)
            .values(
                status=ACTIVE_ACCOUNT_STATUS,
                config=models.PlatformAccount.config.op("||")(
                    {
                        "meta_subscribed_fields": list(subscribed_fields),
                        "meta_app_subscribed_fields": list(app_subscribed_fields),
                        "meta_health_status": "READY",
                        "meta_health_checked_at": _utc_now_iso(),
                        "meta_health_error_code": None,
                    }
                ),
                config_version=models.PlatformAccount.config_version + 1,
            )
        )
        await session.commit()
    return AccountConnectionResult(
        account_id=account_id,
        platform=platform,
        external_account_id=external_account_id,
        public_id=resolved_public_id,
        webhook_url=webhook_url,
        name=name or profile.get("name") or external_account_id,
        automation_default=automation_default,
        platform_app_id=platform_app_id,
        app_public_id=resolved_app_public_id,
        verify_token=resolved_verify_token,
        manual_steps=(
            "已自动完成 App 级 Webhook 回调与字段订阅，无需在 App Dashboard 手工配置。",
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
        records = await _xchat_public_keys(client, account.external_account_id)
        private_keys, key_version = await unlock_account_xchat_keys(
            client=client,
            user_id=account.external_account_id,
            pin=_require_secret(pin, "xchat_pin"),
            records=records,
        )
    finally:
        await client.aclose()
    credentials["xchat_private_keys_b64"] = private_keys
    credentials["xchat_signing_key_version"] = key_version
    state_config = xchat_state_config(
        XChatState(
            key_state=XChatKeyState.READY,
            registered=True,
            public_key_version=key_version,
        ),
        probed_at=_utc_now_iso(),
    )
    async with get_session_factory()() as session:
        await session.execute(
            models.PlatformAccount.__table__.update()
            .where(models.PlatformAccount.id == account_id)
            .values(
                credential_bundle=encrypt_secret_bundle(credentials),
                config=models.PlatformAccount.config.op("||")(
                    {"xchat_enabled": True, **state_config}
                ),
                capability=models.PlatformAccount.capability.op("||")({"x_chat": True}),
                config_version=models.PlatformAccount.config_version + 1,
            )
        )
        await session.commit()

    from social_reply.application.event_ingestion.xchat_actors import recover_xchat_account

    try:
        await dispatch_actor(recover_xchat_account, str(account_id))
    except Exception:  # noqa: BLE001 - keys are already committed; scheduler remains the fallback
        logger.exception("failed to dispatch XChat recovery account=%s", account_id)


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
    private_keys = existing_xchat_credentials.get("xchat_private_keys_b64")
    stored_key_version = existing_xchat_credentials.get("xchat_signing_key_version")
    if private_keys:
        credentials["xchat_private_keys_b64"] = private_keys
    if stored_key_version:
        credentials["xchat_signing_key_version"] = stored_key_version

    if settings.xchat_enabled:
        xchat = XChatClient(
            consumer_key=credentials["consumer_key"],
            consumer_secret=credentials["consumer_secret"],
            access_token=credentials["access_token"],
            access_token_secret=credentials["access_token_secret"],
            api_base_url=api_base_url,
            transport=transport,
        )
        try:
            public_key_records = await _xchat_public_keys(xchat, external_account_id)
            if xchat_pin and xchat_pin.strip():
                private_keys, stored_key_version = await unlock_account_xchat_keys(
                    client=xchat,
                    user_id=external_account_id,
                    pin=xchat_pin.strip(),
                    records=public_key_records,
                )
                credentials["xchat_private_keys_b64"] = private_keys
                credentials["xchat_signing_key_version"] = stored_key_version
        finally:
            await xchat.aclose()
        if xchat_pin and xchat_pin.strip():
            xchat_state = XChatState(
                key_state=XChatKeyState.READY,
                registered=True,
                public_key_version=str(stored_key_version),
            )
        else:
            xchat_state = classify_xchat_state(
                public_key_records,
                private_keys_b64=private_keys,
            )
        if xchat_state.key_state is XChatKeyState.READY:
            credentials["xchat_signing_key_version"] = str(xchat_state.public_key_version)
    else:
        existing_config = existing_runtime.config if existing_runtime is not None else {}
        existing_state_value = existing_config.get("xchat_key_state")
        try:
            existing_state = XChatKeyState(str(existing_state_value))
        except ValueError:
            existing_state = (
                XChatKeyState.READY
                if private_keys and stored_key_version
                else XChatKeyState.NOT_REGISTERED
            )
        xchat_state = XChatState(
            key_state=existing_state,
            registered=bool(existing_config.get("xchat_registered", private_keys)),
            public_key_version=existing_config.get("xchat_public_key_version", stored_key_version),
        )
    xchat_ready = xchat_state.key_state is XChatKeyState.READY
    if settings.xchat_enabled:
        xchat_config = xchat_state_config(xchat_state, probed_at=_utc_now_iso())
    else:
        xchat_config = {
            "xchat_registered": xchat_state.registered,
            "xchat_key_state": xchat_state.key_state.value,
            "xchat_public_key_version": xchat_state.public_key_version,
        }
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
            "xchat_enabled": xchat_ready,
            **xchat_config,
        },
        capability={
            "dm": dm_capable,
            "x_chat": xchat_ready,
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
                else "XChat 已解锁：实时 webhook 与 Chat API 补拉均已启用。"
                if xchat_ready
                else "账号已注册 XChat，但需要提交 4 位 PIN 恢复现有密钥。"
                if xchat_state.key_state is XChatKeyState.RECOVERY_REQUIRED
                else "账号尚未注册 XChat；未加密 DM 仍继续通过 legacy 通道处理。"
                if xchat_state.key_state is XChatKeyState.NOT_REGISTERED
                else "XChat 公私钥不匹配或配置不完整；已关闭加密消息发送。"
            ),
        ),
    )
