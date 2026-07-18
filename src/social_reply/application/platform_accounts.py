import uuid
from dataclasses import dataclass

from sqlalchemy import select

from social_reply.domain.platform_accounts import LEGACY_ACTIVE_ACCOUNT_STATUSES
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.secret_crypto import decrypt_secret_bundle


@dataclass(frozen=True)
class PlatformAppRuntime:
    id: uuid.UUID
    tenant_id: str
    platform_family: str
    name: str
    external_app_id: str | None
    public_id: str
    credential_bundle_data: dict
    config: dict
    config_version: int
    status: str

    @property
    def credential_bundle(self) -> dict[str, str]:
        return decrypt_secret_bundle(self.credential_bundle_data)


@dataclass(frozen=True)
class PlatformAccountRuntime:
    id: uuid.UUID
    tenant_id: str
    brand_id: str
    platform: str
    platform_app_id: uuid.UUID | None
    name: str
    external_account_id: str | None
    public_id: str
    credential_bundle_data: dict
    webhook_secret_bundle_data: dict | None
    config: dict
    capability: dict
    config_version: int
    automation_default: str
    status: str

    @property
    def credential_bundle(self) -> dict[str, str]:
        return decrypt_secret_bundle(self.credential_bundle_data)

    @property
    def webhook_secret_bundle(self) -> dict[str, str]:
        return decrypt_secret_bundle(self.webhook_secret_bundle_data)

    @property
    def webhook_secret(self) -> str:
        # telegram 验签取单值；内联 bundle 后统一走 secret key（修正旧 read() 返回 JSON 串的 bug）
        return self.webhook_secret_bundle["secret"]

    @property
    def credential(self) -> str:
        bundle = self.credential_bundle
        return bundle.get("bot_token") or bundle["access_token"]


def _account_runtime(row: models.PlatformAccount) -> PlatformAccountRuntime | None:
    if not row.public_id or not row.credential_bundle:
        return None
    return PlatformAccountRuntime(
        id=row.id,
        tenant_id=row.tenant_id,
        brand_id=row.brand_id,
        platform=row.platform,
        platform_app_id=row.platform_app_id,
        name=row.name,
        external_account_id=row.external_account_id,
        public_id=row.public_id,
        credential_bundle_data=dict(row.credential_bundle or {}),
        webhook_secret_bundle_data=dict(row.webhook_secret_bundle)
        if row.webhook_secret_bundle
        else None,
        config=dict(row.config or {}),
        capability=dict(row.capability or {}),
        config_version=row.config_version,
        automation_default=row.automation_default,
        status=row.status,
    )


def _app_runtime(row: models.PlatformApp) -> PlatformAppRuntime:
    return PlatformAppRuntime(
        id=row.id,
        tenant_id=row.tenant_id,
        platform_family=row.platform_family,
        name=row.name,
        external_app_id=row.external_app_id,
        public_id=row.public_id,
        credential_bundle_data=dict(row.credential_bundle or {}),
        config=dict(row.config or {}),
        config_version=row.config_version,
        status=row.status,
    )


async def find_platform_account_by_public_id(
    *, platform: str, public_id: str
) -> PlatformAccountRuntime | None:
    async with get_session_factory()() as session:
        row = (
            await session.execute(
                select(models.PlatformAccount).where(
                    models.PlatformAccount.platform == platform,
                    models.PlatformAccount.public_id == public_id,
                    models.PlatformAccount.status.in_(LEGACY_ACTIVE_ACCOUNT_STATUSES),
                )
            )
        ).scalar_one_or_none()
    return _account_runtime(row) if row is not None else None


async def find_platform_account_by_external_id(
    *, platform: str, external_account_id: str, platform_app_id: uuid.UUID
) -> PlatformAccountRuntime | None:
    async with get_session_factory()() as session:
        row = (
            await session.execute(
                select(models.PlatformAccount).where(
                    models.PlatformAccount.platform == platform,
                    models.PlatformAccount.platform_app_id == platform_app_id,
                    models.PlatformAccount.external_account_id == external_account_id,
                    models.PlatformAccount.status.in_(LEGACY_ACTIVE_ACCOUNT_STATUSES),
                )
            )
        ).scalar_one_or_none()
    return _account_runtime(row) if row is not None else None


async def list_active_accounts_by_platform(platform: str) -> list[PlatformAccountRuntime]:
    """列出某平台所有活跃账号（DM 轮询等按平台批处理场景用）。"""
    async with get_session_factory()() as session:
        rows = (
            (
                await session.execute(
                    select(models.PlatformAccount).where(
                        models.PlatformAccount.platform == platform,
                        models.PlatformAccount.status.in_(LEGACY_ACTIVE_ACCOUNT_STATUSES),
                    )
                )
            )
            .scalars()
            .all()
        )
    return [rt for row in rows if (rt := _account_runtime(row)) is not None]


async def get_platform_account_runtime(account_id: uuid.UUID) -> PlatformAccountRuntime:
    async with get_session_factory()() as session:
        row = (
            await session.execute(
                select(models.PlatformAccount).where(models.PlatformAccount.id == account_id)
            )
        ).scalar_one()
    runtime = _account_runtime(row)
    if runtime is None:
        raise LookupError(f"platform_account_runtime_incomplete:{account_id}")
    return runtime


async def find_platform_app_by_public_id(
    *, platform_family: str, public_id: str
) -> PlatformAppRuntime | None:
    async with get_session_factory()() as session:
        row = (
            await session.execute(
                select(models.PlatformApp).where(
                    models.PlatformApp.platform_family == platform_family,
                    models.PlatformApp.public_id == public_id,
                    models.PlatformApp.status.in_(LEGACY_ACTIVE_ACCOUNT_STATUSES),
                )
            )
        ).scalar_one_or_none()
    return _app_runtime(row) if row is not None else None


async def get_platform_app_runtime(app_id: uuid.UUID) -> PlatformAppRuntime:
    async with get_session_factory()() as session:
        row = (
            await session.execute(select(models.PlatformApp).where(models.PlatformApp.id == app_id))
        ).scalar_one()
    return _app_runtime(row)
