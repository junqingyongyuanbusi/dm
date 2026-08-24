import logging
import secrets
import unicodedata
import uuid
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy import select

from social_reply.application.account_management.jobs import (
    public_job,
    retry_provisioning_job,
    submit_provisioning_job,
)
from social_reply.application.account_management.submissions import split_submission
from social_reply.connectors.email.contracts import (
    MAX_EMAIL_CREDENTIAL_CHARS,
    MAX_EMAIL_MAILBOX_CHARS,
    MAX_SENDER_NAME_CHARS,
    normalize_email_address,
    validate_email_account_text,
)
from social_reply.connectors.email.network import (
    EmailNetworkError,
    normalize_hostname,
    require_allowed_host,
    validate_port,
)
from social_reply.connectors.feishu.contracts import (
    FEISHU_API_BASE_URL,
    FEISHU_APP_ID_PATTERN,
    FEISHU_GROUP_MODE,
)
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


_ACCOUNT_SCOPE_PATTERN = r"^[A-Za-z0-9_-]+$"


class _BaseAccountRequest(_StrictRequest):
    tenant_id: str = Field(
        default="default",
        min_length=1,
        max_length=64,
        pattern=_ACCOUNT_SCOPE_PATTERN,
    )
    brand_id: str = Field(
        default="default",
        min_length=1,
        max_length=64,
        pattern=_ACCOUNT_SCOPE_PATTERN,
    )
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
        settings = get_settings()
        if not self.app_id and not self.app_public_id:
            raise ValueError("app_id 或 app_public_id 至少填写一个")
        if not self.enable_dm:
            raise ValueError("Meta 接入必须启用文本私信")
        if "enable_comments" not in self.model_fields_set:
            self.enable_comments = settings.meta_comment_reply_enabled
        if self.enable_comments and not settings.meta_comment_reply_enabled:
            raise ValueError("Meta 评论回复未启用")
        if self.automation_default != "BOT_DRAFT_ONLY":
            raise ValueError("新接入的 Meta 账号必须使用 BOT_DRAFT_ONLY")
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


class EmailAccountRequest(_BaseAccountRequest):
    email_address: str
    # Validate secret content only after authentication, tenant authorization, and the feature
    # gate. Pydantic includes a failing field's raw input in 422 details, so these fields must not
    # raise schema-validation errors containing provider credentials.
    username: Any = Field(json_schema_extra={"writeOnly": True})
    password: Any = Field(json_schema_extra={"writeOnly": True})
    automation_default: str = Field(default="BOT_DRAFT_ONLY", max_length=64)
    imap_host: str
    imap_port: int = 993
    mailbox: str = Field(default="INBOX", min_length=1, max_length=MAX_EMAIL_MAILBOX_CHARS)
    smtp_host: str
    smtp_port: int | None = None
    smtp_security: Literal["ssl", "starttls"] = "ssl"
    from_name: str | None = Field(default=None, min_length=1, max_length=MAX_SENDER_NAME_CHARS)
    internal_domain_policy: Literal["ignore", "allow"] = "ignore"

    @field_validator("email_address")
    @classmethod
    def _canonicalize_email_address(cls, value: str) -> str:
        return normalize_email_address(value)

    @field_validator("imap_host", "smtp_host")
    @classmethod
    def _canonicalize_email_host(cls, value: str) -> str:
        return normalize_hostname(value)

    @field_validator("imap_port")
    @classmethod
    def _validate_imap_port(cls, value: int) -> int:
        return validate_port(value)

    @field_validator("smtp_port")
    @classmethod
    def _validate_smtp_port(cls, value: int | None) -> int | None:
        return validate_port(value) if value is not None else None

    @model_validator(mode="after")
    def _default_smtp_port(self) -> "EmailAccountRequest":
        if self.smtp_port is None:
            self.smtp_port = 465 if self.smtp_security == "ssl" else 587
        return self

    @field_validator("mailbox", "from_name")
    @classmethod
    def _validate_email_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if any(unicodedata.category(character) == "Cc" for character in value):
            raise ValueError("email_control_character_forbidden")
        if not value.strip():
            raise ValueError("email_text_blank")
        return value


class FeishuAccountRequest(_BaseAccountRequest):
    app_id: str = Field(pattern=FEISHU_APP_ID_PATTERN)
    app_secret: SecretStr = Field(min_length=1, max_length=512)
    verification_token: SecretStr = Field(min_length=1, max_length=512)
    encrypt_key: SecretStr = Field(min_length=1, max_length=512)
    api_base_url: Literal[FEISHU_API_BASE_URL] = FEISHU_API_BASE_URL
    group_mode: Literal[FEISHU_GROUP_MODE] = FEISHU_GROUP_MODE

    @model_validator(mode="after")
    def _validate_feishu_request(self) -> "FeishuAccountRequest":
        for field_name in ("app_secret", "verification_token", "encrypt_key"):
            value = getattr(self, field_name).get_secret_value()
            if not value.strip():
                raise ValueError(f"blank_{field_name}")
        if self.automation_default != "BOT_DRAFT_ONLY":
            raise ValueError("新接入的 Feishu 账号必须使用 BOT_DRAFT_ONLY")
        return self


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


def _validate_email_request_after_gates(
    request: EmailAccountRequest,
    *,
    allowed_hosts: frozenset[str],
) -> None:
    if request.automation_default != "BOT_DRAFT_ONLY":
        raise HTTPException(status_code=422, detail="email_requires_bot_draft_only")
    try:
        validate_email_account_text(request.username, maximum=MAX_EMAIL_CREDENTIAL_CHARS)
        validate_email_account_text(request.password, maximum=MAX_EMAIL_CREDENTIAL_CHARS)
        validate_email_account_text(request.mailbox, maximum=MAX_EMAIL_MAILBOX_CHARS)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid_email_account_request") from exc
    try:
        require_allowed_host(request.imap_host, allowed_hosts)
        require_allowed_host(request.smtp_host, allowed_hosts)
    except EmailNetworkError as exc:
        raise HTTPException(status_code=422, detail="email_hostname_not_allowed") from exc


def _split_request(platform: str, request: BaseModel) -> tuple[dict, dict[str, str]]:
    values = request.model_dump()
    for key, value in values.items():
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


@router.post("/email", response_model=ProvisioningJobResponse, status_code=202)
async def create_or_update_email_account(
    request: Request,
    principal: Annotated[ControlPrincipal, Depends(require_control_api_key)],
) -> ProvisioningJobResponse:
    try:
        raw_request = await request.json()
    except (UnicodeDecodeError, ValueError):
        raise HTTPException(status_code=422, detail="invalid_email_account_request") from None
    if not isinstance(raw_request, dict):
        raise HTTPException(status_code=422, detail="invalid_email_account_request")
    tenant_id = raw_request.get("tenant_id", "default")
    if not isinstance(tenant_id, str):
        raise HTTPException(status_code=422, detail="invalid_email_account_request")
    principal.require_tenant(tenant_id)
    settings = get_settings()
    if not settings.platform_integration_enabled("email"):
        raise HTTPException(status_code=503, detail="email_integration_disabled")
    try:
        email_request = EmailAccountRequest.model_validate(raw_request)
    except (ValidationError, ValueError):
        raise HTTPException(status_code=422, detail="invalid_email_account_request") from None
    _validate_email_request_after_gates(
        email_request,
        allowed_hosts=settings.email_allowed_hosts,
    )
    payload, secrets_bundle = _split_request("email", email_request)
    job_id = await submit_provisioning_job(
        tenant_id=email_request.tenant_id,
        brand_id=email_request.brand_id,
        platform="email",
        actor=principal.actor,
        request=payload,
        secrets=secrets_bundle,
    )
    await _enqueue(job_id)
    return _job_response(job_id)


@router.post("/feishu", response_model=ProvisioningJobResponse, status_code=202)
async def create_or_update_feishu_account(
    request: FeishuAccountRequest,
    principal: Annotated[ControlPrincipal, Depends(require_control_api_key)],
) -> ProvisioningJobResponse:
    principal.require_tenant(request.tenant_id)
    _require_platform_enabled("feishu")
    payload, secrets_bundle = _split_request("feishu", request)
    job_id = await submit_provisioning_job(
        tenant_id=request.tenant_id,
        brand_id=request.brand_id,
        platform="feishu",
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
