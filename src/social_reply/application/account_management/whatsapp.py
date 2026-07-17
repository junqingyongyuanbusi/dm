from pathlib import Path

import httpx

from social_reply.application.account_management.meta_app import provision_meta_app
from social_reply.application.account_management.provisioning import provision_direct_account
from social_reply.application.account_management.service import (
    AccountConnectionResult,
    _require_secret,
    _validate_automation_default,
    _webhook_url,
)
from social_reply.connectors.whatsapp.client import WhatsAppClient


async def connect_whatsapp_account(
    *,
    external_account_id: str,
    access_token: str,
    app_secret: str,
    public_base_url: str,
    verify_token: str,
    app_id: str | None = None,
    app_public_id: str | None = None,
    tenant_id: str = "default",
    brand_id: str = "default",
    name: str | None = None,
    app_name: str | None = None,
    public_id: str | None = None,
    secrets_root: Path = Path(".secrets/accounts"),
    graph_base_url: str = "https://graph.facebook.com",
    api_version: str = "v23.0",
    automation_default: str = "BOT_DRAFT_ONLY",
    transport: httpx.AsyncBaseTransport | None = None,
) -> AccountConnectionResult:
    """Connect one WhatsApp Cloud API phone_number_id under a shared Meta App."""
    _validate_automation_default(automation_default)
    external_account_id = _require_secret(external_account_id, "phone_number_id")
    access_token = _require_secret(access_token, "whatsapp_access_token")
    app_secret = _require_secret(app_secret, "meta_app_secret")
    client = WhatsAppClient(
        access_token=access_token,
        phone_number_id=external_account_id,
        graph_base_url=graph_base_url,
        api_version=api_version,
        transport=transport,
    )
    try:
        profile = await client.get_phone_number()
    finally:
        await client.aclose()
    if str(profile.get("id")) != external_account_id:
        raise ValueError("whatsapp_token_account_mismatch")

    platform_app_id, resolved_app_public_id, resolved_verify_token = await provision_meta_app(
        tenant_id=tenant_id,
        app_id=app_id,
        app_public_id=app_public_id,
        app_name=app_name,
        app_secret=app_secret,
        verify_token=verify_token,
        secrets_root=secrets_root,
        graph_base_url=graph_base_url,
        api_version=api_version,
    )
    account_id, resolved_public_id = await provision_direct_account(
        platform="whatsapp",
        external_account_id=external_account_id,
        tenant_id=tenant_id,
        brand_id=brand_id,
        name=(
            name
            or profile.get("verified_name")
            or profile.get("display_phone_number")
            or external_account_id
        ),
        public_id=public_id,
        public_id_prefix="wa",
        secrets_root=secrets_root,
        credential_bundle={"access_token": access_token},
        webhook_secret_bundle=None,
        config={"graph_base_url": graph_base_url, "api_version": api_version},
        capability={
            "dm": True,
            "session_messages": True,
            "templates": False,
            "max_text_length": 4096,
            "quality_rating": profile.get("quality_rating"),
        },
        automation_default=automation_default,
        platform_app_id=platform_app_id,
    )
    resolved_name = (
        name
        or profile.get("verified_name")
        or profile.get("display_phone_number")
        or external_account_id
    )
    return AccountConnectionResult(
        account_id=account_id,
        platform="whatsapp",
        external_account_id=external_account_id,
        public_id=resolved_public_id,
        webhook_url=_webhook_url(public_base_url, f"/webhooks/meta/{resolved_app_public_id}"),
        name=resolved_name,
        automation_default=automation_default,
        platform_app_id=platform_app_id,
        app_public_id=resolved_app_public_id,
        verify_token=resolved_verify_token,
        manual_steps=(
            "Subscribe the WhatsApp Business Account to messages on the Meta App webhook.",
            "Complete Business Verification and register approved templates "
            "for messages outside the service window.",
        ),
    )
