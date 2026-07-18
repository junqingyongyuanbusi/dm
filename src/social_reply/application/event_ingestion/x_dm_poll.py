"""X DM 轮询拉取：替代不可靠的 Account Activity webhook 推送。

X 的 webhook DM 投递不稳定（平台侧已知问题），改由 scheduler 定时调
GET /2/dm_events 主动拉取新 DM，转成 CanonicalEvent 走与 webhook 相同的
入站链路（normalize → 决策 → 投递）。

幂等性：ingest_canonical_event 以 external_event_id 去重（NormalizedEvent
唯一约束 + on_conflict_do_nothing），重复拉取同一条 DM 不会重复处理。
游标（config.x_dm_cursor）仅用于减少无谓拉取，非正确性依赖。
"""

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

# X /2/dm_events 速率限制实测为 15 次 / 15 分钟（≈1 次/分钟）。60s 太贴边易触发 429，
# 取 90s（≈10 次/15 分钟）留足余量。scheduler 每 3s 调一次靠此节流拦截真实请求。
_POLL_INTERVAL_SECONDS = int(os.getenv("X_DM_POLL_INTERVAL_SECONDS", "90"))
_last_poll_at: float = 0.0


async def poll_x_direct_messages() -> list[str]:
    """拉取所有活跃 X 账号的新 DM 并入站。返回已入站的事件 id 列表。

    自带节流：距上次真实拉取不足 _POLL_INTERVAL_SECONDS 时直接跳过，避免 X 429。
    """
    global _last_poll_at
    now = time.monotonic()
    if now - _last_poll_at < _POLL_INTERVAL_SECONDS:
        return []
    _last_poll_at = now

    accounts = await list_active_accounts_by_platform("x")
    ingested: list[str] = []

    for account in accounts:
        try:
            processed = await _poll_account(account)
            ingested.extend(processed)
        except httpx.HTTPStatusError as exc:
            # 429 限流是预期内的偶发情况（节流已尽量避免），降级为 warning 不刷 error
            if exc.response.status_code == 429:
                logger.warning("x dm poll rate-limited (429) account=%s, backing off", account.id)
            else:
                logger.error(
                    "x dm poll http error account=%s: %s", account.id, exc.response.status_code
                )
        except Exception as exc:  # noqa: BLE001 - 单账号失败不阻断其它账号
            # 异常类型+消息拼进单行，避免多行堆栈被日志平台截断而看不到根因
            logger.error(
                "x dm poll failed for account=%s: %s: %s",
                account.id,
                type(exc).__name__,
                exc,
            )

    return ingested


async def _poll_account(account) -> list[str]:
    credentials = account.credential_bundle
    self_id = account.external_account_id
    cursor = (account.config or {}).get("x_dm_cursor")

    client = XClient(
        consumer_key=credentials["consumer_key"],
        consumer_secret=credentials["consumer_secret"],
        access_token=credentials["access_token"],
        access_token_secret=credentials["access_token_secret"],
        api_base_url=account.config.get("api_base_url", "https://api.x.com"),
    )
    try:
        events = await client.read_dm_events(max_results=50)
    finally:
        await client.aclose()

    if not events:
        return []

    # X 返回按时间倒序；正序处理保证 external_event_id 语义与 webhook 一致
    events = list(reversed(events))
    adapter = XWebhookAdapter(account_id=str(account.id), external_account_id=self_id)

    # X event id 是雪花 id（单调递增数字），必须按数值比较——字符串比较在位数不同时会错
    cursor_num = _as_int(cursor)

    # 第一遍：推进游标 + 收集游标之后的新事件
    newest_num = cursor_num
    fresh: list[dict] = []
    for event in events:
        event_num = _as_int(event.get("id"))
        if event_num is not None and (newest_num is None or event_num > newest_num):
            newest_num = event_num
        # 跳过游标之前已处理的（幂等兜底仍在 ingest 层）
        if cursor_num is not None and event_num is not None and event_num <= cursor_num:
            continue
        fresh.append(event)

    # 突发保护：恢复积压时同一发送者一批只回最新一条，其余静默跳过。
    # 否则会瞬间连发多条回复（曾一秒发出 4 条相同回复），触发 X 反垃圾降权。
    latest_per_sender: dict[str, dict] = {}
    for event in fresh:  # events 已正序，后者覆盖前者 = 保留最新
        sender = str(event.get("sender_id") or "")
        latest_per_sender[sender] = event

    ingested: list[str] = []
    for event in latest_per_sender.values():
        event_id = event.get("id")
        # 自己发出的回复会被 X 一并返回，交给 adapter 过滤（sender == 自身）
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
        for ce in canonical:
            if await ingest_canonical_event(ce) is not None:
                ingested.append(event_id)

    await _save_cursor(account.id, str(newest_num) if newest_num is not None else None)
    return ingested


def _as_int(value: str | None) -> int | None:
    """X 雪花 id 转 int 用于数值比较；非数字返回 None（不参与游标）。"""
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


async def _save_cursor(account_id, newest_id: str | None) -> None:
    """把最新 dm_event id 存进 account.config.x_dm_cursor（增量拉取游标）。"""
    if newest_id is None:
        return
    async with get_session_factory()() as session:
        row = await session.get(models.PlatformAccount, account_id)
        if row is None:
            return
        config = dict(row.config or {})
        config["x_dm_cursor"] = newest_id
        await session.execute(
            update(models.PlatformAccount)
            .where(models.PlatformAccount.id == account_id)
            .values(config=config)
        )
        await session.commit()
