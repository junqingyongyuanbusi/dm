import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx

from social_reply.application.account_management.meta_app import provision_meta_app
from social_reply.application.account_management.provisioning import (
    bind_provisioning_external_identity,
    provision_direct_account,
)
from social_reply.application.account_management.service import (
    AccountConnectionResult,
    _require_secret,
    _validate_automation_default,
    _webhook_url,
)
from social_reply.application.platform_accounts import (
    get_platform_account_runtime,
    get_platform_app_runtime,
)
from social_reply.connectors.whatsapp.client import WhatsAppClient
from social_reply.shared.config import get_settings


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
    owner_user_id: uuid.UUID | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    operation: str = "CONNECT_ACCOUNT",
    target_account_id: uuid.UUID | str | None = None,
    expected_config_version: int | None = None,
    initiator_user_id: uuid.UUID | None = None,
    initiator_session_id: uuid.UUID | str | None = None,
    authority_kind: str = "UNVERIFIED",
    authority_version: int = 0,
    provisioning_job_id: uuid.UUID | None = None,
    provisioning_attempt_count: int | None = None,
    trusted_control_api: bool = False,
) -> AccountConnectionResult:
    """Connect one WhatsApp Cloud API phone_number_id under a shared Meta App."""
    if not get_settings().whatsapp_enabled:
        raise ValueError("whatsapp_integration_disabled")
    if operation != "REAUTHORIZE":
        _validate_automation_default(automation_default)
    external_account_id = _require_secret(external_account_id, "phone_number_id")
    access_token = _require_secret(access_token, "whatsapp_access_token")
    reauthorize = operation == "REAUTHORIZE"
    target_runtime = None
    target_app_runtime = None
    if reauthorize:
        if target_account_id is None:
            raise ValueError("platform_account_target_required")
        target_runtime = await get_platform_account_runtime(uuid.UUID(str(target_account_id)))
        if (
            target_runtime.tenant_id != tenant_id
            or target_runtime.platform != "whatsapp"
            or target_runtime.external_account_id != external_account_id
        ):
            raise ValueError("platform_account_target_mismatch")
        if target_runtime.platform_app_id is None:
            raise LookupError("platform_app_not_found")
        target_app_runtime = await get_platform_app_runtime(target_runtime.platform_app_id)
        graph_base_url = str((target_runtime.config or {}).get("graph_base_url") or graph_base_url)
        api_version = str((target_runtime.config or {}).get("api_version") or api_version)
    app_secret = _require_secret(app_secret, "meta_app_secret")
    if reauthorize:
        target_app_credentials = target_app_runtime.credential_bundle
        if (
            (app_id is not None and target_app_runtime.external_app_id != app_id)
            or (app_public_id is not None and target_app_runtime.public_id != app_public_id)
            or target_app_credentials.get("app_secret") != app_secret
            or target_app_credentials.get("verify_token") != verify_token
        ):
            raise ValueError("meta_app_rotation_required")
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

    if reauthorize:
        platform_app_id = target_runtime.platform_app_id
        resolved_app_public_id = target_app_runtime.public_id
        target_app_credentials = target_app_runtime.credential_bundle
        resolved_verify_token = target_app_credentials.get("verify_token")
    else:
        (
            platform_app_id,
            resolved_app_public_id,
            resolved_verify_token,
            _,
        ) = await provision_meta_app(
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
    if provisioning_job_id is not None:
        await bind_provisioning_external_identity(
            provisioning_job_id=provisioning_job_id,
            provisioning_attempt_count=provisioning_attempt_count,
            platform="whatsapp",
            external_account_id=external_account_id,
            credential_bundle={"access_token": access_token},
            platform_app_id=platform_app_id,
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
        derived_config_patch=(
            {
                "whatsapp_health_status": "CREDENTIAL_UPDATED",
                "whatsapp_health_checked_at": datetime.now(UTC).isoformat(),
                "whatsapp_health_error_code": None,
            }
            if operation == "REAUTHORIZE"
            else None
        ),
        capability={
            "dm": True,
            "session_messages": True,
            "templates": False,
            "max_text_length": 4096,
            "quality_rating": profile.get("quality_rating"),
        },
        automation_default=automation_default,
        owner_user_id=owner_user_id,
        platform_app_id=platform_app_id,
        operation=operation,
        target_account_id=target_account_id,
        expected_config_version=expected_config_version,
        initiator_user_id=initiator_user_id,
        initiator_session_id=initiator_session_id,
        authority_kind=authority_kind,
        authority_version=authority_version,
        provisioning_job_id=provisioning_job_id,
        provisioning_attempt_count=provisioning_attempt_count,
        trusted_control_api=trusted_control_api,
    )
    if reauthorize:
        return AccountConnectionResult(
            account_id=account_id,
            platform="whatsapp",
            external_account_id=external_account_id,
            public_id=resolved_public_id,
            webhook_url=_webhook_url(public_base_url, f"/webhooks/meta/{resolved_app_public_id}"),
            name=target_runtime.name,
            automation_default=target_runtime.automation_default,
            platform_app_id=platform_app_id,
            app_public_id=resolved_app_public_id,
            verify_token=resolved_verify_token,
            credential_updated=True,
            connection_ready=False,
            connection_status="NEEDS_ACTION",
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
