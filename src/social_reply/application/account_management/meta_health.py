import logging
import time
import uuid
from datetime import UTC, datetime

import httpx

from social_reply.application.account_management.meta_subscription import (
    get_meta_app_subscription,
    get_meta_subscription_fields,
    meta_app_subscription_object,
    meta_subscription_fields,
    reconcile_meta_app_subscription,
    subscribe_meta_account,
)
from social_reply.application.platform_accounts import (
    PlatformAccountRuntime,
    get_platform_app_runtime,
    list_active_accounts_by_platform,
)
from social_reply.connectors.meta.client import MetaGraphClient
from social_reply.domain.platform_accounts import CapabilityKey, capability_enabled
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.shared.config import get_settings

logger = logging.getLogger(__name__)
_last_check_at: float | None = None


def _health_error_code(exc: Exception) -> str:
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


def _subscription_account_id(account: PlatformAccountRuntime) -> str:
    if (
        account.platform == "instagram"
        and account.config.get("instagram_login_mode") == "facebook_login"
    ):
        page_id = str(account.config.get("page_id") or "")
        if not page_id:
            raise ValueError("instagram_facebook_login_requires_page_id")
        return page_id
    if not account.external_account_id:
        raise ValueError("meta_external_account_id_missing")
    return account.external_account_id


async def _save_health(
    account_id: uuid.UUID,
    *,
    status: str,
    subscribed_fields: tuple[str, ...] = (),
    app_subscribed_fields: tuple[str, ...] = (),
    error_code: str | None = None,
) -> None:
    async with get_session_factory()() as session:
        await session.execute(
            models.PlatformAccount.__table__.update()
            .where(models.PlatformAccount.id == account_id)
            .values(
                config=models.PlatformAccount.config.op("||")(
                    {
                        "meta_health_status": status,
                        "meta_health_checked_at": datetime.now(UTC).isoformat(),
                        "meta_health_error_code": error_code,
                        "meta_subscribed_fields": list(subscribed_fields),
                        "meta_app_subscribed_fields": list(app_subscribed_fields),
                    }
                )
            )
        )
        await session.commit()


async def _reconcile_app_subscription(
    *,
    app_external_id: str,
    app_public_id: str,
    app_secret: str,
    verify_token: str,
    platform: str,
    desired: tuple[str, ...],
    api_version: str,
) -> tuple[str, ...]:
    """Keep the app-level Webhooks product in sync; account-level alone delivers nothing."""
    object_type = meta_app_subscription_object(platform)
    current = await get_meta_app_subscription(
        app_id=app_external_id,
        app_secret=app_secret,
        object_type=object_type,
        api_version=api_version,
    )
    if current is not None and current.active and set(desired).issubset(current.fields):
        return current.fields
    if not verify_token:
        raise ValueError("meta_app_verify_token_missing")
    callback_url = f"{get_settings().public_base_url.rstrip('/')}/webhooks/meta/{app_public_id}"
    return await reconcile_meta_app_subscription(
        app_id=app_external_id,
        app_secret=app_secret,
        object_type=object_type,
        desired_fields=desired,
        callback_url=callback_url,
        verify_token=verify_token,
        api_version=api_version,
    )


async def _check_account(account: PlatformAccountRuntime) -> str | None:
    if account.platform_app_id is None or not account.external_account_id:
        await _save_health(
            account.id,
            status="ERROR",
            error_code="META_ACCOUNT_CONFIGURATION_INVALID",
        )
        return str(account.id)
    app = await get_platform_app_runtime(account.platform_app_id)
    expected_family = (
        "instagram"
        if account.platform == "instagram"
        and account.config.get("instagram_login_mode") == "instagram_login"
        else "meta"
    )
    if app.tenant_id != account.tenant_id or app.platform_family != expected_family:
        await _save_health(
            account.id,
            status="ERROR",
            error_code="META_APP_SCOPE_MISMATCH",
        )
        return str(account.id)
    if not app.external_app_id:
        await _save_health(
            account.id,
            status="ERROR",
            error_code="META_APP_ID_MISSING",
        )
        return str(account.id)
    credentials = account.credential_bundle
    app_credentials = app.credential_bundle
    access_token = credentials["access_token"]
    app_secret = app_credentials["app_secret"]
    login_mode = str(account.config.get("instagram_login_mode") or "facebook_login")
    graph_base_url = str(
        account.config.get("graph_base_url")
        or (
            "https://graph.instagram.com"
            if account.platform == "instagram" and login_mode == "instagram_login"
            else "https://graph.facebook.com"
        )
    )
    api_version = str(account.config.get("api_version") or "v23.0")
    desired = tuple(
        str(field)
        for field in account.config.get("meta_desired_subscribed_fields", [])
        if isinstance(field, str)
    )
    if not desired:
        desired = meta_subscription_fields(
            platform=account.platform,
            enable_dm=capability_enabled(account.capability, CapabilityKey.DM),
            enable_comments=capability_enabled(account.capability, CapabilityKey.COMMENTS),
        )
    client = MetaGraphClient(
        platform=account.platform,
        access_token=access_token,
        app_secret=app_secret,
        external_account_id=account.external_account_id,
        graph_base_url=graph_base_url,
        api_version=api_version,
        instagram_login_mode=login_mode,
        page_id=account.config.get("page_id"),
    )
    try:
        profile = await client.get_account()
        if str(profile.get("id") or "") != account.external_account_id:
            raise ValueError("meta_token_account_mismatch")
        subscription_account_id = _subscription_account_id(account)
        observed = await get_meta_subscription_fields(
            platform=account.platform,
            access_token=access_token,
            app_secret=app_secret,
            app_id=app.external_app_id,
            external_account_id=subscription_account_id,
            instagram_login_mode=login_mode,
            graph_base_url=graph_base_url,
            api_version=api_version,
        )
        if not set(desired).issubset(observed):
            await subscribe_meta_account(
                platform=account.platform,
                access_token=access_token,
                app_secret=app_secret,
                external_account_id=subscription_account_id,
                instagram_login_mode=login_mode,
                graph_base_url=graph_base_url,
                api_version=api_version,
                enable_dm="messages" in desired,
                enable_comments=bool({"feed", "comments"}.intersection(desired)),
            )
            observed = await get_meta_subscription_fields(
                platform=account.platform,
                access_token=access_token,
                app_secret=app_secret,
                app_id=app.external_app_id,
                external_account_id=subscription_account_id,
                instagram_login_mode=login_mode,
                graph_base_url=graph_base_url,
                api_version=api_version,
            )
        status = "READY" if set(desired).issubset(observed) else "SUBSCRIPTION_MISSING"
        app_observed = await _reconcile_app_subscription(
            app_external_id=app.external_app_id,
            app_public_id=app.public_id,
            app_secret=app_secret,
            verify_token=app_credentials.get("verify_token", ""),
            platform=account.platform,
            desired=desired,
            api_version=api_version,
        )
        if not set(desired).issubset(app_observed):
            status = "APP_SUBSCRIPTION_MISSING"
        await _save_health(
            account.id,
            status=status,
            subscribed_fields=observed,
            app_subscribed_fields=app_observed,
        )
        return str(account.id) if status != "READY" else None
    except Exception as exc:  # noqa: BLE001 - provider failures become sanitized health state
        error_code = _health_error_code(exc)
        status = "REAUTH_REQUIRED" if error_code.endswith("_190") else "ERROR"
        await _save_health(account.id, status=status, error_code=error_code)
        logger.warning(
            "meta health check failed account=%s platform=%s code=%s",
            account.id,
            account.platform,
            error_code,
        )
        return str(account.id)
    finally:
        await client.aclose()


async def reconcile_meta_account_health(*, force: bool = False) -> list[str]:
    global _last_check_at
    settings = get_settings()
    now = time.monotonic()
    if (
        not force
        and _last_check_at is not None
        and now - _last_check_at < settings.meta_health_check_interval_seconds
    ):
        return []
    _last_check_at = now
    accounts: list[PlatformAccountRuntime] = []
    if settings.facebook_messenger_enabled:
        accounts.extend(await list_active_accounts_by_platform("facebook"))
    if settings.instagram_messaging_enabled:
        accounts.extend(await list_active_accounts_by_platform("instagram"))
    unhealthy = []
    for account in accounts:
        try:
            result = await _check_account(account)
        except Exception as exc:  # noqa: BLE001 - isolate accounts in the scheduler sweep
            error_code = _health_error_code(exc)
            await _save_health(account.id, status="ERROR", error_code=error_code)
            logger.exception(
                "meta health account check crashed account=%s code=%s",
                account.id,
                error_code,
            )
            result = str(account.id)
        if result is not None:
            unhealthy.append(result)
    return unhealthy
