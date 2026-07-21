"""X webhook 健康自检与自愈。

X 在 CRC 定期校验失败(如服务重启窗口、TLS 波动)后会把 webhook 标为 valid=false
并静默停止推送——秒级通道就此熄火且无任何通知。本任务周期性检查 valid 状态,
失效即调 PUT /2/webhooks/{id} 触发 X 重发 CRC(api 服务在线即可通过),恢复推送。
轮询(x_dm_poll)仍作兜底,两路经 ingest 层 external_event_id 去重幂等并存。
"""

import base64
import logging
import os
import time

import httpx

from social_reply.application.account_management.x_credentials import x_credentials
from social_reply.application.platform_accounts import list_active_accounts_by_platform

logger = logging.getLogger(__name__)

_CHECK_INTERVAL_SECONDS = int(os.getenv("X_WEBHOOK_CHECK_INTERVAL_SECONDS", "600"))
_last_check_at: float = 0.0


async def ensure_x_webhooks_valid() -> list[str]:
    """scheduler 周期任务:失效的 X webhook 触发 CRC 重验;返回重验成功的 webhook id。"""
    global _last_check_at
    now = time.monotonic()
    if now - _last_check_at < _CHECK_INTERVAL_SECONDS:
        return []
    _last_check_at = now

    revalidated: list[str] = []
    seen_apps: set[str] = set()
    for account in await list_active_accounts_by_platform("x"):
        creds = x_credentials(account)
        consumer_key = creds.get("consumer_key")
        consumer_secret = creds.get("consumer_secret")
        if not consumer_key or not consumer_secret or consumer_key in seen_apps:
            continue  # webhook 挂在 app(consumer)级,同 app 多账号只查一次
        seen_apps.add(consumer_key)
        try:
            revalidated.extend(
                await _check_app(
                    consumer_key,
                    consumer_secret,
                    api_base_url=(account.config or {}).get("api_base_url", "https://api.x.com"),
                )
            )
        except Exception:  # noqa: BLE001 - health check must not break the sweep loop
            logger.exception("x webhook health check failed account=%s", account.id)
    return revalidated


async def _check_app(
    consumer_key: str,
    consumer_secret: str,
    *,
    api_base_url: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[str]:
    revalidated: list[str] = []
    async with httpx.AsyncClient(
        base_url=api_base_url.rstrip("/"), timeout=15, transport=transport
    ) as client:
        basic = base64.b64encode(f"{consumer_key}:{consumer_secret}".encode()).decode()
        token_response = await client.post(
            "/oauth2/token",
            headers={"Authorization": f"Basic {basic}"},
            data={"grant_type": "client_credentials"},
        )
        token_response.raise_for_status()
        bearer = {"Authorization": f"Bearer {token_response.json()['access_token']}"}

        webhooks = await client.get("/2/webhooks", headers=bearer)
        webhooks.raise_for_status()
        for hook in webhooks.json().get("data", []):
            hook_id = str(hook.get("id", ""))
            if hook.get("valid") or not hook_id:
                continue
            logger.warning(
                "x webhook invalid, requesting CRC revalidation: id=%s url=%s",
                hook_id,
                hook.get("url"),
            )
            revalidation = await client.put(f"/2/webhooks/{hook_id}", headers=bearer)
            if revalidation.is_success:
                logger.info("x webhook revalidated: id=%s", hook_id)
                revalidated.append(hook_id)
            else:
                # 重验失败通常是 api 服务 CRC 端点不可达,留给下轮;不抛错以免中断其他 app
                logger.error(
                    "x webhook revalidation failed: id=%s status=%s body=%s",
                    hook_id,
                    revalidation.status_code,
                    revalidation.text[:200],
                )
    return revalidated
