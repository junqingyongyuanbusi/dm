import re
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, SecretStr, ValidationError
from sqlalchemy import select

from social_reply.application.account_management.jobs import (
    process_provisioning_job,
    requires_secret_resubmission,
    retry_provisioning_job,
    submit_provisioning_job,
    validate_owner_provisioning_policy,
)
from social_reply.application.account_management.kill_switch_recovery import (
    ACCOUNT_KILL_SWITCH_ACTION,
    acquire_account_kill_switch_lock,
    build_pending_account_kill_switch_detail,
    next_account_kill_switch_sequence,
    reconcile_account_kill_switch_command,
)
from social_reply.application.account_management.router import (
    EmailAccountRequest,
    FeishuAccountRequest,
    MetaAccountRequest,
    TelegramAccountRequest,
    WhatsAppAccountRequest,
    XAccountRequest,
    _validate_email_request_after_gates,
)
from social_reply.application.account_management.service import enable_xchat_for_account
from social_reply.application.account_management.submissions import split_submission
from social_reply.domain.platform_accounts import ACTIVE_ACCOUNT_STATUS, DISABLED_ACCOUNT_STATUS
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.queue.dispatch import dispatch_actor
from social_reply.shared.config import get_settings

# "ADMIN" is an internal Tenant-wide capability label projected only from the
# environment SUPERADMIN. It is not a persistable admin_users role.
ChannelRole = Literal["USER", "ADMIN"]
AutomationTarget = Literal["BOT_ACTIVE", "BOT_DRAFT_ONLY"]

_XCHAT_PIN_PATTERN = re.compile(r"^[0-9]{4}$")


class ChannelManagementError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ChannelNotFoundError(ChannelManagementError):
    pass


class ChannelConflictError(ChannelManagementError):
    pass


class ChannelValidationError(ChannelManagementError):
    pass


class ChannelPermissionError(ChannelManagementError):
    pass


@dataclass(frozen=True)
class ChannelActor:
    actor: str
    role: ChannelRole
    user_id: uuid.UUID | None
    session_id: uuid.UUID

    @property
    def is_admin(self) -> bool:
        return self.role == "ADMIN"


@dataclass(frozen=True)
class ProvisioningCommand:
    tenant_id: str
    brand_id: str
    platform: str
    actor: ChannelActor
    public_values: dict[str, Any]
    secret_values: dict[str, str]


_PLATFORM_REQUEST_MODELS: dict[str, type[BaseModel]] = {
    "telegram": TelegramAccountRequest,
    "facebook": MetaAccountRequest,
    "instagram": MetaAccountRequest,
    "whatsapp": WhatsAppAccountRequest,
    "x": XAccountRequest,
    "feishu": FeishuAccountRequest,
    "email": EmailAccountRequest,
}


def _normalized_form_values(
    *,
    route_tenant_id: str,
    platform: str,
    values: dict[str, Any],
) -> dict[str, Any]:
    try:
        request_model = _PLATFORM_REQUEST_MODELS[platform]
    except KeyError as exc:
        raise ChannelValidationError(f"unsupported_platform:{platform}") from exc
    allowed_fields = set(request_model.model_fields) | {"csrf_token"}
    unexpected_fields = sorted(set(values) - allowed_fields)
    if unexpected_fields:
        raise ChannelValidationError("invalid_channel_account_form_fields")

    normalized_values = {
        key: value
        for key, value in values.items()
        if key != "csrf_token" and value is not None
    }
    normalized_values["tenant_id"] = route_tenant_id
    normalized_values["brand_id"] = str(normalized_values.get("brand_id") or "default")
    if platform in {"facebook", "instagram"}:
        normalized_values["platform"] = platform
    for optional_field in (
        "name",
        "public_id",
        "idempotency_key",
        "from_name",
        "smtp_port",
        "app_id",
        "app_public_id",
        "app_name",
        "page_id",
        "xchat_pin",
    ):
        if optional_field in normalized_values and not str(
            normalized_values[optional_field]
        ).strip():
            normalized_values.pop(optional_field)
    return normalized_values


def _plain_model_values(request_model: BaseModel) -> dict[str, Any]:
    values = request_model.model_dump()
    return {
        key: value.get_secret_value() if isinstance(value, SecretStr) else value
        for key, value in values.items()
    }


def build_provisioning_command(
    *,
    route_tenant_id: str,
    platform: str,
    actor: ChannelActor,
    values: dict[str, Any],
) -> ProvisioningCommand:
    try:
        validate_owner_provisioning_policy(
            platform=platform,
            owner_user_id=None if actor.is_admin else actor.user_id,
            request=values,
        )
    except ValueError as exc:
        raise ChannelPermissionError("tenant_admin_required") from exc
    settings = get_settings()
    if not settings.platform_integration_enabled(platform):
        raise ChannelValidationError(f"{platform}_integration_disabled")
    normalized_values = _normalized_form_values(
        route_tenant_id=route_tenant_id,
        platform=platform,
        values=values,
    )
    request_model_type = _PLATFORM_REQUEST_MODELS[platform]
    try:
        request_model = request_model_type.model_validate(normalized_values)
    except ValidationError as exc:
        raise ChannelValidationError("invalid_channel_account_form") from exc
    if isinstance(request_model, EmailAccountRequest):
        try:
            _validate_email_request_after_gates(
                request_model,
                allowed_hosts=settings.email_allowed_hosts,
            )
        except Exception as exc:
            raise ChannelValidationError("invalid_email_account_request") from exc
    plain_values = _plain_model_values(request_model)
    public_values, secret_values = split_submission(platform, plain_values)
    return ProvisioningCommand(
        tenant_id=route_tenant_id,
        brand_id=str(plain_values.get("brand_id") or "default"),
        platform=platform,
        actor=actor,
        public_values=public_values,
        secret_values=secret_values,
    )


async def submit_channel_provisioning(command: ProvisioningCommand) -> uuid.UUID:
    try:
        job_id = await submit_provisioning_job(
            tenant_id=command.tenant_id,
            brand_id=command.brand_id,
            platform=command.platform,
            actor=command.actor.actor,
            request=command.public_values,
            secrets=command.secret_values,
            admin_session_id=command.actor.session_id,
        )
    except PermissionError as exc:
        raise ChannelPermissionError(str(exc)) from exc
    except ValueError as exc:
        error_code = str(exc)
        if error_code in {
            "idempotency_key_payload_mismatch",
            "provisioning_secret_resubmission_required",
        }:
            raise ChannelConflictError(error_code) from exc
        raise ChannelValidationError(error_code) from exc
    from social_reply.application.account_management.actors import process_platform_provisioning

    await dispatch_actor(
        process_platform_provisioning,
        str(job_id),
        inline=lambda: process_provisioning_job(str(job_id)),
    )
    return job_id


def _account_scope_statement(
    *,
    tenant_id: str,
    account_id: uuid.UUID,
    actor: ChannelActor,
):
    statement = select(models.PlatformAccount).where(
        models.PlatformAccount.id == account_id,
        models.PlatformAccount.tenant_id == tenant_id,
    )
    if not actor.is_admin:
        if actor.user_id is None:
            raise ChannelPermissionError("channel_user_identity_required")
        statement = statement.where(models.PlatformAccount.owner_user_id == actor.user_id)
    return statement


async def _locked_account(
    session,
    *,
    tenant_id: str,
    account_id: uuid.UUID,
    actor: ChannelActor,
) -> models.PlatformAccount:
    account = (
        await session.execute(
            _account_scope_statement(
                tenant_id=tenant_id,
                account_id=account_id,
                actor=actor,
            ).with_for_update()
        )
    ).scalar_one_or_none()
    if account is None:
        raise ChannelNotFoundError("platform_account_not_found")
    return account


def _require_config_version(
    account: models.PlatformAccount,
    expected_config_version: int,
) -> None:
    if expected_config_version < 1:
        raise ChannelValidationError("invalid_account_config_version")
    if account.config_version != expected_config_version:
        raise ChannelConflictError("platform_account_config_version_conflict")


def _add_account_audit(
    session,
    *,
    tenant_id: str,
    actor: ChannelActor,
    action: str,
    account_id: uuid.UUID,
    detail: dict[str, Any],
    audit_id: uuid.UUID | None = None,
) -> models.AuditLog:
    audit_values: dict[str, Any] = {
        "tenant_id": tenant_id,
        "category": "account_management",
        "actor": actor.actor,
        "action": action,
        "subject_type": "platform_account",
        "subject_id": str(account_id),
        "detail": detail,
    }
    if audit_id is not None:
        audit_values["id"] = audit_id
    audit = models.AuditLog(
        **audit_values,
    )
    session.add(audit)
    return audit


async def rename_channel_account(
    *,
    tenant_id: str,
    account_id: uuid.UUID,
    actor: ChannelActor,
    name: str,
    expected_config_version: int,
) -> None:
    normalized_name = name.strip()
    if not normalized_name or len(normalized_name) > 255:
        raise ChannelValidationError("invalid_platform_account_name")
    async with get_session_factory()() as session:
        account = await _locked_account(
            session,
            tenant_id=tenant_id,
            account_id=account_id,
            actor=actor,
        )
        _require_config_version(account, expected_config_version)
        previous_name = account.name
        changed = previous_name != normalized_name
        if changed:
            account.name = normalized_name
            account.config_version += 1
        _add_account_audit(
            session,
            tenant_id=tenant_id,
            actor=actor,
            action="RENAME_PLATFORM_ACCOUNT",
            account_id=account_id,
            detail={
                "previous_name": previous_name,
                "name": normalized_name,
                "changed": changed,
                "config_version": account.config_version,
            },
        )
        await session.commit()


async def set_channel_account_status(
    *,
    tenant_id: str,
    account_id: uuid.UUID,
    actor: ChannelActor,
    enabled: bool,
    expected_config_version: int,
    expected_status: str,
) -> None:
    target_status = ACTIVE_ACCOUNT_STATUS if enabled else DISABLED_ACCOUNT_STATUS
    if expected_status not in {ACTIVE_ACCOUNT_STATUS, DISABLED_ACCOUNT_STATUS}:
        raise ChannelValidationError("invalid_platform_account_expected_status")
    async with get_session_factory()() as session:
        account = await _locked_account(
            session,
            tenant_id=tenant_id,
            account_id=account_id,
            actor=actor,
        )
        _require_config_version(account, expected_config_version)
        if account.status != expected_status:
            raise ChannelConflictError("platform_account_status_conflict")
        previous_status = account.status
        changed = previous_status != target_status
        if changed:
            account.status = target_status
            account.config_version += 1
        _add_account_audit(
            session,
            tenant_id=tenant_id,
            actor=actor,
            action="SET_PLATFORM_ACCOUNT_STATUS",
            account_id=account_id,
            detail={
                "previous_status": previous_status,
                "status": target_status,
                "enabled": enabled,
                "changed": changed,
                "config_version": account.config_version,
            },
        )
        await session.commit()


async def set_channel_account_automation(
    *,
    tenant_id: str,
    account_id: uuid.UUID,
    actor: ChannelActor,
    target: AutomationTarget,
    expected_config_version: int,
) -> None:
    if target not in {"BOT_ACTIVE", "BOT_DRAFT_ONLY"}:
        raise ChannelValidationError("invalid_automation_target")
    async with get_session_factory()() as session:
        account = await _locked_account(
            session,
            tenant_id=tenant_id,
            account_id=account_id,
            actor=actor,
        )
        if not actor.is_admin and target != "BOT_DRAFT_ONLY":
            raise ChannelPermissionError("tenant_admin_required")
        _require_config_version(account, expected_config_version)
        if not get_settings().automation_default_allowed(account.platform, target):
            raise ChannelValidationError("automation_target_disabled")
        previous_target = account.automation_default
        changed = previous_target != target
        if changed:
            account.automation_default = target
            account.config_version += 1
        _add_account_audit(
            session,
            tenant_id=tenant_id,
            actor=actor,
            action="SET_PLATFORM_ACCOUNT_AUTOMATION",
            account_id=account_id,
            detail={
                "previous_target": previous_target,
                "target": target,
                "changed": changed,
                "config_version": account.config_version,
            },
        )
        await session.commit()


async def assign_channel_account_owner(
    *,
    tenant_id: str,
    account_id: uuid.UUID,
    actor: ChannelActor,
    owner_user_id: uuid.UUID | None,
    expected_config_version: int,
) -> None:
    if not actor.is_admin:
        raise ChannelPermissionError("tenant_admin_required")
    async with get_session_factory()() as session:
        account = await _locked_account(
            session,
            tenant_id=tenant_id,
            account_id=account_id,
            actor=actor,
        )
        _require_config_version(account, expected_config_version)
        if owner_user_id is not None:
            owner = await session.scalar(
                select(models.AdminUser).where(
                    models.AdminUser.id == owner_user_id,
                    models.AdminUser.tenant_id == tenant_id,
                    models.AdminUser.role == "USER",
                    models.AdminUser.status == "active",
                )
            )
            if owner is None:
                raise ChannelValidationError("platform_account_owner_not_assignable")
        previous_owner_user_id = account.owner_user_id
        changed = previous_owner_user_id != owner_user_id
        if changed:
            account.owner_user_id = owner_user_id
            account.config_version += 1
        _add_account_audit(
            session,
            tenant_id=tenant_id,
            actor=actor,
            action="ASSIGN_PLATFORM_ACCOUNT_OWNER",
            account_id=account_id,
            detail={
                "previous_owner_user_id": (
                    str(previous_owner_user_id) if previous_owner_user_id else None
                ),
                "owner_user_id": str(owner_user_id) if owner_user_id else None,
                "changed": changed,
                "config_version": account.config_version,
            },
        )
        await session.commit()


async def set_channel_account_kill_switch(
    *,
    tenant_id: str,
    account_id: uuid.UUID,
    actor: ChannelActor,
    enabled: bool,
) -> None:
    operation_id = uuid.uuid4()
    async with get_session_factory()() as session:
        await acquire_account_kill_switch_lock(
            session,
            tenant_id=tenant_id,
            account_id=account_id,
        )
        account = await _locked_account(
            session,
            tenant_id=tenant_id,
            account_id=account_id,
            actor=actor,
        )
        account_sequence = await next_account_kill_switch_sequence(
            session,
            tenant_id=tenant_id,
            account_id=account_id,
        )
        _add_account_audit(
            session,
            tenant_id=tenant_id,
            actor=actor,
            action=ACCOUNT_KILL_SWITCH_ACTION,
            account_id=account_id,
            detail=build_pending_account_kill_switch_detail(
                operation_id=operation_id,
                tenant_id=tenant_id,
                account_id=account_id,
                target_enabled=enabled,
                account_sequence=account_sequence,
                actor_role=actor.role,
                owner_user_id=account.owner_user_id,
            ),
            audit_id=operation_id,
        )
        await session.commit()

    await reconcile_account_kill_switch_command(
        operation_id,
        raise_on_redis_error=True,
    )


async def retry_channel_job(
    *,
    tenant_id: str,
    job_id: uuid.UUID,
    actor: ChannelActor,
) -> None:
    async with get_session_factory()() as session:
        statement = select(models.ProvisioningJob).where(
            models.ProvisioningJob.id == job_id,
            models.ProvisioningJob.tenant_id == tenant_id,
        )
        if not actor.is_admin:
            statement = statement.where(models.ProvisioningJob.owner_user_id == actor.user_id)
        job = await session.scalar(statement)
    if job is None:
        raise ChannelNotFoundError("provisioning_job_not_found")
    if requires_secret_resubmission(job):
        raise ChannelConflictError("provisioning_secret_resubmission_required")
    try:
        validate_owner_provisioning_policy(
            platform=job.platform,
            owner_user_id=job.owner_user_id,
            request=dict(job.request or {}),
        )
    except ValueError as exc:
        raise ChannelPermissionError("tenant_admin_required") from exc
    if not get_settings().platform_integration_enabled(job.platform):
        raise ChannelValidationError(f"{job.platform}_integration_disabled")
    try:
        await retry_provisioning_job(
            job_id,
            tenant_id=tenant_id,
            owner_user_id=None if actor.is_admin else actor.user_id,
            scope_owner=not actor.is_admin,
            actor=actor.actor,
        )
    except LookupError as exc:
        raise ChannelNotFoundError("provisioning_job_not_found") from exc
    except ValueError as exc:
        raise ChannelConflictError(str(exc)) from exc
    from social_reply.application.account_management.actors import process_platform_provisioning

    await dispatch_actor(
        process_platform_provisioning,
        str(job_id),
        inline=lambda: process_provisioning_job(str(job_id)),
    )


async def repair_channel_xchat(
    *,
    tenant_id: str,
    account_id: uuid.UUID,
    actor: ChannelActor,
    pin: str,
) -> None:
    if not get_settings().xchat_enabled:
        raise ChannelValidationError("xchat_disabled")
    if not _XCHAT_PIN_PATTERN.fullmatch(pin):
        raise ChannelValidationError("invalid_xchat_pin")
    async with get_session_factory()() as session:
        account = await session.scalar(
            _account_scope_statement(
                tenant_id=tenant_id,
                account_id=account_id,
                actor=actor,
            )
        )
        if (
            account is None
            or account.platform != "x"
            or account.status != ACTIVE_ACCOUNT_STATUS
        ):
            raise ChannelNotFoundError("x_account_not_found")
    await enable_xchat_for_account(account_id=account_id, pin=pin)
    async with get_session_factory()() as session:
        _add_account_audit(
            session,
            tenant_id=tenant_id,
            actor=actor,
            action="REPAIR_XCHAT_ACCOUNT",
            account_id=account_id,
            detail={"outcome": "completed"},
        )
        await session.commit()
