"""Reconcile X Activity subscriptions and XChat key registration state."""

import logging
import time
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select, text

from social_reply.application.account_management.x_credentials import x_credentials
from social_reply.application.platform_accounts import list_active_accounts_by_platform
from social_reply.connectors.xchat.client import SUPPORTED_ACTIVITY_EVENT_TYPES, XChatClient
from social_reply.connectors.xchat.state import (
    XChatKeyState,
    XChatState,
    classify_xchat_state,
    xchat_state_config,
)
from social_reply.domain.platform_accounts import CapabilityKey, capability_enabled
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.secret_crypto import decrypt_secret_bundle, encrypt_secret_bundle
from social_reply.shared.config import Settings, get_settings

logger = logging.getLogger(__name__)

# 订阅对象与客户端允许创建的事件必须是同一份名单，否则会在运行时才暴露。
_EVENT_TYPES = SUPPORTED_ACTIVITY_EVENT_TYPES
_last_check_at: float | None = None


async def ensure_xchat_subscriptions(
    *,
    account_ids: set[uuid.UUID] | None = None,
    force: bool = False,
) -> list[str]:
    settings = get_settings()
    if settings.testing:
        return await _ensure_xchat_subscriptions_unlocked(
            settings=settings,
            account_ids=account_ids,
            force=force,
        )
    async with get_session_factory()() as lock_session:
        acquired = await lock_session.scalar(
            text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": "reply-core:x-activity-reconcile"},
        )
        if acquired is not True:
            return []
        return await _ensure_xchat_subscriptions_unlocked(
            settings=settings,
            account_ids=account_ids,
            force=force,
        )


async def _ensure_xchat_subscriptions_unlocked(
    *,
    settings: Settings,
    account_ids: set[uuid.UUID] | None,
    force: bool,
) -> list[str]:
    global _last_check_at
    check_interval_seconds = settings.xchat_subscription_check_interval_seconds
    ready_probe_interval = timedelta(seconds=settings.xchat_ready_probe_interval_seconds)
    pending_probe_interval = timedelta(seconds=settings.xchat_pending_probe_interval_seconds)
    if not settings.x_activity_enabled:
        return []
    now = time.monotonic()
    if not force and _last_check_at is not None and now - _last_check_at < check_interval_seconds:
        return []
    _last_check_at = now

    created: list[str] = []
    subscriptions_by_app: dict[str, list[dict]] = {}
    webhook_by_app: dict[str, str | None] = {}
    for account in await list_active_accounts_by_platform("x"):
        if account_ids is not None and account.id not in account_ids:
            continue
        credentials = x_credentials(account)
        consumer_key = credentials.get("consumer_key")
        consumer_secret = credentials.get("consumer_secret")
        external_account_id = account.external_account_id
        if not all((consumer_key, consumer_secret, external_account_id)):
            continue
        client = XChatClient(
            consumer_key=consumer_key,
            consumer_secret=consumer_secret,
            access_token=credentials["access_token"],
            access_token_secret=credentials["access_token_secret"],
            api_base_url=(account.config or {}).get("api_base_url", "https://api.x.com"),
        )
        activity_state: dict[str, dict] = {}
        try:
            subscriptions = subscriptions_by_app.get(consumer_key)
            if subscriptions is None:
                subscriptions = await client.list_subscriptions()
                subscriptions_by_app[consumer_key] = subscriptions

            xchat_registered = False
            if settings.xchat_enabled:
                xchat_registered = await _reconcile_xchat_key_state(
                    account,
                    credentials,
                    client,
                    force=force,
                    ready_probe_interval=ready_probe_interval,
                    pending_probe_interval=pending_probe_interval,
                )
            desired = {
                "dm.received": settings.x_legacy_dm_enabled
                and capability_enabled(account.capability or {}, CapabilityKey.DM),
                "chat.received": settings.xchat_enabled and xchat_registered,
                "post.mention.create": settings.x_mention_ingest_enabled
                and capability_enabled(account.capability or {}, CapabilityKey.MENTIONS),
            }
            configured_webhook_id = (account.config or {}).get("x_webhook_id")
            webhook_id = str(configured_webhook_id) if configured_webhook_id else None
            if any(desired.values()) and not webhook_id:
                if consumer_key not in webhook_by_app:
                    webhook_by_app[consumer_key] = await _resolve_webhook_id(client)
                webhook_id = webhook_by_app[consumer_key]
            for event_type in _EVENT_TYPES:
                if not desired[event_type]:
                    activity_state[event_type] = {"status": "NOT_REQUIRED"}
                    continue
                if not webhook_id:
                    activity_state[event_type] = {
                        "status": "ERROR",
                        "last_error_code": "X_ACTIVITY_WEBHOOK_MISSING",
                    }
                    logger.error(
                        "X Activity subscription has no webhook account=%s event_type=%s",
                        account.id,
                        event_type,
                    )
                    continue
                existing = _find_subscription(
                    subscriptions,
                    event_type=event_type,
                    user_id=external_account_id,
                    webhook_id=webhook_id,
                )
                if existing is not None:
                    activity_state[event_type] = _subscription_health("ACTIVE", existing)
                    continue
                try:
                    result = await client.create_activity_subscription(
                        event_type=event_type,
                        user_id=external_account_id,
                        webhook_id=webhook_id,
                        tag=f"reply-core:{account.public_id}:{event_type}",
                    )
                except Exception as exc:  # noqa: BLE001 - keep the other transport healthy
                    activity_state[event_type] = {
                        "status": "ERROR",
                        "last_error_code": _subscription_error_code(exc),
                    }
                    logger.exception(
                        "X Activity subscription failed account=%s event_type=%s",
                        account.id,
                        event_type,
                    )
                    continue
                subscription = _created_subscription(
                    result,
                    event_type=event_type,
                    user_id=external_account_id,
                    webhook_id=webhook_id,
                )
                if not subscription:
                    activity_state[event_type] = {
                        "status": "ERROR",
                        "last_error_code": "X_ACTIVITY_CREATE_RESPONSE_INVALID",
                    }
                    continue
                subscriptions.append(subscription)
                subscription_id = str(subscription.get("subscription_id") or "")
                activity_state[event_type] = _subscription_health("ACTIVE", subscription)
                if subscription_id:
                    created.append(subscription_id)
                    logger.info(
                        "X Activity subscription created account=%s event_type=%s "
                        "subscription=%s webhook=%s",
                        account.id,
                        event_type,
                        subscription_id,
                        webhook_id,
                    )
        except Exception as exc:  # noqa: BLE001 - one account must not block reconciliation
            error_code = _subscription_error_code(exc)
            activity_state = {
                event_type: {"status": "ERROR", "last_error_code": error_code}
                for event_type in _EVENT_TYPES
            }
            logger.exception("X Activity reconciliation failed account=%s", account.id)
        finally:
            await client.aclose()
        await _save_activity_state(account.id, activity_state)
    return created


async def _reconcile_xchat_key_state(
    account,
    credentials: dict[str, str],
    client: XChatClient,
    *,
    force: bool,
    ready_probe_interval: timedelta,
    pending_probe_interval: timedelta,
) -> bool:
    config = account.config or {}
    if not force and not _probe_due(
        config,
        ready_probe_interval=ready_probe_interval,
        pending_probe_interval=pending_probe_interval,
    ):
        return config.get("xchat_registered") is True
    try:
        records = await client.get_user_public_keys(str(account.external_account_id))
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            records = []
        else:
            logger.exception("XChat public-key probe failed account=%s", account.id)
            return config.get("xchat_registered") is True
    except Exception:  # noqa: BLE001 - preserve the last proven state on transient probe failure
        logger.exception("XChat public-key probe failed account=%s", account.id)
        return config.get("xchat_registered") is True
    state = classify_xchat_state(
        records,
        private_keys_b64=credentials.get("xchat_private_keys_b64"),
    )
    await _save_xchat_key_state(account.id, state)
    return state.registered


def _probe_due(
    config: dict,
    *,
    ready_probe_interval: timedelta,
    pending_probe_interval: timedelta,
) -> bool:
    value = config.get("xchat_last_probed_at")
    if not isinstance(value, str):
        return True
    try:
        last_probed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return True
    if last_probed.tzinfo is None:
        last_probed = last_probed.replace(tzinfo=UTC)
    interval = (
        ready_probe_interval
        if config.get("xchat_key_state") == XChatKeyState.READY.value
        else pending_probe_interval
    )
    return datetime.now(UTC) - last_probed >= interval


async def _save_xchat_key_state(account_id: uuid.UUID, state: XChatState) -> None:
    async with get_session_factory()() as session:
        row = (
            await session.execute(
                select(models.PlatformAccount)
                .where(models.PlatformAccount.id == account_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            return
        config = dict(row.config or {})
        capability = dict(row.capability or {})
        credentials = decrypt_secret_bundle(row.credential_bundle)
        config.update(xchat_state_config(state, probed_at=datetime.now(UTC).isoformat()))
        config["xchat_enabled"] = state.key_state is XChatKeyState.READY
        capability_changed = capability.get("x_chat") != (state.key_state is XChatKeyState.READY)
        capability["x_chat"] = state.key_state is XChatKeyState.READY
        credential_changed = False
        if state.key_state is XChatKeyState.READY and state.public_key_version:
            if credentials.get("xchat_signing_key_version") != state.public_key_version:
                credentials["xchat_signing_key_version"] = state.public_key_version
                credential_changed = True
        row.config = config
        row.capability = capability
        if credential_changed:
            row.credential_bundle = encrypt_secret_bundle(credentials)
        if capability_changed or credential_changed:
            row.config_version += 1
        await session.commit()


def _find_subscription(
    subscriptions: list[dict],
    *,
    event_type: str,
    user_id: str,
    webhook_id: str | None = None,
) -> dict | None:
    return next(
        (
            item
            for item in subscriptions
            if item.get("event_type") == event_type
            and str((item.get("filter") or {}).get("user_id")) == str(user_id)
            and (webhook_id is None or str(item.get("webhook_id") or "") == str(webhook_id))
        ),
        None,
    )


def _has_received_subscription(subscriptions: list[dict], user_id: str) -> bool:
    return (
        _find_subscription(
            subscriptions,
            event_type="chat.received",
            user_id=user_id,
        )
        is not None
    )


def _created_subscription(
    result: dict,
    *,
    event_type: str,
    user_id: str,
    webhook_id: str,
) -> dict:
    data = result.get("data") or {}
    if isinstance(data, list):
        subscription = dict(data[0]) if data and isinstance(data[0], dict) else {}
    elif isinstance(data, dict):
        nested = data.get("subscription")
        subscription = dict(nested) if isinstance(nested, dict) else dict(data)
    else:
        return {}
    if (
        not subscription.get("subscription_id")
        or subscription.get("event_type") != event_type
        or str((subscription.get("filter") or {}).get("user_id")) != str(user_id)
        or str(subscription.get("webhook_id") or "") != str(webhook_id)
    ):
        return {}
    return subscription


def _subscription_health(status: str, subscription: dict) -> dict:
    return {
        "status": status,
        "subscription_id": str(subscription.get("subscription_id") or "") or None,
        "webhook_id": str(subscription.get("webhook_id") or "") or None,
    }


def _subscription_error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    return f"X_ACTIVITY_HTTP_{status_code}" if status_code else "X_ACTIVITY_RECONCILE_FAILED"


async def _save_activity_state(account_id: uuid.UUID, states: dict[str, dict]) -> None:
    checked_at = datetime.now(UTC).isoformat()
    async with get_session_factory()() as session:
        row = (
            await session.execute(
                select(models.PlatformAccount)
                .where(models.PlatformAccount.id == account_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            return
        config = dict(row.config or {})
        current = dict(config.get("x_activity_subscriptions") or {})
        for event_type, state in states.items():
            current[event_type] = {**state, "checked_at": checked_at}
        config["x_activity_subscriptions"] = current
        row.config = config
        await session.commit()


async def _resolve_webhook_id(client: XChatClient) -> str | None:
    webhooks = await client.list_webhooks()
    valid = [item for item in webhooks if item.get("valid") and item.get("id")]
    return str(valid[0]["id"]) if valid else None
