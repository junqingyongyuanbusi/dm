import logging
from dataclasses import dataclass

from sqlalchemy import select

from social_reply.application.account_management.provisioning import tenant_public_id
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.secret_crypto import decrypt_secret_bundle
from social_reply.shared.config import get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MetaAppCredentials:
    app_id: str
    app_secret: str
    verify_token: str
    public_id: str
    platform_family: str


def _configured_credentials(
    *, tenant_id: str, standalone_instagram: bool
) -> MetaAppCredentials | None:
    settings = get_settings()
    if standalone_instagram:
        credentials = settings.instagram_app_credentials
        verify_token = (
            settings.instagram_verify_token.get_secret_value().strip()
            or settings.meta_verify_token.get_secret_value().strip()
        )
        public_id_prefix = "instagram_oauth"
        platform_family = "instagram"
    else:
        credentials = settings.facebook_app_credentials
        verify_token = settings.meta_verify_token.get_secret_value().strip()
        public_id_prefix = "meta_oauth"
        platform_family = "meta"
    if credentials is None or not verify_token:
        return None
    app_id, app_secret = credentials
    return MetaAppCredentials(
        app_id=app_id,
        app_secret=app_secret,
        verify_token=verify_token,
        public_id=tenant_public_id(public_id_prefix, tenant_id),
        platform_family=platform_family,
    )


async def _stored_credentials(*, tenant_id: str, platform_family: str) -> MetaAppCredentials | None:
    async with get_session_factory()() as session:
        apps = (
            (
                await session.execute(
                    select(models.PlatformApp)
                    .where(
                        models.PlatformApp.tenant_id == tenant_id,
                        models.PlatformApp.platform_family == platform_family,
                        models.PlatformApp.status == "active",
                    )
                    .order_by(models.PlatformApp.created_at)
                )
            )
            .scalars()
            .all()
        )
    for app in apps:
        try:
            bundle = decrypt_secret_bundle(app.credential_bundle)
        except ValueError:
            logger.warning("platform_app %s has undecryptable credential bundle", app.id)
            continue
        app_secret = str(bundle.get("app_secret", ""))
        verify_token = str(bundle.get("verify_token", ""))
        if app.external_app_id and app_secret and verify_token:
            return MetaAppCredentials(
                app_id=app.external_app_id,
                app_secret=app_secret,
                verify_token=verify_token,
                public_id=app.public_id,
                platform_family=platform_family,
            )
    return None


async def facebook_app_credentials(tenant_id: str) -> MetaAppCredentials | None:
    """Prefer deployment credentials, with a legacy PlatformApp fallback."""
    return _configured_credentials(
        tenant_id=tenant_id, standalone_instagram=False
    ) or await _stored_credentials(
        tenant_id=tenant_id,
        platform_family="meta",
    )


async def instagram_app_credentials(tenant_id: str) -> MetaAppCredentials | None:
    """Resolve the standalone Instagram Login app for one tenant."""
    return _configured_credentials(
        tenant_id=tenant_id, standalone_instagram=True
    ) or await _stored_credentials(
        tenant_id=tenant_id,
        platform_family="instagram",
    )
