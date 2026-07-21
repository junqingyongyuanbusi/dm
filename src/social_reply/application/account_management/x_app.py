import uuid

from sqlalchemy import select

from social_reply.application.account_management.provisioning import provision_platform_app
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.shared.config import get_settings

_X_APP_PUBLIC_ID = "x_oauth"
_X_API_BASE_URL = "https://api.x.com"


def x_app_credentials() -> tuple[str, str] | None:
    return get_settings().x_app_credentials


async def ensure_x_platform_app(
    *,
    tenant_id: str,
    consumer_key: str | None = None,
    consumer_secret: str | None = None,
) -> tuple[uuid.UUID, str]:
    credentials = (
        (consumer_key.strip(), consumer_secret.strip())
        if consumer_key and consumer_secret
        else x_app_credentials()
    )
    if credentials is None:
        raise ValueError("x_oauth_app_not_configured")
    consumer_key, consumer_secret = credentials
    return await provision_platform_app(
        platform_family="x",
        external_app_id=consumer_key,
        tenant_id=tenant_id,
        name="X OAuth App",
        public_id=_X_APP_PUBLIC_ID,
        public_id_prefix="xapp",
        secrets_root=get_settings().account_secrets_root,
        credential_bundle={
            "consumer_key": consumer_key,
            "consumer_secret": consumer_secret,
        },
        config={"api_base_url": _X_API_BASE_URL},
    )


async def x_platform_app_id(*, tenant_id: str) -> uuid.UUID | None:
    credentials = x_app_credentials()
    if credentials is None:
        return None
    consumer_key, _consumer_secret = credentials
    async with get_session_factory()() as session:
        return await session.scalar(
            select(models.PlatformApp.id).where(
                models.PlatformApp.tenant_id == tenant_id,
                models.PlatformApp.platform_family == "x",
                models.PlatformApp.external_app_id == consumer_key,
            )
        )
