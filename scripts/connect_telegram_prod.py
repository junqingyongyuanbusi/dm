"""一次性：在生产环境连接 Telegram bot（验证 + 落库 + setWebhook）。

用法（token 经环境变量传入，不落盘、不写进代码）：
    TG_BOT_TOKEN=<token> railway run --service api \
        python scripts/connect_telegram_prod.py

public_id 固定为 primary，与历史 webhook 路径一致：
    {PUBLIC_BASE_URL}/webhooks/telegram/primary
"""

import asyncio
import os

from social_reply.application.account_management.service import connect_telegram_account
from social_reply.shared.config import get_settings


async def main() -> None:
    token = os.environ["TG_BOT_TOKEN"].strip()
    settings = get_settings()
    result = await connect_telegram_account(
        token=token,
        public_base_url=settings.public_base_url,
        tenant_id=settings.tenant_id,
        brand_id="default",
        public_id="primary",
        automation_default="BOT_ACTIVE",
        rotate_webhook_secret=True,
        drop_pending_updates=True,
    )
    print("connected account:")
    print("  account_id:", result.account_id)
    print("  external_account_id:", result.external_account_id)
    print("  public_id:", result.public_id)
    print("  name:", result.name)
    print("  automation_default:", result.automation_default)
    print("  webhook_url:", result.webhook_url)
    print("  pending_update_count:", result.pending_update_count)
    print("  last_webhook_error:", result.last_webhook_error)


if __name__ == "__main__":
    asyncio.run(main())
