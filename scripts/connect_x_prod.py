"""一次性：在生产环境连接 X 账号（验证 OAuth 1.0a 凭证 + 落库）。

X 不像 Telegram 能自动 setWebhook——Account Activity API 的 webhook 需在
Developer Portal 手动注册。本脚本只做凭证验证 + 落库，并打印需手动注册的 webhook_url。

用法（凭证经环境变量传入，不落盘）：
    X_CONSUMER_KEY=... X_CONSUMER_SECRET=... \
    X_ACCESS_TOKEN=... X_ACCESS_TOKEN_SECRET=... X_ENVIRONMENT=... \
    DATABASE_URL=<public-url> \
        uv run python scripts/connect_x_prod.py
"""

import asyncio
import os

from social_reply.application.account_management.service import connect_x_account
from social_reply.shared.config import get_settings


async def main() -> None:
    settings = get_settings()
    result = await connect_x_account(
        consumer_key=os.environ["X_CONSUMER_KEY"].strip(),
        consumer_secret=os.environ["X_CONSUMER_SECRET"].strip(),
        access_token=os.environ["X_ACCESS_TOKEN"].strip(),
        access_token_secret=os.environ["X_ACCESS_TOKEN_SECRET"].strip(),
        environment=os.environ["X_ENVIRONMENT"].strip(),
        public_base_url=settings.public_base_url,
        tenant_id=settings.tenant_id,
        brand_id="default",
        public_id="primary",
        automation_default=os.environ.get("X_AUTOMATION_DEFAULT", "BOT_DRAFT_ONLY"),
    )
    print("connected X account:")
    print("  account_id:", result.account_id)
    print("  external_account_id:", result.external_account_id)
    print("  public_id:", result.public_id)
    print("  name:", result.name)
    print("  automation_default:", result.automation_default)
    print("  webhook_url:", result.webhook_url)
    print("  manual_steps:")
    for step in result.manual_steps:
        print("    -", step)


if __name__ == "__main__":
    asyncio.run(main())
