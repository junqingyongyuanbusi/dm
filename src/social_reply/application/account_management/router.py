import logging
import secrets
import uuid
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from sqlalchemy import select

from social_reply.application.account_management.jobs import (
    public_job,
    retry_provisioning_job,
    submit_provisioning_job,
)
from social_reply.application.account_management.submissions import split_submission
from social_reply.domain.platform_accounts import (
    ACTIVE_ACCOUNT_STATUS,
    DISABLED_ACCOUNT_STATUS,
)
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.queue.dispatch import dispatch_actor
from social_reply.shared.config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/platform-accounts", tags=["platform-accounts"])


class _StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _BaseAccountRequest(_StrictRequest):
    tenant_id: str = Field(default="default", min_length=1, max_length=64)
    brand_id: str = Field(default="default", min_length=1, max_length=64)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    public_id: str | None = Field(default=None, min_length=1, max_length=128)
    automation_default: Literal["BOT_ACTIVE", "BOT_DRAFT_ONLY"] = "BOT_DRAFT_ONLY"
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=255)


class TelegramAccountRequest(_BaseAccountRequest):
    token: SecretStr
    rotate_webhook_secret: bool = False
    drop_pending_updates: bool = False


class MetaAccountRequest(_BaseAccountRequest):
    platform: Literal["facebook", "instagram"]
    external_account_id: str = Field(min_length=1, max_length=255)
    access_token: SecretStr
    app_secret: SecretStr
    app_id: str | None = Field(default=None, min_length=1, max_length=255)
    app_public_id: str | None = Field(default=None, min_length=1, max_length=128)
    app_name: str | None = Field(default=None, min_length=1, max_length=255)
    verify_token: SecretStr
    api_version: str = Field(default="v23.0", pattern=r"^v\d+\.\d+$")
    instagram_login_mode: Literal["facebook_login", "instagram_login"] = "facebook_login"
    page_id: str | None = Field(default=None, min_length=1, max_length=255)
    enable_dm: bool = True
    enable_comments: bool = False

    @model_validator(mode="after")
    def _validate_meta_request(self) -> "MetaAccountRequest":
        if not self.app_id and not self.app_public_id:
            raise ValueError("app_id 或 app_public_id 至少填写一个")
        if not self.enable_dm or self.enable_comments:
            raise ValueError("当前 Meta 接入仅允许文本私信")
        if not get_settings().meta_automation_default_allowed(
            self.platform, self.automation_default
        ):
            raise ValueError("Meta 接入必须使用 BOT_DRAFT_ONLY（未开启 META_AUTO_REPLY_ENABLED）")
        if self.platform == "facebook" and self.instagram_login_mode != "facebook_login":
            raise ValueError("Facebook 账号必须使用 facebook_login")
        if (
            self.platform == "instagram"
            and self.instagram_login_mode == "facebook_login"
            and not self.page_id
        ):
            raise ValueError("Facebook Login Instagram 必须填写 page_id")
        if (
            self.platform == "instagram"
            and self.instagram_login_mode == "instagram_login"
            and self.page_id
        ):
            raise ValueError("Instagram Login 不允许填写 page_id")
        return self


class WhatsAppAccountRequest(_BaseAccountRequest):
    external_account_id: str = Field(min_length=1, max_length=255)
    access_token: SecretStr
    app_secret: SecretStr
    app_id: str | None = Field(default=None, min_length=1, max_length=255)
    app_public_id: str | None = Field(default=None, min_length=1, max_length=128)
    app_name: str | None = Field(default=None, min_length=1, max_length=255)
    verify_token: SecretStr
    api_version: str = Field(default="v23.0", pattern=r"^v\d+\.\d+$")

    @model_validator(mode="after")
    def _validate_whatsapp_request(self) -> "WhatsAppAccountRequest":
        if not self.app_id and not self.app_public_id:
            raise ValueError("app_id 或 app_public_id 至少填写一个")
        return self


class XAccountRequest(_BaseAccountRequest):
    consumer_key: SecretStr
    consumer_secret: SecretStr
    access_token: SecretStr
    access_token_secret: SecretStr
    environment: str = Field(default="oauth", min_length=1, max_length=128)
    xchat_pin: SecretStr | None = None


class ProvisioningJobResponse(BaseModel):
    job_id: str
    status: str
    status_url: str


@dataclass(frozen=True)
class ControlPrincipal:
    actor: str
    allowed_tenants: frozenset[str]

    def require_tenant(self, tenant_id: str) -> None:
        if tenant_id not in self.allowed_tenants:
            raise HTTPException(status_code=403, detail="tenant_access_denied")


def require_control_api_key(
    authorization: Annotated[str | None, Header()] = None,
    x_control_api_key: Annotated[str | None, Header()] = None,
) -> ControlPrincipal:
    settings = get_settings()
    expected = settings.control_api_key.get_secret_value()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="control_api_not_configured",
        )
    presented = x_control_api_key
    if authorization and authorization.startswith("Bearer "):
        presented = authorization.removeprefix("Bearer ").strip()
    if not presented or not secrets.compare_digest(presented, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_control_api_key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return ControlPrincipal(
        actor="service:control_api", allowed_tenants=settings.allowed_admin_tenants
    )


def _require_platform_enabled(platform: str) -> None:
    if not get_settings().platform_integration_enabled(platform):
        raise HTTPException(status_code=503, detail=f"{platform}_integration_disabled")


def _split_request(platform: str, request: BaseModel) -> tuple[dict, dict[str, str]]:
    values = request.model_dump()
    for key in (
        "token",
        "access_token",
        "app_secret",
        "verify_token",
        "consumer_key",
        "consumer_secret",
        "access_token_secret",
        "xchat_pin",
    ):
        value = values.get(key)
        if isinstance(value, SecretStr):
            values[key] = value.get_secret_value()
    return split_submission(platform, values)


def _job_response(job_id: uuid.UUID) -> ProvisioningJobResponse:
    return ProvisioningJobResponse(
        job_id=str(job_id),
        status="PENDING",
        status_url=f"/api/v1/platform-accounts/jobs/{job_id}",
    )


async def _enqueue(job_id: uuid.UUID) -> None:
    from social_reply.application.account_management.actors import process_platform_provisioning

    await dispatch_actor(process_platform_provisioning, str(job_id))


@router.post("/telegram", response_model=ProvisioningJobResponse, status_code=202)
async def create_or_update_telegram_account(
    request: TelegramAccountRequest,
    principal: Annotated[ControlPrincipal, Depends(require_control_api_key)],
) -> ProvisioningJobResponse:
    principal.require_tenant(request.tenant_id)
    payload, secrets_bundle = _split_request("telegram", request)
    job_id = await submit_provisioning_job(
        tenant_id=request.tenant_id,
        brand_id=request.brand_id,
        platform="telegram",
        actor=principal.actor,
        request=payload,
        secrets=secrets_bundle,
    )
    await _enqueue(job_id)
    return _job_response(job_id)


@router.post("/meta", response_model=ProvisioningJobResponse, status_code=202)
async def create_or_update_meta_account(
    request: MetaAccountRequest,
    principal: Annotated[ControlPrincipal, Depends(require_control_api_key)],
) -> ProvisioningJobResponse:
    _require_platform_enabled(request.platform)
    principal.require_tenant(request.tenant_id)
    payload, secrets_bundle = _split_request(request.platform, request)
    job_id = await submit_provisioning_job(
        tenant_id=request.tenant_id,
        brand_id=request.brand_id,
        platform=request.platform,
        actor=principal.actor,
        request=payload,
        secrets=secrets_bundle,
    )
    await _enqueue(job_id)
    return _job_response(job_id)


@router.post("/whatsapp", response_model=ProvisioningJobResponse, status_code=202)
async def create_or_update_whatsapp_account(
    request: WhatsAppAccountRequest,
    principal: Annotated[ControlPrincipal, Depends(require_control_api_key)],
) -> ProvisioningJobResponse:
    _require_platform_enabled("whatsapp")
    principal.require_tenant(request.tenant_id)
    payload, secrets_bundle = _split_request("whatsapp", request)
    job_id = await submit_provisioning_job(
        tenant_id=request.tenant_id,
        brand_id=request.brand_id,
        platform="whatsapp",
        actor=principal.actor,
        request=payload,
        secrets=secrets_bundle,
    )
    await _enqueue(job_id)
    return _job_response(job_id)


@router.post("/x", response_model=ProvisioningJobResponse, status_code=202)
async def create_or_update_x_account(
    request: XAccountRequest,
    principal: Annotated[ControlPrincipal, Depends(require_control_api_key)],
) -> ProvisioningJobResponse:
    settings = get_settings()
    if not settings.x_integration_enabled:
        raise HTTPException(status_code=503, detail="x_integration_disabled")
    if request.xchat_pin is not None and not settings.xchat_enabled:
        raise HTTPException(status_code=422, detail="xchat_disabled")
    principal.require_tenant(request.tenant_id)
    payload, secrets_bundle = _split_request("x", request)
    job_id = await submit_provisioning_job(
        tenant_id=request.tenant_id,
        brand_id=request.brand_id,
        platform="x",
        actor=principal.actor,
        request=payload,
        secrets=secrets_bundle,
    )
    await _enqueue(job_id)
    return _job_response(job_id)


@router.get("/jobs/{job_id}")
async def get_provisioning_job(
    job_id: uuid.UUID,
    principal: Annotated[ControlPrincipal, Depends(require_control_api_key)],
) -> dict:
    async with get_session_factory()() as session:
        row = await session.get(models.ProvisioningJob, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="provisioning_job_not_found")
    principal.require_tenant(row.tenant_id)
    return public_job(row)


@router.get("")
async def list_platform_accounts(
    principal: Annotated[ControlPrincipal, Depends(require_control_api_key)],
    tenant_id: str | None = Query(default=None),
) -> list[dict]:
    async with get_session_factory()() as session:
        statement = select(models.PlatformAccount).order_by(
            models.PlatformAccount.created_at.desc()
        )
        if tenant_id:
            principal.require_tenant(tenant_id)
            statement = statement.where(models.PlatformAccount.tenant_id == tenant_id)
        else:
            statement = statement.where(
                models.PlatformAccount.tenant_id.in_(principal.allowed_tenants)
            )
        rows = list((await session.execute(statement)).scalars())
    return [
        {
            "id": str(row.id),
            "tenant_id": row.tenant_id,
            "brand_id": row.brand_id,
            "platform": row.platform,
            "name": row.name,
            "external_account_id": row.external_account_id,
            "public_id": row.public_id,
            "status": row.status,
            "automation_default": row.automation_default,
            "capability": dict(row.capability or {}),
            "config_version": row.config_version,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]


@router.post("/jobs/{job_id}/retry", status_code=202)
async def retry_job(
    job_id: uuid.UUID,
    principal: Annotated[ControlPrincipal, Depends(require_control_api_key)],
) -> ProvisioningJobResponse:
    async with get_session_factory()() as session:
        job = await session.get(models.ProvisioningJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="provisioning_job_not_found")
    _require_platform_enabled(job.platform)
    principal.require_tenant(job.tenant_id)
    try:
        await retry_provisioning_job(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await _enqueue(job_id)
    return _job_response(job_id)


async def _set_account_status(
    account_id: uuid.UUID, principal: ControlPrincipal, target_status: str
) -> str:
    async with get_session_factory()() as session:
        row = await session.get(models.PlatformAccount, account_id)
        if row is None:
            raise HTTPException(status_code=404, detail="platform_account_not_found")
        principal.require_tenant(row.tenant_id)
        row.status = target_status
        row.config_version += 1
        await session.commit()
    return target_status


@router.post("/{account_id}/disable")
async def disable_account(
    account_id: uuid.UUID,
    principal: Annotated[ControlPrincipal, Depends(require_control_api_key)],
) -> dict[str, str]:
    return {"status": await _set_account_status(account_id, principal, DISABLED_ACCOUNT_STATUS)}


@router.post("/{account_id}/enable")
async def enable_account(
    account_id: uuid.UUID,
    principal: Annotated[ControlPrincipal, Depends(require_control_api_key)],
) -> dict[str, str]:
    return {"status": await _set_account_status(account_id, principal, ACTIVE_ACCOUNT_STATUS)}
