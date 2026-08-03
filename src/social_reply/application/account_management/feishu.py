import re
from datetime import UTC, datetime
from pathlib import Path

import httpx

from social_reply.application.account_management.provisioning import provision_direct_account
from social_reply.application.account_management.service import (
    AccountConnectionResult,
    _require_secret,
    _webhook_url,
)
from social_reply.connectors.feishu.client import FeishuClient
from social_reply.connectors.feishu.contracts import (
    FEISHU_API_BASE_URL,
    FEISHU_GROUP_MODE,
)
from social_reply.connectors.feishu.contracts import (
    FEISHU_APP_ID_PATTERN as FEISHU_APP_ID_PATTERN_TEXT,
)
from social_reply.domain.platform_accounts import ACTIVE_ACCOUNT_STATUS
from social_reply.shared.config import get_settings

FEISHU_APP_ID_PATTERN = re.compile(FEISHU_APP_ID_PATTERN_TEXT)


def validate_feishu_app_id(value: str) -> str:
    app_id = value.strip()
    if not FEISHU_APP_ID_PATTERN.fullmatch(app_id):
        raise ValueError("invalid_feishu_app_id")
    return app_id


async def connect_feishu_account(
    *,
    app_id: str,
    app_secret: str,
    verification_token: str,
    encrypt_key: str,
    public_base_url: str,
    tenant_id: str = "default",
    brand_id: str = "default",
    name: str | None = None,
    public_id: str | None = None,
    secrets_root: Path = Path(".secrets/accounts"),
    api_base_url: str = FEISHU_API_BASE_URL,
    group_mode: str = FEISHU_GROUP_MODE,
    automation_default: str = "BOT_DRAFT_ONLY",
    transport: httpx.AsyncBaseTransport | None = None,
) -> AccountConnectionResult:
    if not get_settings().feishu_enabled:
        raise ValueError("feishu_integration_disabled")
    if automation_default != "BOT_DRAFT_ONLY":
        raise ValueError("feishu_requires_bot_draft_only")
    if group_mode != FEISHU_GROUP_MODE:
        raise ValueError("unsupported_feishu_group_mode")
    app_id = validate_feishu_app_id(app_id)
    app_secret = _require_secret(app_secret, "feishu_app_secret")
    verification_token = _require_secret(verification_token, "feishu_verification_token")
    encrypt_key = _require_secret(encrypt_key, "feishu_encrypt_key")
    api_base_url = api_base_url.strip().rstrip("/")
    if api_base_url != FEISHU_API_BASE_URL:
        raise ValueError("invalid_feishu_api_base_url")

    client = FeishuClient(
        app_id=app_id,
        app_secret=app_secret,
        api_base_url=api_base_url,
        transport=transport,
    )
    try:
        bot = await client.inspect_bot()
    finally:
        await client.aclose()

    checked_at = datetime.now(UTC).isoformat()
    resolved_name = name or bot.name or app_id
    account_id, resolved_public_id = await provision_direct_account(
        platform="feishu",
        external_account_id=app_id,
        tenant_id=tenant_id,
        brand_id=brand_id,
        name=resolved_name,
        public_id=public_id,
        public_id_prefix="fs",
        secrets_root=secrets_root,
        credential_bundle={"app_id": app_id, "app_secret": app_secret},
        webhook_secret_bundle={
            "verification_token": verification_token,
            "encrypt_key": encrypt_key,
        },
        config={
            "api_base_url": api_base_url,
            "feishu_group_mode": group_mode,
            "feishu_bot_open_id": bot.open_id,
            "feishu_bot_name": bot.name,
            "feishu_bot_activate_status": bot.activate_status,
            "feishu_health_status": "READY",
            "feishu_health_checked_at": checked_at,
        },
        capability={"dm": True, "mentions": True, "max_text_length": 4000},
        automation_default=automation_default,
        status=ACTIVE_ACCOUNT_STATUS,
    )
    callback_url = _webhook_url(
        public_base_url,
        f"/webhooks/feishu/{resolved_public_id}",
    )
    return AccountConnectionResult(
        account_id=account_id,
        platform="feishu",
        external_account_id=app_id,
        public_id=resolved_public_id,
        webhook_url=callback_url,
        name=resolved_name,
        automation_default=automation_default,
        bot_name=bot.name,
        bot_status=bot.activate_status,
        manual_steps=(
            f"Set the Feishu event callback URL to {callback_url} and subscribe to "
            "im.message.receive_v1.",
            "Enable P2P messaging and group @mention permissions; do not enable "
            "group-wide listening.",
        ),
    )
