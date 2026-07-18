"""Durable X DM polling with safe bootstrap and complete pagination."""

import logging
import os
import time

import httpx
from sqlalchemy import update

from social_reply.application.event_ingestion.direct import ingest_canonical_event
from social_reply.application.platform_accounts import list_active_accounts_by_platform
from social_reply.connectors.x.adapter import XWebhookAdapter
from social_reply.connectors.x.client import XClient
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = int(os.getenv("X_DM_POLL_INTERVAL_SECONDS", "90"))
_last_poll_at: float = 0.0


async def poll_x_direct_messages() -> list[str]:
    global _last_poll_at
    now = time.monotonic()
    if now - _last_poll_at < _POLL_INTERVAL_SECONDS:
        return []
    _last_poll_at = now

    ingested: list[str] = []
    for account in await list_active_accounts_by_platform("x"):
        try:
            ingested.extend(await _poll_account(account))
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                logger.warning("x dm poll rate-limited account=%s", account.id)
            else:
                logger.error(
                    "x dm poll http error account=%s status=%s",
                    account.id,
                    exc.response.status_code,
                )
        except Exception:  # noqa: BLE001 - one account must not block others
            logger.exception("x dm poll failed account=%s", account.id)
    return ingested


async def _poll_account(account) -> list[str]:
    self_id = account.external_account_id
    if not self_id:
        logger.warning("x account %s has no external_account_id; skipping poll", account.id)
        return []

    config = account.config or {}
    cursor = config.get("x_dm_cursor")
    # Existing deployments already persisted a cursor before the explicit bootstrap marker existed.
    bootstrapped = bool(config.get("x_dm_bootstrapped")) or cursor is not None
    client = XClient(
        consumer_key=account.credential_bundle["consumer_key"],
        consumer_secret=account.credential_bundle["consumer_secret"],
        access_token=account.credential_bundle["access_token"],
        access_token_secret=account.credential_bundle["access_token_secret"],
        api_base_url=account.config.get("api_base_url", "https://api.x.com"),
    )
    try:
        events = await _read_until_cursor(client, cursor, bootstrap=not bootstrapped)
    finally:
        await client.aclose()
    if not bootstrapped:
        newest_id = _max_event_id(events)
        await _save_cursor(account.id, newest_id, bootstrapped=True)
        return []
    if not events:
        return []

    newest_id = _max_event_id(events)
    adapter = XWebhookAdapter(account_id=str(account.id), external_account_id=self_id)
    ingested: list[str] = []
    for event in sorted(events, key=lambda item: _as_int(item.get("id")) or 0):
        event_id = str(event.get("id") or "")
        canonical = adapter.normalize(
            {
                "for_user_id": self_id,
                "direct_message_events": [
                    {
                        "id": event_id,
                        "type": "message_create",
                        "message_create": {
                            "sender_id": event.get("sender_id"),
                            "message_data": {"text": event.get("text")},
                        },
                    }
                ],
            }
        )
        for item in canonical:
            if await ingest_canonical_event(item) is not None:
                ingested.append(event_id)

    # Advance only after every fetched event has completed ingestion. Any exception leaves the
    # cursor unchanged so the full range is retried and durable deduplication absorbs repeats.
    await _save_cursor(account.id, newest_id)
    return ingested


async def _read_until_cursor(
    client: XClient, cursor: str | None, *, bootstrap: bool = False
) -> list[dict]:
    cursor_num = _as_int(cursor)
    pagination_token: str | None = None
    seen_tokens: set[str] = set()
    collected: list[dict] = []
    for _page in range(100):
        events, pagination_token = await client.read_dm_events(
            max_results=100, pagination_token=pagination_token
        )
        for event in events:
            event_num = _as_int(event.get("id"))
            if cursor_num is None or event_num is None or event_num > cursor_num:
                collected.append(event)
        if bootstrap or not pagination_token:
            break
        if pagination_token in seen_tokens:
            raise RuntimeError("x_dm_pagination_token_repeated")
        seen_tokens.add(pagination_token)
        numeric_ids = [_as_int(event.get("id")) for event in events]
        comparable = [event_id for event_id in numeric_ids if event_id is not None]
        if cursor_num is not None and comparable and min(comparable) <= cursor_num:
            break
    else:
        raise RuntimeError("x_dm_pagination_limit_exceeded")
    return collected


def _max_event_id(events: list[dict]) -> str | None:
    values = [_as_int(event.get("id")) for event in events]
    numeric = [value for value in values if value is not None]
    return str(max(numeric)) if numeric else None


def _as_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def _save_cursor(
    account_id, newest_id: str | None, *, bootstrapped: bool | None = None
) -> None:
    async with get_session_factory()() as session:
        row = await session.get(models.PlatformAccount, account_id)
        if row is None:
            return
        config = dict(row.config or {})
        if newest_id is not None:
            config["x_dm_cursor"] = newest_id
        if bootstrapped is not None:
            config["x_dm_bootstrapped"] = bootstrapped
        await session.execute(
            update(models.PlatformAccount)
            .where(models.PlatformAccount.id == account_id)
            .values(config=config)
        )
        await session.commit()
