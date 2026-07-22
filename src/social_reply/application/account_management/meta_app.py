from pathlib import Path

from sqlalchemy import select

from social_reply.application.account_management.provisioning import provision_platform_app
from social_reply.application.platform_accounts import find_platform_app_by_public_id
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory


async def _find_platform_app_external_id(
    *, tenant_id: str, platform_family: str, public_id: str
) -> str | None:
    async with get_session_factory()() as session:
        return (
            await session.execute(
                select(models.PlatformApp.external_app_id).where(
                    models.PlatformApp.tenant_id == tenant_id,
                    models.PlatformApp.platform_family == platform_family,
                    models.PlatformApp.public_id == public_id,
                )
            )
        ).scalar_one_or_none()


async def provision_meta_app(
    *,
    tenant_id: str,
    app_id: str | None,
    app_public_id: str | None,
    app_name: str | None,
    app_secret: str,
    verify_token: str,
    secrets_root: Path,
    graph_base_url: str,
    api_version: str,
    platform_family: str = "meta",
) -> tuple[object, str, str]:
    app_id = app_id.strip() if app_id else None
    app_public_id = app_public_id.strip() if app_public_id else None
    if not app_id and not app_public_id:
        raise ValueError("meta_app_id_or_public_id_required")
    existing_app = None
    if app_public_id:
        existing_app = await find_platform_app_by_public_id(
            platform_family=platform_family,
            public_id=app_public_id,
            tenant_id=tenant_id,
        )
        if not app_id:
            app_id = await _find_platform_app_external_id(
                tenant_id=tenant_id,
                platform_family=platform_family,
                public_id=app_public_id,
            )
            if not app_id:
                raise LookupError(f"meta_app_not_found:{app_public_id}")
    elif app_id:
        async with get_session_factory()() as session:
            row = (
                await session.execute(
                    select(models.PlatformApp).where(
                        models.PlatformApp.tenant_id == tenant_id,
                        models.PlatformApp.platform_family == platform_family,
                        models.PlatformApp.external_app_id == app_id,
                    )
                )
            ).scalar_one_or_none()
        if row is not None:
            existing_app = await find_platform_app_by_public_id(
                platform_family=platform_family,
                public_id=row.public_id,
                tenant_id=tenant_id,
            )
            app_public_id = row.public_id

    resolved_verify_token = verify_token.strip()
    if not resolved_verify_token:
        raise ValueError("missing_meta_verify_token")
    if existing_app is not None:
        stored_token = existing_app.credential_bundle.get("verify_token")
        if resolved_verify_token is not None and stored_token != resolved_verify_token:
            raise ValueError("meta_verify_token_rotation_not_supported")
        resolved_verify_token = stored_token
    platform_app_id, resolved_public_id = await provision_platform_app(
        platform_family=platform_family,
        external_app_id=app_id,
        tenant_id=tenant_id,
        name=app_name or f"Meta App {app_id}",
        public_id=app_public_id,
        public_id_prefix="meta" if platform_family == "meta" else "igapp",
        secrets_root=secrets_root,
        credential_bundle={"app_secret": app_secret, "verify_token": resolved_verify_token},
        config={"graph_base_url": graph_base_url, "api_version": api_version},
    )
    return platform_app_id, resolved_public_id, resolved_verify_token
