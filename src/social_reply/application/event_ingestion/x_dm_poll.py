"""Durable X DM polling with safe bootstrap and complete pagination."""

import logging
import os
import time

import httpx
from sqlalchemy import update

from social_reply.application.account_management.x_credentials import x_credentials
from social_reply.application.event_ingestion.direct import ingest_canonical_event
from social_reply.application.platform_accounts import list_active_accounts_by_platform
from social_reply.connectors.x.adapter import XWebhookAdapter
from social_reply.connectors.x.client import XClient
from social_reply.domain.platform_accounts import CapabilityKey, capability_enabled
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.shared.config import get_settings

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = int(os.getenv("X_DM_POLL_INTERVAL_SECONDS", "90"))
_last_poll_at: float = 0.0
# /2/dm_events 限流为 15 req/15min(实测+官方 PPU 文档)。90s 间隔常态每轮 1 页
# ≈10 req/15min;页帽限制积压轮的额外翻页,防单轮打爆预算引发 429 连环。
_MAX_PAGES_PER_POLL = 3
# 游标回看窗口:X 全局端点有官方未修复的事件晚到问题,晚到事件 id 小于已推进游标
# 会被永久跳过。回看 5 分钟,重复入站由 NormalizedEvent 唯一约束幂等吸收。
_CURSOR_LOOKBACK_MS = 5 * 60 * 1000


async def poll_x_direct_messages() -> list[str]:
    global _last_poll_at
    settings = get_settings()
    now = time.monotonic()
    if now - _last_poll_at < _POLL_INTERVAL_SECONDS:
        return []
    _last_poll_at = now

    ingested: list[str] = []
    for account in await list_active_accounts_by_platform("x"):
        dm_capable = capability_enabled(account.capability or {}, CapabilityKey.DM)
        if not settings.x_legacy_dm_enabled and not dm_capable:
            continue
        try:
            ingested.extend(
                await _poll_account(
                    account,
                    reconcile_capability=settings.x_legacy_dm_enabled and not dm_capable,
                )
            )
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


async def _poll_account(account, *, reconcile_capability: bool = False) -> list[str]:
    self_id = account.external_account_id
    if not self_id:
        logger.warning("x account %s has no external_account_id; skipping poll", account.id)
        return []

    config = account.config or {}
    cursor = config.get("x_dm_cursor")
    # Existing deployments already persisted a cursor before the explicit bootstrap marker existed.
    bootstrapped = bool(config.get("x_dm_bootstrapped")) or cursor is not None
    credentials = x_credentials(account)
    client = XClient(
        consumer_key=credentials["consumer_key"],
        consumer_secret=credentials["consumer_secret"],
        access_token=credentials["access_token"],
        access_token_secret=credentials["access_token_secret"],
        api_base_url=account.config.get("api_base_url", "https://api.x.com"),
    )
    try:
        events = await _read_until_cursor(client, cursor, bootstrap=not bootstrapped)
    finally:
        await client.aclose()
    if reconcile_capability:
        await _mark_dm_capable(account.id)
    if not bootstrapped:
        newest_id = _max_event_id(events)
        await _save_cursor(account.id, newest_id, bootstrapped=True)
        return []
    if not events:
        # 空转也要留痕:证明本轮确实调了 API 且无新于游标的 legacy 事件。
        # XChat 使用独立的 Chat API；由 xchat_poll 负责补拉，不能据此判断 X 侧无消息。
        logger.info("x_dm_poll idle account=%s cursor=%s", account.id, cursor)
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
    # 回看窗口可能使本轮 newest 旧于现游标(纯重叠轮),游标只进不退。
    newest_num, cursor_num = _as_int(newest_id), _as_int(cursor)
    if newest_num is not None and cursor_num is not None and newest_num < cursor_num:
        newest_id = cursor
    await _save_cursor(account.id, newest_id)
    return ingested


async def _read_until_cursor(
    client: XClient, cursor: str | None, *, bootstrap: bool = False
) -> list[dict]:
    cursor_num = _as_int(cursor)
    # 比较基准 = 游标减去回看窗口(雪花 id 高 42 位是毫秒时间戳,时间差需左移 22 位)
    floor_num = cursor_num - (_CURSOR_LOOKBACK_MS << 22) if cursor_num is not None else None
    pagination_token: str | None = None
    seen_tokens: set[str] = set()
    collected: list[dict] = []
    for _page in range(_MAX_PAGES_PER_POLL):
        events, pagination_token = await client.read_dm_events(
            max_results=100, pagination_token=pagination_token
        )
        for event in events:
            event_num = _as_int(event.get("id"))
            if floor_num is None or event_num is None or event_num > floor_num:
                collected.append(event)
        if bootstrap or not pagination_token:
            return collected
        if pagination_token in seen_tokens:
            raise RuntimeError("x_dm_pagination_token_repeated")
        seen_tokens.add(pagination_token)
        numeric_ids = [_as_int(event.get("id")) for event in events]
        comparable = [event_id for event_id in numeric_ids if event_id is not None]
        if floor_num is not None and comparable and min(comparable) <= floor_num:
            return collected
    # 页帽用尽仍未触及游标:处理已拉到的最新事件并推进游标,更深的积压放弃并告警。
    # 常态流量不可能在一个轮询间隔内积压 300+ 条;深积压只出现在游标数日未推进的
    # 病态场景,此时继续翻页会打爆 15/15min 限流,截断优于 429 雪崩。
    logger.warning(
        "x_dm_poll page budget exhausted (%d pages); deeper backlog dropped", _MAX_PAGES_PER_POLL
    )
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


async def _mark_dm_capable(account_id) -> None:
    async with get_session_factory()() as session:
        await session.execute(
            update(models.PlatformAccount)
            .where(models.PlatformAccount.id == account_id)
            .values(
                capability=models.PlatformAccount.capability.op("||")(
                    {CapabilityKey.DM.value: True}
                ),
                config_version=models.PlatformAccount.config_version + 1,
            )
        )
        await session.commit()


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
