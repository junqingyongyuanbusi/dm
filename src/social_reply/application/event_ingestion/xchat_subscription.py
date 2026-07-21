"""Reconcile X Activity ``chat.received`` subscriptions for X accounts."""

import logging
import os
import time

from social_reply.application.account_management.x_credentials import x_credentials
from social_reply.application.platform_accounts import list_active_accounts_by_platform
from social_reply.connectors.xchat.client import XChatClient

logger = logging.getLogger(__name__)

_CHECK_INTERVAL_SECONDS = int(os.getenv("XCHAT_SUBSCRIPTION_CHECK_INTERVAL_SECONDS", "600"))
_last_check_at: float = 0.0


async def ensure_xchat_subscriptions() -> list[str]:
    global _last_check_at
    now = time.monotonic()
    if now - _last_check_at < _CHECK_INTERVAL_SECONDS:
        return []
    _last_check_at = now

    created: list[str] = []
    seen_apps: dict[str, list[dict]] = {}
    for account in await list_active_accounts_by_platform("x"):
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
        try:
            subscriptions = seen_apps.get(consumer_key)
            if subscriptions is None:
                subscriptions = await client.list_subscriptions()
                seen_apps[consumer_key] = subscriptions
            if _has_received_subscription(subscriptions, external_account_id):
                continue
            webhook_id = (account.config or {}).get("x_webhook_id")
            if not webhook_id:
                webhook_id = await _resolve_webhook_id(client)
            if not webhook_id:
                logger.error("xchat subscription has no webhook account=%s", account.id)
                continue
            result = await client.create_received_subscription(
                user_id=external_account_id,
                webhook_id=str(webhook_id),
                tag=f"reply-core:{account.public_id}",
            )
            subscription = ((result.get("data") or {}).get("subscription") or {})
            subscription_id = str(subscription.get("subscription_id") or "")
            if subscription_id:
                subscriptions.append(subscription)
                created.append(subscription_id)
                logger.info(
                    "xchat subscription created account=%s subscription=%s webhook=%s",
                    account.id,
                    subscription_id,
                    webhook_id,
                )
        except Exception:  # noqa: BLE001 - one account must not block reconciliation
            logger.exception("xchat subscription reconciliation failed account=%s", account.id)
        finally:
            await client.aclose()
    return created


def _has_received_subscription(subscriptions: list[dict], user_id: str) -> bool:
    return any(
        item.get("event_type") == "chat.received"
        and str((item.get("filter") or {}).get("user_id")) == str(user_id)
        for item in subscriptions
    )


async def _resolve_webhook_id(client: XChatClient) -> str | None:
    webhooks = await client.list_webhooks()
    valid = [item for item in webhooks if item.get("valid") and item.get("id")]
    return str(valid[0]["id"]) if valid else None
