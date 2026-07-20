"""X DM 链路一键诊断:排查「发了 DM 但没有自动回复」。

用法(本地,凭证经环境变量注入,不落盘):
    DATABASE_URL=postgresql+asyncpg://... PLATFORM_SECRET_KEYS=... \
        uv run python scripts/diagnose_x_dm.py [对方用户ID]

输出四段:
  1. 账号与游标状态(游标时间 = 系统已处理到的位置)
  2. /2/dm_events 最近事件 vs 游标(有无「新于游标」的事件 = 轮询是否漏)
  3. 指定对方的 legacy 按会话端点对照
  4. XChat 会话事件端点对照（legacy DM API 看不到的加密消息会出现在这里）
  5. webhook 注册 + 订阅状态(valid=false 即秒级通道熄火,scheduler 会自动重验)
"""

import asyncio
import base64
import os
import sys

import httpx

# 本地只读诊断:跳过生产级 Settings 校验(无需全套生产 env)
os.environ.setdefault("TESTING", "true")

from social_reply.application.platform_accounts import (  # noqa: E402
    list_active_accounts_by_platform,
)
from social_reply.connectors.x.client import XClient  # noqa: E402


def _snowflake_ts(value: str | int | None) -> str:
    if value is None:
        return "-"
    from datetime import UTC, datetime

    ms = (int(value) >> 22) + 1288834974657
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M:%S")


def _print_events(events: list[dict], cursor: str | None) -> None:
    for event in events:
        event_id = str(event.get("id", "?"))
        flags = ""
        if cursor and event_id == cursor:
            flags += "  <== 游标"
        if cursor and event_id.isdigit() and int(event_id) > int(cursor):
            flags += "  [新于游标:应被轮询入站]"
        print(
            f"  {event_id} ({_snowflake_ts(event.get('id'))}) "
            f"sender={event.get('sender_id')} text={(event.get('text') or '')[:24]!r}{flags}"
        )


async def main() -> None:
    peer_id = sys.argv[1] if len(sys.argv) > 1 else None
    accounts = await list_active_accounts_by_platform("x")
    if not accounts:
        print("!! 无活跃 X 账号(platform_accounts.status != active?)")
        return
    account = accounts[0]
    credentials = account.credential_bundle
    cursor = (account.config or {}).get("x_dm_cursor")
    print(f"[1] 账号 {account.external_account_id}  status={account.status}")
    print(f"    游标 {cursor} ({_snowflake_ts(cursor)})  <- 系统已处理到此")

    client = XClient(
        consumer_key=credentials["consumer_key"],
        consumer_secret=credentials["consumer_secret"],
        access_token=credentials["access_token"],
        access_token_secret=credentials["access_token_secret"],
        api_base_url=(account.config or {}).get("api_base_url", "https://api.x.com"),
    )
    try:
        events, _ = await client.read_dm_events(max_results=100)
        print(f"\n[2] /2/dm_events 最近 {len(events)} 条(倒序,30 天窗口):")
        _print_events(events[:12], cursor)
        if (
            events
            and cursor
            and all(
                not (str(e.get("id", "")).isdigit() and int(e["id"]) > int(cursor)) for e in events
            )
        ):
            print("    => 无新于游标的事件:X 侧没有新消息可拉(非轮询问题)")

        if peer_id:
            response = await client.read_conversation_dm_events(peer_id, max_results=50)
            print(f"\n[3] 按会话端点(与 {peer_id})最近 {len(response)} 条:")
            _print_events(response[:10], cursor)
            global_ids = {str(e.get("id")) for e in events}
            only_conv = [e for e in response if str(e.get("id")) not in global_ids]
            if only_conv:
                print("    => 按会话可见但全局缺失(dm_events 漏消息 bug),以下事件待补:")
                _print_events(only_conv, cursor)
            else:
                print("    => legacy 全局/会话端点一致；继续检查 XChat，不应直接判定消息未到 X。")

            try:
                xchat_payload = await client.read_xchat_conversation_events(peer_id)
            except httpx.HTTPStatusError as exc:
                print(f"\n[4] XChat 会话端点 HTTP {exc.response.status_code}:")
                print(f"    => XChat 查询失败: {exc.response.text[:300]}")
            else:
                print("\n[4] XChat 会话端点 HTTP 200:")
                xchat_events = xchat_payload.get("data") or []
                for event in xchat_events[:12]:
                    encrypted = bool(event.get("encoded_event"))
                    print(
                        f"  {event.get('id')} ({event.get('created_at') or '-'}) "
                        f"sender={event.get('sender_id')} encrypted={encrypted}"
                    )
                legacy_ids = {str(e.get("id")) for e in response}
                xchat_only = [e for e in xchat_events if str(e.get("id")) not in legacy_ids]
                if xchat_only:
                    print(
                        f"    => 发现 {len(xchat_only)} 条仅 XChat 可见事件；"
                        "legacy API 并未提供这些数据。"
                    )
                elif not xchat_events:
                    print("    => XChat 端点也无事件；再考虑反垃圾/消息请求/收信权限。")
    finally:
        await client.aclose()

    async with httpx.AsyncClient(timeout=15) as http:
        basic = base64.b64encode(
            f"{credentials['consumer_key']}:{credentials['consumer_secret']}".encode()
        ).decode()
        token_response = await http.post(
            "https://api.x.com/oauth2/token",
            headers={"Authorization": f"Basic {basic}"},
            data={"grant_type": "client_credentials"},
        )
        if token_response.status_code != 200:
            print(f"\n[5] webhook 检查跳过:bearer 换取失败 HTTP {token_response.status_code}")
            return
        bearer = {"Authorization": f"Bearer {token_response.json()['access_token']}"}
        webhooks = (await http.get("https://api.x.com/2/webhooks", headers=bearer)).json()
        print("\n[5] webhook 注册状态:")
        for hook in webhooks.get("data", []):
            state = "OK(推送通道在线)" if hook.get("valid") else "INVALID(已停推,等自愈重验)"
            print(f"    {hook.get('id')} -> {hook.get('url')}  {state}")
            subscriptions = (
                await http.get(
                    f"https://api.x.com/2/account_activity/webhooks/"
                    f"{hook.get('id')}/subscriptions/all/list",
                    headers=bearer,
                )
            ).json()
            subs = (subscriptions.get("data") or {}).get("subscriptions") or []
            print(f"    订阅用户: {[s.get('user_id') for s in subs] or '无(需重新订阅!)'}")


if __name__ == "__main__":
    asyncio.run(main())
