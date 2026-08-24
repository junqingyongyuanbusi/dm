import hashlib
import secrets
import uuid
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from social_reply.domain.platform_accounts import (
    ACTIVE_ACCOUNT_STATUS,
    account_platform,
    canonical_account_status,
    normalize_account_capability,
)
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.secret_crypto import encrypt_secret_bundle


def make_public_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(12).replace('-', '_')}"


def tenant_public_id(prefix: str, tenant_id: str) -> str:
    safe_tenant = "".join(
        character
        if character.isascii() and (character.isalnum() or character in {"_", "-"})
        else "_"
        for character in tenant_id
    ).strip("_")
    safe_tenant = safe_tenant or "tenant"
    if safe_tenant != tenant_id:
        digest = hashlib.sha256(tenant_id.encode()).hexdigest()[:8]
        safe_tenant = f"{safe_tenant}_{digest}"
    return f"{prefix}_{safe_tenant}"


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
    allow_external_app_id_rotation: bool = False,
) -> tuple[uuid.UUID, str]:
    """幂等创建共享平台 App/Webhook；一个 App 可挂接多个平台账号。"""
    rotating_external_app_id = False
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
        if public_id and platform_family in {"meta", "instagram"}:
            shared_route_owner = (
                await session.execute(
                    select(models.PlatformApp).where(
                        models.PlatformApp.platform_family.in_(("meta", "instagram")),
                        models.PlatformApp.public_id == public_id,
                    )
                )
            ).scalar_one_or_none()
            if (
                shared_route_owner is not None
                and shared_route_owner.platform_family != platform_family
            ):
                raise ValueError("meta_webhook_public_id_collision")
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
                if not allow_external_app_id_rotation:
                    raise ValueError("platform_app_public_id_external_id_mismatch")
                if existing is not None and existing.id != public_id_owner.id:
                    raise ValueError("platform_app_external_id_already_bound")
                existing = public_id_owner
                rotating_external_app_id = True
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
        "status": (
            canonical_account_status(existing.status)
            if existing is not None
            else ACTIVE_ACCOUNT_STATUS
        ),
    }
    if rotating_external_app_id:
        async with get_session_factory()() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        select(models.PlatformApp)
                        .where(models.PlatformApp.id == app_id)
                        .with_for_update()
                    )
                ).scalar_one()
                conflict = (
                    await session.execute(
                        select(models.PlatformApp.id).where(
                            models.PlatformApp.tenant_id == tenant_id,
                            models.PlatformApp.platform_family == platform_family,
                            models.PlatformApp.external_app_id == external_app_id,
                            models.PlatformApp.id != app_id,
                        )
                    )
                ).scalar_one_or_none()
                if conflict is not None:
                    raise ValueError("platform_app_external_id_already_bound")
                row.name = name
                row.external_app_id = external_app_id
                row.credential_bundle = values["credential_bundle"]
                row.config = config
                row.config_version += 1
        return app_id, resolved_public_id

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
    status: str | None = None,
    provisioning_job_id: uuid.UUID | None = None,
    provisioning_attempt_count: int | None = None,
) -> tuple[uuid.UUID, str]:
    """幂等创建/更新直连账号；每个账号拥有独立凭证目录。"""
    platform = account_platform(platform).value
    capability = normalize_account_capability(platform, capability)
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
        "config_version": 1,
        "chatwoot_inbox_id": None,
        # Reauthorization must not silently change an operator-selected automation mode.
        "automation_default": (
            existing.automation_default if existing is not None else automation_default
        ),
        "status": (
            canonical_account_status(status)
            if status is not None
            else (
                canonical_account_status(existing.status)
                if existing is not None
                else ACTIVE_ACCOUNT_STATUS
            )
        ),
    }
    if (provisioning_job_id is None) != (provisioning_attempt_count is None):
        raise ValueError("provisioning_claim_fence_incomplete")

    async with get_session_factory()() as session:
        if provisioning_job_id is not None:
            claim = (
                await session.execute(
                    select(models.ProvisioningJob.id)
                    .where(
                        models.ProvisioningJob.id == provisioning_job_id,
                        models.ProvisioningJob.status == "PROCESSING",
                        models.ProvisioningJob.attempt_count == provisioning_attempt_count,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if claim is None:
                raise ValueError("provisioning_claim_lost")
        statement = pg_insert(models.PlatformAccount).values(**values)
        update_values = {
            key: value
            for key, value in values.items()
            if key not in {"id", "public_id", "config_version"}
        }
        update_values["public_id"] = func.coalesce(
            models.PlatformAccount.public_id,
            statement.excluded.public_id,
        )
        update_values["config_version"] = models.PlatformAccount.config_version + 1
        persisted = (
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=["tenant_id", "platform", "external_account_id"],
                    set_=update_values,
                ).returning(
                    models.PlatformAccount.id,
                    models.PlatformAccount.public_id,
                )
            )
        ).one()
        await session.commit()
    if persisted.public_id is None:
        raise RuntimeError("platform_account_public_id_missing")
    return persisted.id, persisted.public_id
