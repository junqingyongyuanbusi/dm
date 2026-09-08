import re
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, SecretStr, ValidationError
from sqlalchemy import select

from social_reply.application.account_management.access import (
    lock_session_authorities,
    lock_user_authority,
    require_reauthorization,
    user_can_access_account,
)
from social_reply.application.account_management.auth import Principal, principal_from_session_row
from social_reply.application.account_management.jobs import (
    canonical_provisioning_operation,
    process_provisioning_job,
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
from social_reply.application.account_management.xchat_activation import XChatActivationError
from social_reply.application.handoff_notifications.service import (
    advance_handoff_notification_for_work,
)
from social_reply.application.message_delivery.intents import OutboxActor, OutboxOrigin
from social_reply.domain.platform_accounts import ACTIVE_ACCOUNT_STATUS, DISABLED_ACCOUNT_STATUS
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.advisory_locks import (
    acquire_conversation_delivery_xact_lock,
)
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.queue.dispatch import dispatch_actor
from social_reply.shared.config import get_settings

# "ADMIN" is an internal Tenant-wide capability label projected only from the
# environment SUPERADMIN. It is not a persistable admin_users role.
ChannelRole = Literal["USER", "ADMIN"]
ProvisioningOperation = Literal["CONNECT_ACCOUNT", "REAUTHORIZE"]
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
    operation: ProvisioningOperation = "CONNECT_ACCOUNT"
    target_account_id: uuid.UUID | None = None
    expected_config_version: int | None = None


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
        key: value for key, value in values.items() if key != "csrf_token" and value is not None
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
        if (
            optional_field in normalized_values
            and not str(normalized_values[optional_field]).strip()
        ):
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
    operation: str = "CONNECT_ACCOUNT",
    target_account_id: uuid.UUID | str | None = None,
    expected_config_version: int | None = None,
    target_brand_id: str | None = None,
    target_external_account_id: str | None = None,
) -> ProvisioningCommand:
    try:
        canonical_operation = canonical_provisioning_operation(operation)
    except ValueError as exc:
        raise ChannelValidationError(str(exc)) from exc
    try:
        target_uuid = (
            uuid.UUID(str(target_account_id)) if target_account_id is not None else None
        )
    except (TypeError, ValueError) as exc:
        raise ChannelValidationError("platform_account_target_invalid") from exc
    if canonical_operation == "REAUTHORIZE" and (
        target_uuid is None or expected_config_version is None
    ):
        raise ChannelValidationError("platform_account_target_required")
    if canonical_operation == "CONNECT_ACCOUNT" and (
        target_uuid is not None or expected_config_version is not None
    ):
        raise ChannelValidationError("connect_target_not_allowed")
    if expected_config_version is not None and expected_config_version < 1:
        raise ChannelValidationError("invalid_expected_config_version")
    if canonical_operation == "REAUTHORIZE" and not target_brand_id:
        raise ChannelValidationError("platform_account_target_brand_required")
    settings = get_settings()
    if not settings.platform_integration_enabled(platform):
        raise ChannelValidationError(f"{platform}_integration_disabled")
    normalized_values = _normalized_form_values(
        route_tenant_id=route_tenant_id,
        platform=platform,
        values=values,
    )
    if canonical_operation == "REAUTHORIZE":
        # The target row, rather than browser fields, supplies the account scope.
        normalized_values["brand_id"] = target_brand_id
        if target_external_account_id:
            if platform == "email":
                normalized_values["email_address"] = target_external_account_id
            elif platform in {"facebook", "instagram", "whatsapp"}:
                normalized_values["external_account_id"] = target_external_account_id
            elif platform == "feishu":
                normalized_values["app_id"] = target_external_account_id
        # Reauthorization preserves the persisted automation policy. The request
        # model only permits the safe new-account default for some platforms.
        normalized_values["automation_default"] = "BOT_DRAFT_ONLY"
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
    command_brand_id = str(plain_values.get("brand_id") or "default")
    if canonical_operation == "REAUTHORIZE":
        command_brand_id = str(target_brand_id)
        for field_name in ("brand_id", "name", "public_id"):
            public_values.pop(field_name, None)
        if platform == "telegram":
            # Omitted new-account options become False in the request model. They are
            # not reconnect scope changes; explicit True still fails the scope allowlist.
            for field_name in ("rotate_webhook_secret", "drop_pending_updates"):
                if public_values.get(field_name) is False:
                    public_values.pop(field_name)
    return ProvisioningCommand(
        tenant_id=route_tenant_id,
        brand_id=command_brand_id,
        platform=platform,
        actor=actor,
        public_values=public_values,
        secret_values=secret_values,
        operation=canonical_operation,
        target_account_id=target_uuid,
        expected_config_version=expected_config_version,
    )


async def _freeze_reauthorization_request_values(
    session,
    target: models.PlatformAccount,
    request_values: dict[str, Any],
) -> None:
    """Replace browser-supplied non-secret values with the target snapshot."""
    config = dict(target.config or {})
    request_values["automation_default"] = target.automation_default
    if target.platform in {"facebook", "instagram"}:
        request_values.update(
            {
                "external_account_id": target.external_account_id,
                "api_version": str(config.get("api_version") or "v23.0"),
                "instagram_login_mode": str(
                    config.get("instagram_login_mode") or "facebook_login"
                ),
                "page_id": config.get("page_id"),
                "enable_dm": bool((target.capability or {}).get("dm", True)),
                "enable_comments": bool((target.capability or {}).get("comments", False)),
            }
        )
        if target.platform_app_id is not None:
            app = await session.get(models.PlatformApp, target.platform_app_id)
            if app is not None:
                request_values.update(
                    {
                        "app_id": app.external_app_id,
                        "app_public_id": app.public_id,
                        "app_name": app.name,
                    }
                )
    elif target.platform == "whatsapp":
        request_values.update(
            {
                "external_account_id": target.external_account_id,
                "api_version": str(config.get("api_version") or "v23.0"),
            }
        )
        if target.platform_app_id is not None:
            app = await session.get(models.PlatformApp, target.platform_app_id)
            if app is not None:
                request_values.update(
                    {
                        "app_id": app.external_app_id,
                        "app_public_id": app.public_id,
                        "app_name": app.name,
                    }
                )
    elif target.platform == "feishu":
        request_values.update(
            {
                "app_id": target.external_account_id,
                "api_base_url": config.get(
                    "api_base_url", "https://open.feishu.cn/open-apis"
                ),
                "group_mode": config.get("feishu_group_mode", "mentions_only"),
            }
        )
    elif target.platform == "email":
        request_values.update(
            {
                "email_address": target.external_account_id,
                "imap_host": config.get("imap_host"),
                "imap_port": config.get("imap_port", 993),
                "mailbox": config.get("mailbox", "INBOX"),
                "smtp_host": config.get("smtp_host"),
                "smtp_port": config.get("smtp_port", 465),
                "smtp_security": config.get("smtp_security", "ssl"),
                "from_name": config.get("from_name"),
                "internal_domain_policy": config.get("internal_domain_policy", "ignore"),
            }
        )


def _reauthorization_public_scope_fields() -> frozenset[str]:
    return frozenset(
        {
            "idempotency_key",
            "tenant_id",
            "platform",
            "external_account_id",
            "email_address",
            "app_id",
            "app_public_id",
            "app_name",
            "api_version",
            "api_base_url",
            "group_mode",
            "imap_host",
            "imap_port",
            "mailbox",
            "smtp_host",
            "smtp_port",
            "smtp_security",
            "from_name",
            "internal_domain_policy",
            "instagram_login_mode",
            "page_id",
            "enable_dm",
            "enable_comments",
            "automation_default",
        }
    )



async def submit_channel_provisioning(command: ProvisioningCommand) -> uuid.UUID:
    try:
        operation = canonical_provisioning_operation(command.operation)
    except ValueError as exc:
        raise ChannelValidationError(str(exc)) from exc

    request_values = dict(command.public_values)
    job_brand_id = command.brand_id
    if operation == "REAUTHORIZE":
        if command.target_account_id is None or command.expected_config_version is None:
            raise ChannelValidationError("platform_account_target_required")
        allowed_scope_fields = _reauthorization_public_scope_fields()
        unexpected_scope_fields = sorted(set(request_values) - allowed_scope_fields)
        if unexpected_scope_fields:
            raise ChannelValidationError("reauthorization_scope_fields_forbidden")

    async with get_session_factory()() as session:
        principal = await _lock_current_channel_principal(
            session,
            command.actor,
            command.tenant_id,
            invalid_code=(
                "account_reauthorization_denied"
                if operation == "REAUTHORIZE"
                else "provisioning_session_invalid"
            ),
        )
        if operation == "CONNECT_ACCOUNT":
            try:
                validate_owner_provisioning_policy(
                    platform=command.platform,
                    owner_user_id=(
                        None if principal.is_workspace_admin else principal.user_id
                    ),
                    request=request_values,
                    is_workspace_admin=principal.is_workspace_admin,
                )
            except ValueError as exc:
                raise ChannelPermissionError("tenant_admin_required") from exc
        else:
            target = await session.scalar(
                select(models.PlatformAccount)
                .where(
                    models.PlatformAccount.id == command.target_account_id,
                    models.PlatformAccount.tenant_id == command.tenant_id,
                    models.PlatformAccount.platform == command.platform,
                )
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            if target is None:
                raise ChannelNotFoundError("platform_account_target_not_found")
            if not target.external_account_id:
                raise ChannelValidationError("target_account_identity_missing")
            try:
                await require_reauthorization(
                    session,
                    principal=principal,
                    account=target,
                    expected_config_version=command.expected_config_version,
                )
            except PermissionError as exc:
                raise ChannelPermissionError(str(exc)) from exc
            except ValueError as exc:
                raise ChannelConflictError(str(exc)) from exc
            job_brand_id = target.brand_id
            await _freeze_reauthorization_request_values(session, target, request_values)

    try:
        job_id = await submit_provisioning_job(
            tenant_id=command.tenant_id,
            brand_id=job_brand_id,
            platform=command.platform,
            actor=command.actor.actor,
            request=request_values,
            secrets=command.secret_values,
            operation=operation,
            target_account_id=command.target_account_id,
            expected_config_version=command.expected_config_version,
            admin_session_id=command.actor.session_id,
        )
    except PermissionError as exc:
        safe_codes = {
            "account_reauthorization_denied",
            "reauthorization_requires_session",
            "admin_session_invalid",
            "initiator_session_invalid",
            "provisioning_authority_invalid",
        }
        code = str(exc)
        raise ChannelPermissionError(
            code if code in safe_codes else "provisioning_authority_invalid"
        ) from exc
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

def _channel_principal_matches_actor(
    principal: Any,
    actor: ChannelActor,
    tenant_id: str,
) -> bool:
    return bool(
        principal is not None
        and principal.session_id == actor.session_id
        and principal.actor == actor.actor
        and principal.user_id == actor.user_id
        and principal.is_workspace_admin == actor.is_admin
        and not principal.must_change_password
        and tenant_id in principal.allowed_tenants
    )


async def _lock_current_channel_principal(
    session,
    actor: ChannelActor,
    tenant_id: str,
    *,
    additional_user_ids: tuple[uuid.UUID, ...] = (),
    invalid_code: str = "provisioning_session_invalid",
    return_staff_rows: bool = False,
):
    initial_principal = await principal_from_session_row(session, actor.session_id)
    if not _channel_principal_matches_actor(initial_principal, actor, tenant_id):
        raise ChannelPermissionError(invalid_code)
    staff_ids = tuple(
        sorted(
            {
                user_id
                for user_id in (
                    actor.user_id,
                    initial_principal.user_id,
                    *additional_user_ids,
                )
                if user_id is not None
            },
            key=str,
        )
    )
    for user_id in staff_ids:
        await lock_user_authority(session, user_id)
    await _lock_account_access_sessions(session, (actor.session_id,))
    staff_rows = await _lock_account_access_staff_rows(session, staff_ids)
    principal = await principal_from_session_row(session, actor.session_id)
    if not _channel_principal_matches_actor(principal, actor, tenant_id):
        raise ChannelPermissionError(invalid_code)
    if return_staff_rows:
        return principal, staff_rows
    return principal


async def _require_current_channel_admin(
    session,
    actor: ChannelActor,
    tenant_id: str,
    *,
    additional_user_ids: tuple[uuid.UUID, ...] = (),
):
    principal, staff_rows = await _lock_current_channel_principal(
        session,
        actor,
        tenant_id,
        additional_user_ids=additional_user_ids,
        invalid_code="tenant_admin_required",
        return_staff_rows=True,
    )
    if not principal.is_workspace_admin:
        raise ChannelPermissionError("tenant_admin_required")
    return principal, staff_rows


async def _locked_account(
    session,
    *,
    tenant_id: str,
    account_id: uuid.UUID,
    actor: ChannelActor,
    additional_user_ids: tuple[uuid.UUID, ...] = (),
    require_admin: bool = True,
) -> models.PlatformAccount:
    principal, staff_rows = await (
        _require_current_channel_admin(
            session,
            actor,
            tenant_id,
            additional_user_ids=additional_user_ids,
        )
        if require_admin
        else _lock_current_channel_principal(
            session,
            actor,
            tenant_id,
            additional_user_ids=additional_user_ids,
            return_staff_rows=True,
        )
    )
    account = await session.scalar(
        select(models.PlatformAccount)
        .where(
            models.PlatformAccount.id == account_id,
            models.PlatformAccount.tenant_id == tenant_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if account is None:
        raise ChannelNotFoundError("platform_account_not_found")
    if principal.user_id is None:
        if not principal.is_superadmin:
            raise ChannelNotFoundError("platform_account_not_found")
    else:
        user = staff_rows.get(principal.user_id)
        if user is None or not user_can_access_account(user, account):
            raise ChannelNotFoundError("platform_account_not_found")
    return account


async def _lock_channel_reauthorization_grant_context(
    session,
    *,
    tenant_id: str,
    account_id: uuid.UUID,
    actor: ChannelActor,
    target_user_id: uuid.UUID,
):
    initial_principal = await principal_from_session_row(session, actor.session_id)
    if not _channel_admin_principal_matches_actor(initial_principal, actor, tenant_id):
        raise ChannelPermissionError("tenant_admin_required")
    staff_ids = tuple(
        sorted(
            {
                user_id
                for user_id in (
                    actor.user_id,
                    initial_principal.user_id,
                    target_user_id,
                )
                if user_id is not None
            },
            key=str,
        )
    )
    for staff_id in staff_ids:
        await lock_user_authority(session, staff_id)
    await _lock_account_access_sessions(session, (actor.session_id,))
    staff_rows = await _lock_account_access_staff_rows(session, staff_ids)
    principal = await principal_from_session_row(session, actor.session_id)
    if not _channel_admin_principal_matches_actor(principal, actor, tenant_id):
        raise ChannelPermissionError("tenant_admin_required")

    target = staff_rows.get(target_user_id)
    if (
        target is None
        or target.tenant_id != tenant_id
        or target.role != "USER"
        or target.status != "active"
    ):
        raise ChannelValidationError("platform_account_reauthorization_grantee_invalid")

    account = await session.scalar(
        select(models.PlatformAccount)
        .where(
            models.PlatformAccount.id == account_id,
            models.PlatformAccount.tenant_id == tenant_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
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


@dataclass(frozen=True)
class _AccountWorkIdentity:
    work_id: uuid.UUID
    conversation_id: uuid.UUID
    status: str
    version: int
    assigned_user_id: uuid.UUID | None
    assigned_actor: str | None
    assigned_session_id: uuid.UUID | None


@dataclass(frozen=True)
class _AccountOutboxIdentity:
    outbox_id: uuid.UUID
    conversation_id: uuid.UUID
    status: str
    initiator_user_id: uuid.UUID | None
    initiator_session_id: uuid.UUID | None
    human_work_item_version: int | None


@dataclass(frozen=True)
class _AccountAccessSnapshot:
    tenant_id: str
    account_id: uuid.UUID
    owner_user_id: uuid.UUID | None
    shared_with_support: bool
    status: str
    config_version: int
    work_items: tuple[_AccountWorkIdentity, ...]
    outboxes: tuple[_AccountOutboxIdentity, ...]
    conversation_ids: tuple[uuid.UUID, ...]
    staff_ids: tuple[uuid.UUID, ...]
    session_ids: tuple[uuid.UUID, ...]


@dataclass(frozen=True)
class _LockedAccountAccessContext:
    snapshot: _AccountAccessSnapshot
    account: models.PlatformAccount
    conversations: dict[uuid.UUID, models.Conversation]
    staff_rows: dict[uuid.UUID, models.AdminUser]
    work_items: dict[uuid.UUID, models.HumanWorkItem]
    states: dict[uuid.UUID, models.AutomationState]
    outboxes: dict[uuid.UUID, models.OutboxMessage]
    notification_intents: dict[uuid.UUID, models.HandoffNotificationIntent]
    session_principals: dict[uuid.UUID, Any]


_ACCOUNT_ACCESS_OUTBOX_STATUSES = (
    "PENDING",
    "FAILED",
    "NEEDS_REVIEW",
    "SENDING",
)
_ACCOUNT_ACCESS_CANCELABLE_OUTBOX_STATUSES = frozenset(
    {"PENDING", "FAILED", "NEEDS_REVIEW"}
)
_ACCOUNT_ACCESS_SNAPSHOT_ATTEMPTS = 3


def _account_work_identity(work: models.HumanWorkItem) -> _AccountWorkIdentity:
    return _AccountWorkIdentity(
        work_id=work.id,
        conversation_id=work.conversation_id,
        status=work.status,
        version=work.version,
        assigned_user_id=work.assigned_user_id,
        assigned_actor=work.assigned_actor,
        assigned_session_id=work.assigned_session_id,
    )


def _account_outbox_identity(outbox: models.OutboxMessage) -> _AccountOutboxIdentity:
    return _AccountOutboxIdentity(
        outbox_id=outbox.id,
        conversation_id=outbox.conversation_id,
        status=outbox.status,
        initiator_user_id=outbox.initiator_user_id,
        initiator_session_id=outbox.initiator_session_id,
        human_work_item_version=outbox.human_work_item_version,
    )


async def _read_account_access_snapshot(
    session,
    *,
    tenant_id: str,
    account_id: uuid.UUID,
    actor: ChannelActor,
    target_owner_user_id: uuid.UUID | None = None,
) -> _AccountAccessSnapshot:
    account = await session.scalar(
        select(models.PlatformAccount)
        .where(
            models.PlatformAccount.id == account_id,
            models.PlatformAccount.tenant_id == tenant_id,
        )
        .execution_options(populate_existing=True)
    )
    if account is None:
        raise ChannelNotFoundError("platform_account_not_found")

    work_rows = list(
        (
            await session.scalars(
                select(models.HumanWorkItem)
                .join(
                    models.Conversation,
                    models.Conversation.id == models.HumanWorkItem.conversation_id,
                )
                .where(
                    models.HumanWorkItem.tenant_id == tenant_id,
                    models.HumanWorkItem.status == "CLAIMED",
                    models.Conversation.tenant_id == tenant_id,
                    models.Conversation.platform_account_id == account_id,
                )
                .execution_options(populate_existing=True)
                .order_by(models.HumanWorkItem.id)
            )
        ).all()
    )
    outbox_rows = list(
        (
            await session.scalars(
                select(models.OutboxMessage)
                .join(
                    models.Conversation,
                    models.Conversation.id == models.OutboxMessage.conversation_id,
                )
                .where(
                    models.OutboxMessage.tenant_id == tenant_id,
                    models.OutboxMessage.platform_account_id == account_id,
                    models.OutboxMessage.origin_kind == OutboxOrigin.MANUAL_REPLY,
                    models.OutboxMessage.actor_kind == OutboxActor.ADMIN_HUMAN,
                    models.OutboxMessage.status.in_(_ACCOUNT_ACCESS_OUTBOX_STATUSES),
                    models.Conversation.tenant_id == tenant_id,
                    models.Conversation.platform_account_id == account_id,
                )
                .execution_options(populate_existing=True)
                .order_by(models.OutboxMessage.id)
            )
        ).all()
    )
    work_identities = tuple(_account_work_identity(work) for work in work_rows)
    outbox_identities = tuple(_account_outbox_identity(outbox) for outbox in outbox_rows)
    conversation_ids = tuple(
        sorted(
            {
                *(work.conversation_id for work in work_rows),
                *(outbox.conversation_id for outbox in outbox_rows),
            },
            key=str,
        )
    )
    staff_ids = {
        user_id
        for user_id in (
            actor.user_id,
            account.owner_user_id,
            target_owner_user_id,
            *(work.assigned_user_id for work in work_rows),
            *(outbox.initiator_user_id for outbox in outbox_rows),
        )
        if user_id is not None
    }
    session_ids = {
        session_id
        for session_id in (
            actor.session_id,
            *(work.assigned_session_id for work in work_rows),
            *(outbox.initiator_session_id for outbox in outbox_rows),
        )
        if session_id is not None
    }
    return _AccountAccessSnapshot(
        tenant_id=tenant_id,
        account_id=account_id,
        owner_user_id=account.owner_user_id,
        shared_with_support=bool(account.shared_with_support),
        status=account.status,
        config_version=account.config_version,
        work_items=work_identities,
        outboxes=outbox_identities,
        conversation_ids=conversation_ids,
        staff_ids=tuple(sorted(staff_ids, key=str)),
        session_ids=tuple(sorted(session_ids, key=str)),
    )


async def _lock_account_access_staff(session, staff_ids: tuple[uuid.UUID, ...]) -> None:
    for staff_id in staff_ids:
        await lock_user_authority(session, staff_id)


async def _lock_account_access_staff_rows(
    session,
    staff_ids: tuple[uuid.UUID, ...],
) -> dict[uuid.UUID, models.AdminUser]:
    if not staff_ids:
        return {}
    return {
        user.id: user
        for user in (
            await session.scalars(
                select(models.AdminUser)
                .where(models.AdminUser.id.in_(staff_ids))
                .execution_options(populate_existing=True)
                .order_by(models.AdminUser.id)
                .with_for_update()
            )
        ).all()
    }

async def _lock_account_access_sessions(session, session_ids: tuple[uuid.UUID, ...]) -> None:
    if not session_ids:
        return
    await lock_session_authorities(session, session_ids)
    await session.execute(
        select(models.AdminSession)
        .where(models.AdminSession.id.in_(session_ids))
        .execution_options(populate_existing=True)
        .order_by(models.AdminSession.id)
        .with_for_update()
    )


async def _lock_account_access_delivery(
    session,
    conversation_ids: tuple[uuid.UUID, ...],
) -> None:
    for conversation_id in conversation_ids:
        await acquire_conversation_delivery_xact_lock(session, conversation_id)


def _channel_admin_principal_matches_actor(
    principal: Any,
    actor: ChannelActor,
    tenant_id: str,
) -> bool:
    return bool(
        _channel_principal_matches_actor(principal, actor, tenant_id)
        and principal.is_workspace_admin
    )


async def _lock_account_access_context(
    session,
    *,
    tenant_id: str,
    account_id: uuid.UUID,
    actor: ChannelActor,
    expected_config_version: int,
    target_owner_user_id: uuid.UUID | None = None,
) -> _LockedAccountAccessContext:
    initial_principal = await principal_from_session_row(session, actor.session_id)
    if not _channel_admin_principal_matches_actor(initial_principal, actor, tenant_id):
        raise ChannelPermissionError("tenant_admin_required")

    snapshot = await _read_account_access_snapshot(
        session,
        tenant_id=tenant_id,
        account_id=account_id,
        actor=actor,
        target_owner_user_id=target_owner_user_id,
    )
    await _lock_account_access_staff(session, snapshot.staff_ids)
    await _lock_account_access_sessions(session, snapshot.session_ids)
    staff_rows = await _lock_account_access_staff_rows(session, snapshot.staff_ids)
    await _lock_account_access_delivery(session, snapshot.conversation_ids)

    locked_snapshot = await _read_account_access_snapshot(
        session,
        tenant_id=tenant_id,
        account_id=account_id,
        actor=actor,
        target_owner_user_id=target_owner_user_id,
    )
    if locked_snapshot != snapshot:
        raise ChannelConflictError("platform_account_access_snapshot_conflict")

    conversations = {
        conversation.id: conversation
        for conversation in (
            await session.scalars(
                select(models.Conversation)
                .where(
                    models.Conversation.id.in_(snapshot.conversation_ids),
                    models.Conversation.tenant_id == tenant_id,
                )
                .execution_options(populate_existing=True)
                .order_by(models.Conversation.id)
                .with_for_update()
            )
        ).all()
    } if snapshot.conversation_ids else {}
    if len(conversations) != len(snapshot.conversation_ids):
        raise ChannelConflictError("platform_account_access_snapshot_conflict")

    account = await session.scalar(
        select(models.PlatformAccount)
        .where(
            models.PlatformAccount.id == account_id,
            models.PlatformAccount.tenant_id == tenant_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if account is None:
        raise ChannelNotFoundError("platform_account_not_found")

    work_items = {
        work.id: work
        for work in (
            await session.scalars(
                select(models.HumanWorkItem)
                .where(
                    models.HumanWorkItem.id.in_(
                        tuple(item.work_id for item in snapshot.work_items)
                    ),
                    models.HumanWorkItem.tenant_id == tenant_id,
                )
                .execution_options(populate_existing=True)
                .order_by(models.HumanWorkItem.id)
                .with_for_update()
            )
        ).all()
    } if snapshot.work_items else {}
    if len(work_items) != len(snapshot.work_items):
        raise ChannelConflictError("platform_account_access_snapshot_conflict")

    states = {
        state.conversation_id: state
        for state in (
            await session.scalars(
                select(models.AutomationState)
                .where(
                    models.AutomationState.conversation_id.in_(snapshot.conversation_ids)
                )
                .execution_options(populate_existing=True)
                .order_by(models.AutomationState.conversation_id)
                .with_for_update()
            )
        ).all()
    } if snapshot.conversation_ids else {}
    outboxes = {
        outbox.id: outbox
        for outbox in (
            await session.scalars(
                select(models.OutboxMessage)
                .where(
                    models.OutboxMessage.id.in_(
                        tuple(item.outbox_id for item in snapshot.outboxes)
                    ),
                    models.OutboxMessage.tenant_id == tenant_id,
                )
                .execution_options(populate_existing=True)
                .order_by(models.OutboxMessage.id)
                .with_for_update()
            )
        ).all()
    } if snapshot.outboxes else {}
    if len(outboxes) != len(snapshot.outboxes):
        raise ChannelConflictError("platform_account_access_snapshot_conflict")

    notification_intents = {
        intent.human_work_item_id: intent
        for intent in (
            await session.scalars(
                select(models.HandoffNotificationIntent)
                .where(
                    models.HandoffNotificationIntent.human_work_item_id.in_(
                        tuple(item.work_id for item in snapshot.work_items)
                    ),
                    models.HandoffNotificationIntent.tenant_id == tenant_id,
                )
                .execution_options(populate_existing=True)
                .order_by(models.HandoffNotificationIntent.id)
                .with_for_update()
            )
        ).all()
    } if snapshot.work_items else {}
    if any(
        intent.conversation_id not in conversations
        for intent in notification_intents.values()
    ):
        raise ChannelConflictError("platform_account_access_snapshot_conflict")

    final_snapshot = await _read_account_access_snapshot(
        session,
        tenant_id=tenant_id,
        account_id=account_id,
        actor=actor,
        target_owner_user_id=target_owner_user_id,
    )
    if final_snapshot != snapshot:
        raise ChannelConflictError("platform_account_access_snapshot_conflict")
    _require_config_version(account, expected_config_version)

    current_principal = await principal_from_session_row(
        session,
        actor.session_id,
    )
    if not _channel_admin_principal_matches_actor(current_principal, actor, tenant_id):
        raise ChannelPermissionError("tenant_admin_required")
    session_principals = {actor.session_id: current_principal}
    for session_id in snapshot.session_ids:
        if session_id == actor.session_id:
            continue
        session_principals[session_id] = await principal_from_session_row(
            session,
            session_id,
        )
    return _LockedAccountAccessContext(
        snapshot=snapshot,
        account=account,
        conversations=conversations,
        staff_rows=staff_rows,
        work_items=work_items,
        states=states,
        outboxes=outboxes,
        notification_intents=notification_intents,
        session_principals=session_principals,
    )


def _account_work_assignment_is_eligible(
    work: models.HumanWorkItem,
    *,
    account: models.PlatformAccount,
    staff_rows: dict[uuid.UUID, models.AdminUser],
    session_principals: dict[uuid.UUID, Any],
) -> bool:
    if work.assigned_user_id is None:
        if work.assigned_session_id is None:
            return False
        principal = session_principals.get(work.assigned_session_id)
        return bool(
            principal is not None
            and principal.is_superadmin
            and principal.session_id == work.assigned_session_id
            and principal.user_id is None
            and account.tenant_id in principal.allowed_tenants
        )

    staff_user = staff_rows.get(work.assigned_user_id)
    if staff_user is None or not user_can_access_account(staff_user, account):
        return False
    return True


def _account_outbox_belongs_to_work(
    outbox: models.OutboxMessage,
    work: _AccountWorkIdentity,
) -> bool:
    return (
        outbox.conversation_id == work.conversation_id
        and outbox.initiator_user_id == work.assigned_user_id
        and (
            work.assigned_user_id is not None
            or (
                work.assigned_session_id is not None
                and outbox.initiator_session_id == work.assigned_session_id
            )
        )
        and outbox.human_work_item_version == work.version
    )


async def _release_ineligible_account_work(
    session,
    *,
    context: _LockedAccountAccessContext,
    actor: ChannelActor,
    reason: str,
) -> int:
    """Release only assignments that lost current account access in this transaction."""
    released = 0
    work_snapshots = {item.work_id: item for item in context.snapshot.work_items}
    for work_id in sorted(context.work_items, key=str):
        work = context.work_items[work_id]
        if work.status != "CLAIMED":
            continue
        if _account_work_assignment_is_eligible(
            work,
            account=context.account,
            staff_rows=context.staff_rows,
            session_principals=context.session_principals,
        ):
            continue
        work_snapshot = work_snapshots[work_id]
        state = context.states.get(work.conversation_id)
        if state is None:
            raise ChannelValidationError("automation_state_not_found")

        work.status = "WAITING"
        work.assigned_user_id = None
        work.assigned_actor = None
        work.assigned_session_id = None
        work.claimed_at = None
        work.version += 1
        if state.state in {"HUMAN_ACTIVE", "HANDOFF_PENDING"}:
            state.state = "HANDOFF_PENDING"
            state.state_version += 1
            state.human_agent_id = None
            state.state_changed_reason = reason

        for outbox in context.outboxes.values():
            if (
                outbox.status in _ACCOUNT_ACCESS_CANCELABLE_OUTBOX_STATUSES
                and outbox.origin_kind == OutboxOrigin.MANUAL_REPLY
                and outbox.actor_kind == OutboxActor.ADMIN_HUMAN
                and _account_outbox_belongs_to_work(outbox, work_snapshot)
            ):
                outbox.status = "CANCELLED"
                outbox.last_error_code = "ACCOUNT_ACCESS_REVOKED"
                outbox.last_error_message = reason[:500]
                outbox.next_attempt_at = None

        # The handoff service deliberately keeps SENDING status, claim lease, sending
        # revision, and provider evidence intact while advancing desired card state.
        intent = context.notification_intents.get(work.id)
        sending_next_attempt_at = (
            intent.next_attempt_at if intent is not None and intent.status == "SENDING" else None
        )
        await advance_handoff_notification_for_work(session, work=work)
        if intent is not None and intent.status == "SENDING":
            intent.next_attempt_at = sending_next_attempt_at
        session.add(
            models.AuditLog(
                tenant_id=context.snapshot.tenant_id,
                category="human_work",
                actor=actor.actor,
                action="RELEASE_ACCOUNT_ACCESS",
                subject_type="human_work_item",
                subject_id=str(work.id),
                detail={
                    "account_id": str(context.snapshot.account_id),
                    "reason": reason,
                    "version": work.version,
                },
            )
        )
        released += 1
    return released


async def _apply_channel_account_access_change(
    *,
    tenant_id: str,
    account_id: uuid.UUID,
    actor: ChannelActor,
    expected_config_version: int,
    change: Literal["owner", "support"],
    owner_user_id: uuid.UUID | None = None,
    shared: bool | None = None,
    reason: str,
) -> None:
    for attempt in range(_ACCOUNT_ACCESS_SNAPSHOT_ATTEMPTS):
        try:
            async with get_session_factory()() as session:
                context = await _lock_account_access_context(
                    session,
                    tenant_id=tenant_id,
                    account_id=account_id,
                    actor=actor,
                    expected_config_version=expected_config_version,
                    target_owner_user_id=owner_user_id if change == "owner" else None,
                )
                account = context.account
                previous_owner_user_id = account.owner_user_id
                previous_shared_with_support = bool(account.shared_with_support)
                if change == "owner":
                    changed = previous_owner_user_id != owner_user_id
                    if changed and owner_user_id is not None:
                        owner = context.staff_rows.get(owner_user_id)
                        if (
                            owner is None
                            or owner.tenant_id != tenant_id
                            or owner.status != "active"
                            or owner.role not in {"USER", "WORKSPACE_ADMIN"}
                        ):
                            raise ChannelValidationError(
                                "platform_account_owner_not_assignable"
                            )
                    if changed:
                        account.owner_user_id = owner_user_id
                        account.config_version += 1
                elif change == "support":
                    if shared is None:
                        raise ChannelValidationError("support_visibility_value_required")
                    changed = previous_shared_with_support != shared
                    if changed:
                        account.shared_with_support = shared
                        account.config_version += 1
                else:
                    raise ChannelValidationError("unsupported_account_access_change")

                released = await _release_ineligible_account_work(
                    session,
                    context=context,
                    actor=actor,
                    reason=reason,
                )
                detail = {
                    "changed": changed,
                    "config_version": account.config_version,
                    "released_work_items": released,
                }
                if change == "owner":
                    detail.update(
                        {
                            "previous_owner_user_id": (
                                str(previous_owner_user_id)
                                if previous_owner_user_id
                                else None
                            ),
                            "owner_user_id": str(owner_user_id) if owner_user_id else None,
                        }
                    )
                    action = "ASSIGN_PLATFORM_ACCOUNT_OWNER"
                else:
                    detail.update(
                        {
                            "previous_shared_with_support": previous_shared_with_support,
                            "shared_with_support": shared,
                        }
                    )
                    action = "SET_PLATFORM_ACCOUNT_SUPPORT_VISIBILITY"
                _add_account_audit(
                    session,
                    tenant_id=tenant_id,
                    actor=actor,
                    action=action,
                    account_id=account_id,
                    detail=detail,
                )
                await session.commit()
                return
        except ChannelConflictError as exc:
            if (
                exc.code != "platform_account_access_snapshot_conflict"
                or attempt == _ACCOUNT_ACCESS_SNAPSHOT_ATTEMPTS - 1
            ):
                raise


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
            require_admin=True,
        )
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
    await _apply_channel_account_access_change(
        tenant_id=tenant_id,
        account_id=account_id,
        actor=actor,
        expected_config_version=expected_config_version,
        change="owner",
        owner_user_id=owner_user_id,
        reason="account_owner_changed",
    )

async def set_channel_account_support_visibility(
    *,
    tenant_id: str,
    account_id: uuid.UUID,
    actor: ChannelActor,
    shared: bool,
    expected_config_version: int,
) -> None:
    await _apply_channel_account_access_change(
        tenant_id=tenant_id,
        account_id=account_id,
        actor=actor,
        expected_config_version=expected_config_version,
        change="support",
        shared=shared,
        reason="account_support_visibility_revoked",
    )

async def set_channel_reauthorization_grant(
    *,
    tenant_id: str,
    account_id: uuid.UUID,
    actor: ChannelActor,
    user_id: uuid.UUID,
    enabled: bool,
    expected_config_version: int,
) -> None:
    async with get_session_factory()() as session:
        account = await _lock_channel_reauthorization_grant_context(
            session,
            tenant_id=tenant_id,
            account_id=account_id,
            actor=actor,
            target_user_id=user_id,
        )
        _require_config_version(account, expected_config_version)
        grant = await session.scalar(
            select(models.AccountReauthorizationGrant)
            .where(
                models.AccountReauthorizationGrant.tenant_id == tenant_id,
                models.AccountReauthorizationGrant.platform_account_id == account_id,
                models.AccountReauthorizationGrant.user_id == user_id,
            )
            .with_for_update()
        )
        previous_enabled = bool(grant and grant.active)
        changed = previous_enabled != enabled
        if changed:
            if grant is None:
                grant = models.AccountReauthorizationGrant(
                    tenant_id=tenant_id,
                    platform_account_id=account_id,
                    user_id=user_id,
                    active=enabled,
                )
                session.add(grant)
            else:
                grant.active = enabled
            account.config_version += 1
        _add_account_audit(
            session,
            tenant_id=tenant_id,
            actor=actor,
            action="SET_PLATFORM_ACCOUNT_REAUTHORIZATION_GRANT",
            account_id=account_id,
            detail={
                "user_id": str(user_id),
                "previous_enabled": previous_enabled,
                "enabled": enabled,
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
                actor_user_id=actor.user_id,
                actor_session_id=actor.session_id,
            ),
            audit_id=operation_id,
        )
        await session.commit()

    outcome = await reconcile_account_kill_switch_command(
        operation_id,
        raise_on_redis_error=True,
    )
    if outcome not in {"APPLIED", "UNCHANGED"}:
        raise ChannelConflictError("kill_switch_command_requires_reconfirmation")


async def retry_channel_job(
    *,
    tenant_id: str,
    job_id: uuid.UUID,
    principal: Principal,
) -> None:
    try:
        await retry_provisioning_job(
            job_id,
            tenant_id=tenant_id,
            caller=principal,
        )
    except LookupError as exc:
        raise ChannelNotFoundError("provisioning_job_not_found") from exc
    except PermissionError as exc:
        safe_codes = {
            "account_reauthorization_denied",
            "reauthorization_requires_session",
            "admin_session_invalid",
            "initiator_session_invalid",
            "provisioning_authority_invalid",
            "retry_caller_required",
            "retry_caller_mismatch",
        }
        code = str(exc)
        raise ChannelPermissionError(
            code if code in safe_codes else "provisioning_job_authority_invalid"
        ) from exc
    except ValueError as exc:
        code = str(exc)
        if code == "user_provisioning_policy_rejected":
            raise ChannelPermissionError("tenant_admin_required") from exc
        if code.endswith("_integration_disabled"):
            raise ChannelValidationError(code) from exc
        raise ChannelConflictError(code) from exc


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
    expected_config_version: int | None = None,
) -> None:
    if not get_settings().xchat_enabled:
        raise ChannelValidationError("xchat_disabled")
    if not _XCHAT_PIN_PATTERN.fullmatch(pin):
        raise ChannelValidationError("invalid_xchat_pin")
    async with get_session_factory()() as session:
        principal = await principal_from_session_row(session, actor.session_id)
        if not _channel_principal_matches_actor(principal, actor, tenant_id):
            raise ChannelPermissionError("xchat_repair_authority_revoked")
    try:
        await enable_xchat_for_account(
            account_id=account_id, tenant_id=tenant_id, pin=pin, principal=principal,
            expected_config_version=expected_config_version,
        )
    except PermissionError as exc:
        raise ChannelPermissionError(str(exc)) from exc
    except XChatActivationError:
        raise
    except ValueError as exc:
        if str(exc) in {"account_reauthorization_version_conflict", "xchat_repair_account_changed"}:
            raise ChannelConflictError(str(exc)) from exc
        raise ChannelValidationError(str(exc)) from exc
