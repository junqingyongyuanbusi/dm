import secrets
import uuid
from pathlib import Path  # noqa: F401  secrets_root 参数签名保留（内联存储后不再使用）

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.secret_crypto import encrypt_secret_bundle


def make_public_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(12).replace('-', '_')}"


async def provision_platform_app(
    *,
    platform_family: str,
    external_app_id: str | None,
    tenant_id: str,
    name: str,
    public_id: str | None,
    public_id_prefix: str,
    secrets_root: Path,
    credential_bundle: dict[str, str],
    config: dict,
) -> tuple[uuid.UUID, str]:
    """幂等创建共享平台 App/Webhook；一个 App 可挂接多个平台账号。"""
    async with get_session_factory()() as session:
        existing = None
        if external_app_id:
            existing = (
                await session.execute(
                    select(models.PlatformApp).where(
                        models.PlatformApp.tenant_id == tenant_id,
                        models.PlatformApp.platform_family == platform_family,
                        models.PlatformApp.external_app_id == external_app_id,
                    )
                )
            ).scalar_one_or_none()
        elif public_id:
            existing = (
                await session.execute(
                    select(models.PlatformApp).where(
                        models.PlatformApp.tenant_id == tenant_id,
                        models.PlatformApp.platform_family == platform_family,
                        models.PlatformApp.public_id == public_id,
                    )
                )
            ).scalar_one_or_none()
        if external_app_id and public_id:
            public_id_owner = (
                await session.execute(
                    select(models.PlatformApp).where(
                        models.PlatformApp.tenant_id == tenant_id,
                        models.PlatformApp.platform_family == platform_family,
                        models.PlatformApp.public_id == public_id,
                    )
                )
            ).scalar_one_or_none()
            if public_id_owner is not None and public_id_owner.external_app_id != external_app_id:
                raise ValueError("platform_app_public_id_external_id_mismatch")
            if existing is not None and existing.public_id != public_id:
                raise ValueError("platform_app_public_id_is_immutable")

    app_id = existing.id if existing is not None else uuid.uuid4()
    resolved_public_id = public_id or (
        existing.public_id if existing is not None else make_public_id(public_id_prefix)
    )

    values = {
        "id": app_id,
        "tenant_id": tenant_id,
        "platform_family": platform_family,
        "name": name,
        "external_app_id": external_app_id or (existing.external_app_id if existing else None),
        "public_id": resolved_public_id,
        "credential_bundle": encrypt_secret_bundle(credential_bundle),
        "config": config,
        "config_version": existing.config_version + 1 if existing is not None else 1,
        "status": existing.status if existing is not None else "active",
    }
    async with get_session_factory()() as session:
        statement = pg_insert(models.PlatformApp).values(**values)
        if external_app_id:
            statement = statement.on_conflict_do_update(
                index_elements=["tenant_id", "platform_family", "external_app_id"],
                set_={key: value for key, value in values.items() if key != "id"},
            )
        else:
            statement = statement.on_conflict_do_update(
                index_elements=["tenant_id", "platform_family", "public_id"],
                set_={key: value for key, value in values.items() if key != "id"},
            )
        persisted_id = (
            await session.execute(statement.returning(models.PlatformApp.id))
        ).scalar_one()
        await session.commit()
        app_id = persisted_id
    return app_id, resolved_public_id


async def provision_direct_account(
    *,
    platform: str,
    external_account_id: str,
    tenant_id: str,
    brand_id: str,
    name: str,
    public_id: str | None,
    public_id_prefix: str,
    secrets_root: Path,
    credential_bundle: dict[str, str],
    webhook_secret_bundle: dict[str, str] | None,
    config: dict,
    capability: dict,
    automation_default: str,
    platform_app_id: uuid.UUID | None = None,
    preserve_existing_webhook_secret: bool = False,
) -> tuple[uuid.UUID, str]:
    """幂等创建/更新直连账号；每个账号拥有独立凭证目录。"""
    async with get_session_factory()() as session:
        existing = (
            await session.execute(
                select(models.PlatformAccount).where(
                    models.PlatformAccount.tenant_id == tenant_id,
                    models.PlatformAccount.platform == platform,
                    models.PlatformAccount.external_account_id == external_account_id,
                )
            )
        ).scalar_one_or_none()

    if existing is not None and public_id is not None and existing.public_id != public_id:
        raise ValueError("platform_account_public_id_is_immutable")

    account_id = existing.id if existing is not None else uuid.uuid4()
    resolved_public_id = public_id or (
        existing.public_id
        if existing is not None and existing.public_id
        else make_public_id(public_id_prefix)
    )

    # 非 rotate 且已有 secret 时复用旧 bundle；否则写入新 bundle
    webhook_bundle = existing.webhook_secret_bundle if existing is not None else None
    if webhook_secret_bundle is not None and not (
        existing is not None and preserve_existing_webhook_secret and webhook_bundle
    ):
        webhook_bundle = encrypt_secret_bundle(webhook_secret_bundle)

    values = {
        "id": account_id,
        "tenant_id": tenant_id,
        "brand_id": brand_id,
        "platform": platform,
        "platform_app_id": platform_app_id,
        "name": name,
        "external_account_id": external_account_id,
        "public_id": resolved_public_id,
        "credential_bundle": encrypt_secret_bundle(credential_bundle),
        "webhook_secret_bundle": webhook_bundle,
        "config": {"delivery_mode": "direct", **config},
        "capability": capability,
        "config_version": existing.config_version + 1 if existing is not None else 1,
        "chatwoot_inbox_id": None,
        "automation_default": automation_default,
        "status": existing.status if existing is not None else "active",
    }
    async with get_session_factory()() as session:
        account_id = (
            await session.execute(
                pg_insert(models.PlatformAccount)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=["tenant_id", "platform", "external_account_id"],
                    set_={key: value for key, value in values.items() if key != "id"},
                )
                .returning(models.PlatformAccount.id)
            )
        ).scalar_one()
        await session.commit()
    return account_id, resolved_public_id
