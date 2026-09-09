import imaplib
import logging
import ssl
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from social_reply.application.account_management.access import (
    lock_session_authorities,
    lock_user_authority,
    require_reauthorization,
)
from social_reply.application.account_management.audit import record_account_management_audit
from social_reply.application.account_management.auth import Principal, principal_from_session_row
from social_reply.application.account_management.provisioning import (
    PRIVATE_INPUT_FINGERPRINT_KEY,
    PRIVATE_INPUT_FINGERPRINT_WITHOUT_REQUIRED_KEY,
    checkpoint_matches_job,
    checkpoint_output_version,
    credential_fingerprint_matches,
    provisioning_checkpoint,
    provisioning_input_fingerprint_matches,
    provisioning_input_fingerprint_record,
    provisioning_input_fingerprint_without_required_matches,
)
from social_reply.application.account_management.service import (
    AccountConnectionResult,
    connect_meta_account,
    connect_telegram_account,
    connect_x_account,
    resume_checkpointed_provisioning,
)
from social_reply.application.account_management.submissions import split_submission
from social_reply.application.account_management.xchat_activation import XChatActivationError
from social_reply.connectors.email.contracts import (
    MAX_EMAIL_CREDENTIAL_CHARS,
    validate_email_account_text,
)
from social_reply.connectors.email.imap_client import ImapClientError
from social_reply.connectors.email.network import EmailNetworkError
from social_reply.connectors.errors import PermanentSendError, RetryableSendError
from social_reply.connectors.feishu.client import FeishuClientError
from social_reply.connectors.feishu.contracts import FEISHU_API_BASE_URL, FEISHU_GROUP_MODE
from social_reply.connectors.meta.client import MetaCommentPermissionError
from social_reply.domain.platform_accounts import PROVISIONABLE_ACCOUNT_PLATFORMS
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.queue.dispatch import dispatch_actor
from social_reply.infrastructure.secret_crypto import decrypt_secret_bundle, encrypt_secret_bundle
from social_reply.shared.config import get_settings

logger = logging.getLogger(__name__)
_MAX_BACKOFF_SECONDS = 300
_MAX_ATTEMPTS = 8
_STALE_AFTER = timedelta(minutes=5)
_RETRY_DISPLAY_GRACE = timedelta(minutes=2)
_PLATFORM_DISABLED_STATUS = "PAUSED_PLATFORM_DISABLED"
_USER_SELF_SERVICE_PLATFORMS = frozenset({"x", "facebook", "instagram", "telegram", "email"})
_CONTROL_API_TRUST_TOKEN = object()
_CONTROL_API_AUTHORITY_TOKEN = object()
_CONTROL_API_RETRY_TOKEN = object()
_CONTROL_API_SECRET_KEY = "__control_api_trust__"
_CONTROL_API_SECRET_VALUE = "v1"
_AUTHORITY_VERSION = 1
_AUTHORITY_KINDS = frozenset({"STAFF_SESSION", "BOOTSTRAP_SESSION", "CONTROL_API"})
_XCHAT_CHECKPOINT_PHASES = frozenset({"CREDENTIALS_APPLIED", "SUBSCRIPTIONS_APPLIED"})


def _staging_secrets(secrets: dict[str, str], *, control_trusted: bool) -> dict[str, str]:
    staged = dict(secrets)
    if control_trusted:
        staged[_CONTROL_API_SECRET_KEY] = _CONTROL_API_SECRET_VALUE
    return staged


def _has_control_trust(staging_secret: object) -> bool:
    if not staging_secret:
        return False
    try:
        values = decrypt_secret_bundle(staging_secret)
    except (TypeError, ValueError):
        return False
    return values.get(_CONTROL_API_SECRET_KEY) == _CONTROL_API_SECRET_VALUE


def _disabled_platform(exc: Exception) -> str | None:
    if not isinstance(exc, ValueError):
        return None
    message = str(exc)
    for platform in PROVISIONABLE_ACCOUNT_PLATFORMS:
        if message == f"{platform}_integration_disabled":
            return platform
    return None


def _request_bool(request: dict[str, Any], key: str, *, default: bool) -> bool:
    value = request.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise ValueError(f"invalid_boolean:{key}")


def _idempotency_key(
    tenant_id: str,
    platform: str,
    request: dict[str, Any],
    *,
    operation: str = "CONNECT_ACCOUNT",
    target_account_id: uuid.UUID | None = None,
    expected_config_version: int | None = None,
) -> str:
    """Keep the caller's marker stable across retries and version changes."""
    del tenant_id, platform, operation, target_account_id, expected_config_version
    explicit = request.get("idempotency_key")
    return str(explicit) if explicit else uuid.uuid4().hex


def _safe_request(platform: str, request: dict[str, Any]) -> dict[str, Any]:
    public, _secrets = split_submission(platform, request)
    return public


def _input_fingerprint_record(
    *,
    tenant_id: str,
    brand_id: str,
    platform: str,
    operation: str,
    target_account_id: uuid.UUID | str | None,
    expected_config_version: int | None,
    request: dict[str, Any],
    secrets: dict[str, str],
) -> dict[str, Any]:
    return provisioning_input_fingerprint_record(
        tenant_id=tenant_id,
        brand_id=brand_id,
        platform=platform,
        operation=operation,
        target_account_id=target_account_id,
        expected_config_version=expected_config_version,
        request=request,
        credential_bundle=secrets,
    )


def _input_fingerprint_matches_job(
    job: models.ProvisioningJob,
    *,
    request: dict[str, Any],
    secrets: dict[str, str],
    expected: object,
    omit_secret: str | None = None,
) -> bool:
    return provisioning_input_fingerprint_matches(
        expected=expected,
        tenant_id=job.tenant_id,
        brand_id=job.brand_id,
        platform=job.platform,
        operation=job.operation,
        target_account_id=job.target_account_id,
        expected_config_version=job.expected_config_version,
        request=request,
        credential_bundle=secrets,
        omit_secret=omit_secret,
    )


def _xchat_resubmission_marked(job: models.ProvisioningJob) -> bool:
    result = dict(job.result or {})
    required_secret = str(result.get("required_secret") or "")
    if required_secret == "xchat_pin":
        return True
    if required_secret or not result.get("requires_secret_resubmission"):
        return False
    return not any(
        result.get(key)
        for key in ("manual_followup", "needed_action", "neededAction", "operator_action")
    )


def _checkpoint_versions_match_job(
    job: models.ProvisioningJob,
    checkpoint: dict[str, Any],
    output_version: int,
) -> bool:
    phase = checkpoint.get("phase")
    input_version = checkpoint.get("input_config_version")
    if job.operation == "REAUTHORIZE":
        if phase != "CREDENTIALS_APPLIED":
            return False
        try:
            normalized_input = int(input_version)
            expected_version = int(job.expected_config_version)
        except (TypeError, ValueError):
            return False
        return normalized_input == expected_version and output_version > expected_version
    if job.operation == "CONNECT_ACCOUNT":
        if phase != "SUBSCRIPTIONS_APPLIED" or input_version is not None:
            return False
        if job.expected_config_version is None:
            return output_version >= 1
        try:
            expected_version = int(job.expected_config_version)
        except (TypeError, ValueError):
            return False
        return output_version == expected_version
    return False


async def _checkpoint_recovery_proof(
    session,
    job: models.ProvisioningJob,
    *,
    staged_secrets: dict[str, Any] | None = None,
) -> bool:
    """Prove that a checkpoint can resume without reapplying credentials."""
    checkpoint = provisioning_checkpoint(job)
    if checkpoint is None or checkpoint.get("phase") not in _XCHAT_CHECKPOINT_PHASES:
        return False
    if not checkpoint_matches_job(job, checkpoint):
        return False
    try:
        account_id = uuid.UUID(str(checkpoint["account_id"]))
        output_version = checkpoint_output_version(checkpoint)
    except (KeyError, TypeError, ValueError):
        return False
    try:
        target_id = (
            uuid.UUID(str(job.target_account_id))
            if job.target_account_id is not None
            else None
        )
    except (TypeError, ValueError):
        return False
    try:
        job_account_id = uuid.UUID(str(job.account_id))
    except (TypeError, ValueError):
        return False
    if job.account_id is None or account_id != job_account_id:
        return False
    if job.operation == "REAUTHORIZE" and (
        target_id is None
        or account_id != target_id
        or checkpoint.get("target_account_id") != str(target_id)
    ):
        return False
    if not _checkpoint_versions_match_job(job, checkpoint, output_version):
        return False
    account = (
        await session.execute(
            select(models.PlatformAccount)
            .where(
                models.PlatformAccount.id == account_id,
                models.PlatformAccount.tenant_id == job.tenant_id,
                models.PlatformAccount.platform == job.platform,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if account is None:
        return False
    if (
        checkpoint.get("external_account_id") != account.external_account_id
        or checkpoint.get("public_id") != account.public_id
        or account.config_version < output_version
    ):
        return False
    result = dict(job.result or {})
    if result.get("bound_external_account_id") != account.external_account_id:
        return False
    bound_fingerprint = result.get("bound_credential_fingerprint")
    checkpoint_fingerprint = checkpoint.get("credential_fingerprint")
    if (
        not isinstance(bound_fingerprint, str) or not bound_fingerprint
        or not isinstance(checkpoint_fingerprint, str) or not checkpoint_fingerprint
    ):
        return False
    if account.config_version > output_version and bound_fingerprint != checkpoint_fingerprint:
        return False
    if account.config_version == output_version:
        try:
            applied_credentials = decrypt_secret_bundle(account.credential_bundle)
        except (TypeError, ValueError):
            return False
        if not isinstance(applied_credentials, dict):
            return False
        if not credential_fingerprint_matches(applied_credentials, checkpoint_fingerprint):
            return False
        if not credential_fingerprint_matches(applied_credentials, bound_fingerprint):
            return False
    # A later version can replace the output, not erase its atomic checkpoint.
    # The immutable Job/input bindings below must still hold for read-only completion.

    if staged_secrets is None:
        try:
            staged_secrets = (
                decrypt_secret_bundle(job.staging_secret) if job.staging_secret else {}
            )
        except (TypeError, ValueError):
            return False
    if not isinstance(staged_secrets, dict):
        return False
    private_without = result.get(PRIVATE_INPUT_FINGERPRINT_WITHOUT_REQUIRED_KEY)
    if not isinstance(private_without, dict):
        return False
    expected_without = private_without.get("xchat_pin")
    if not isinstance(expected_without, str):
        return False
    if not _input_fingerprint_matches_job(
        job,
        request=dict(job.request or {}),
        secrets=staged_secrets,
        expected=expected_without,
        omit_secret="xchat_pin",
    ):
        return False
    full_expected = result.get(PRIVATE_INPUT_FINGERPRINT_KEY)
    if "xchat_pin" in staged_secrets and full_expected is not None:
        if not _input_fingerprint_matches_job(
            job,
            request=dict(job.request or {}),
            secrets=staged_secrets,
            expected=full_expected,
        ):
            return False
    return True


async def _normalize_xchat_pin_state(
    session,
    job: models.ProvisioningJob,
    *,
    staged_secrets: dict[str, Any] | None = None,
    checkpoint_recovery: bool | None = None,
    mark_missing_pin_on_failure: bool = True,
) -> bool:
    """Remove one-time XChat PINs and clear only a proven XChat marker."""
    if job.platform != "x":
        return False
    if staged_secrets is None:
        try:
            staged_secrets = (
                decrypt_secret_bundle(job.staging_secret) if job.staging_secret else {}
            )
        except (TypeError, ValueError):
            return False
    if not isinstance(staged_secrets, dict):
        return False
    staged = {str(key): value for key, value in staged_secrets.items()}
    has_pin = "xchat_pin" in staged
    has_marker = _xchat_resubmission_marked(job)
    if checkpoint_recovery is None:
        checkpoint_recovery = await _checkpoint_recovery_proof(
            session,
            job,
            staged_secrets=staged,
        )
    if not has_pin and not has_marker:
        if checkpoint_recovery:
            return True
        if (
            not mark_missing_pin_on_failure
            or job.operation != "REAUTHORIZE"
            or provisioning_checkpoint(job) is None
        ):
            return False
        result = dict(job.result or {})
        required_secret = str(result.get("required_secret") or "")
        if (
            not result.get("requires_secret_resubmission")
            and not required_secret
            and not any(
                result.get(key)
                for key in ("manual_followup", "needed_action", "neededAction", "operator_action")
            )
        ):
            result["requires_secret_resubmission"] = True
            result["required_secret"] = "xchat_pin"
            job.result = result
        return False

    staged.pop("xchat_pin", None)
    if _has_control_trust(job.staging_secret):
        staged[_CONTROL_API_SECRET_KEY] = _CONTROL_API_SECRET_VALUE
    job.staging_secret = encrypt_secret_bundle(staged) if staged else None

    result = dict(job.result or {})
    required_secret = str(result.get("required_secret") or "")
    if checkpoint_recovery:
        if has_marker:
            result.pop("requires_secret_resubmission", None)
            result.pop("required_secret", None)
    elif mark_missing_pin_on_failure and required_secret not in {"password"} and (
        not required_secret or required_secret == "xchat_pin"
    ):
        result["requires_secret_resubmission"] = True
        result["required_secret"] = "xchat_pin"
    job.result = result
    return checkpoint_recovery


async def _required_secret_resubmission_allowed(
    session,
    job: models.ProvisioningJob,
    *,
    checkpoint_recovery: bool | None = None,
) -> bool:
    if not requires_secret_resubmission(job):
        return False
    required_secret = str((job.result or {}).get("required_secret") or "")
    if required_secret == "password":
        return True
    if required_secret != "xchat_pin":
        return False
    if checkpoint_recovery is None:
        checkpoint_recovery = await _checkpoint_recovery_proof(session, job)
    return not checkpoint_recovery



async def _merge_required_secret_submission(
    *,
    session,
    job: models.ProvisioningJob,
    submitted_secrets: dict[str, str],
) -> tuple[dict[str, str], str]:
    if not await _required_secret_resubmission_allowed(session, job):
        raise ValueError("provisioning_secret_resubmission_required")
    required_secret = str((job.result or {}).get("required_secret") or "")
    supplied = submitted_secrets.get(required_secret)
    if not isinstance(supplied, str) or not supplied.strip():
        raise ValueError("provisioning_secret_resubmission_required")
    if required_secret == "password":
        _validate_email_secrets({"password": supplied}, allow_password_only=True)
    try:
        staged = decrypt_secret_bundle(job.staging_secret) if job.staging_secret else {}
    except (TypeError, ValueError) as exc:
        raise ValueError("provisioning_authority_invalid") from exc
    if not isinstance(staged, dict):
        raise ValueError("provisioning_authority_invalid")
    merged = {
        str(key): str(value)
        for key, value in staged.items()
        if key != _CONTROL_API_SECRET_KEY
    }
    for key, value in submitted_secrets.items():
        if key == required_secret:
            continue
        if key not in merged or str(value) != merged[key]:
            raise ValueError("idempotency_key_payload_mismatch")
    merged[required_secret] = supplied
    private_without = (job.result or {}).get(PRIVATE_INPUT_FINGERPRINT_WITHOUT_REQUIRED_KEY)
    expected_without = (
        private_without.get(required_secret) if isinstance(private_without, dict) else None
    )
    if not provisioning_input_fingerprint_without_required_matches(
        expected=expected_without,
        required_secret=required_secret,
        tenant_id=job.tenant_id,
        brand_id=job.brand_id,
        platform=job.platform,
        operation=job.operation,
        target_account_id=job.target_account_id,
        expected_config_version=job.expected_config_version,
        request=dict(job.request or {}),
        credential_bundle=merged,
    ):
        raise ValueError("idempotency_key_payload_mismatch")
    return merged, required_secret


def validate_owner_provisioning_policy(
    *,
    platform: str,
    owner_user_id: uuid.UUID | None,
    request: dict[str, Any],
    is_workspace_admin: bool = False,
) -> None:
    """Validate self-service limits using a freshly verified staff capability.

    ``owner_user_id`` records business ownership only.  It is never used to
    infer whether the initiating identity is a workspace administrator.
    """
    if owner_user_id is None or is_workspace_admin:
        return
    requested_automation = str(request.get("automation_default") or "BOT_DRAFT_ONLY")
    if platform not in _USER_SELF_SERVICE_PLATFORMS or requested_automation != "BOT_DRAFT_ONLY":
        raise ValueError("user_provisioning_policy_rejected")


def _validate_email_secrets(secrets: object, *, allow_password_only: bool = False) -> None:
    if not isinstance(secrets, dict):
        raise ValueError("invalid_email_credentials")
    secret_names = set(secrets)
    if secret_names != {"username", "password"} and not (
        allow_password_only and secret_names == {"password"}
    ):
        raise ValueError("invalid_email_credentials")
    try:
        for value in secrets.values():
            validate_email_account_text(value, maximum=MAX_EMAIL_CREDENTIAL_CHARS)
    except ValueError:
        raise ValueError("invalid_email_credentials") from None


def _email_resubmission_staging_secret(staging_secret: object) -> dict | None:
    try:
        values = decrypt_secret_bundle(staging_secret) if staging_secret else {}
    except (TypeError, ValueError):
        values = {}
    values.pop("password", None)
    if _has_control_trust(staging_secret):
        values[_CONTROL_API_SECRET_KEY] = _CONTROL_API_SECRET_VALUE
    return encrypt_secret_bundle(values) if values else None


def _email_resubmission_result(result: object) -> dict[str, Any]:
    public_result = dict(result) if isinstance(result, dict) else {}
    return {
        **public_result,
        "requires_secret_resubmission": True,
        "required_secret": "password",
    }


def _email_authority_marker(staging_secret: object) -> dict | None:
    if not _has_control_trust(staging_secret):
        return None
    return encrypt_secret_bundle({_CONTROL_API_SECRET_KEY: _CONTROL_API_SECRET_VALUE})


def _mark_email_needs_action(
    job: models.ProvisioningJob,
    *,
    error_code: str,
    message: str,
) -> None:
    job.status = "NEEDS_ACTION"
    job.current_step = "FAILED"
    job.next_attempt_at = None
    job.locked_at = None
    job.locked_by = None
    job.last_error_code = error_code
    job.last_error_message = message
    job.staging_secret = _email_resubmission_staging_secret(job.staging_secret)
    job.result = _email_resubmission_result(job.result)


def _email_terminal_values(
    result: object,
    staging_secret: object,
) -> dict[str, Any]:
    return {
        "staging_secret": _email_resubmission_staging_secret(staging_secret),
        "result": _email_resubmission_result(result),
    }


def _error(exc: Exception) -> tuple[str, str, bool]:
    if isinstance(exc, EmailNetworkError):
        return (
            exc.code,
            "Email endpoint validation failed",
            exc.code == "email_dns_resolution_failed",
        )
    if isinstance(exc, ImapClientError):
        if exc.code in {"imap_authentication_failed", "imap_login_failed"}:
            return exc.code, "Email IMAP credentials were rejected", False
        if exc.retryable:
            return exc.code, "Email IMAP service is temporarily unavailable", True
        return exc.code, "Email IMAP protocol validation failed", False
    if isinstance(exc, imaplib.IMAP4.error):
        return "imap_protocol_error", "Email IMAP protocol validation failed", False
    if isinstance(exc, PermanentSendError):
        return exc.code, "Email SMTP validation failed", False
    if isinstance(exc, RetryableSendError):
        return exc.code, "Email SMTP validation is temporarily unavailable", True
    if isinstance(exc, ssl.SSLError):
        return "EMAIL_TLS_INVALID", "Email TLS validation failed", False
    if isinstance(exc, TimeoutError):
        return "EMAIL_CONNECTION_TIMEOUT", "Email service connection timed out", True
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        retryable = status >= 500 or status == 429
        return f"PLATFORM_HTTP_{status}", f"Platform API returned HTTP {status}", retryable
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
        return "PLATFORM_UNAVAILABLE", "Platform API is temporarily unavailable", True
    if isinstance(exc, LookupError):
        return "DEPENDENCY_NOT_FOUND", str(exc)[:500], False
    if isinstance(exc, XChatActivationError):
        # The XChat PIN is removed after the first attempt. Never schedule an
        # automatic retry that would silently reconnect without unlocking keys.
        return exc.code, exc.operator_message, False
    if isinstance(exc, FeishuClientError):
        return exc.code, "Feishu account validation failed", exc.retryable
    if isinstance(exc, MetaCommentPermissionError):
        return (
            "META_COMMENT_PERMISSION_REQUIRED",
            "请重新授权 Meta 账号，并允许该 Facebook Page 或 Instagram 账号的评论权限。",
            False,
        )
    if isinstance(exc, PermissionError):
        if str(exc) == "platform_account_owner_conflict":
            return (
                "ACCOUNT_OWNER_CONFLICT",
                "该平台账号已绑定到其他用户或 Tenant 共享范围，不能重复认领。",
                False,
            )
        if str(exc) in {"account_reauthorization_denied", "reauthorization_requires_session"}:
            return "ACCOUNT_REAUTHORIZATION_DENIED", "无权重新授权该平台账号", False
        if str(exc) in {
            "initiator_session_invalid",
            "admin_session_invalid",
            "provisioning_authority_invalid",
            "control_api_trust_required",
        }:
            return "INITIATOR_SESSION_INVALID", "发起授权的身份已失效或未经过验证", False
        return "PROVISIONING_PERMISSION_DENIED", "Provisioning permission denied", False
    if isinstance(exc, ValueError) and str(exc) in {
        "platform_account_already_exists",
        "platform_account_target_mismatch",
    }:
        return "ACCOUNT_TARGET_CONFLICT", "目标平台账号已存在或身份不匹配", False
    if isinstance(exc, ValueError) and str(exc) in {
        "platform_app_rotation_required",
        "meta_app_rotation_required",
        "x_app_rotation_required",
    }:
        return (
            "PLATFORM_APP_ROTATION_REQUIRED",
            "平台 App 凭证已变化，需要执行显式 App 级轮换",
            False,
        )
    if isinstance(exc, ValueError) and str(exc) == "account_reauthorization_version_conflict":
        return "ACCOUNT_VERSION_CONFLICT", "平台账号配置已变化，请重新授权", False
    if isinstance(exc, ValueError) and str(exc) == "provisioning_checkpoint_superseded":
        return (
            "CHECKPOINT_SUPERSEDED",
            "平台账号在本 Job 阶段后已被管理员修改，保留后续配置并需要重新检查订阅阶段",
            False,
        )
    if isinstance(exc, ValueError) and str(exc).startswith("x_direct_message_permission_missing:"):
        return (
            "X_DM_PERMISSION_REQUIRED",
            "请在 X Developer Portal 将 App permissions 设为 "
            "Read and write and Direct message，保存后重新授权账号。",
            False,
        )
    if isinstance(exc, (ValueError, KeyError)):
        return "INVALID_REQUEST", str(exc)[:500], False
    logger.exception("provisioning job failed")
    return "INTERNAL_ERROR", "Provisioning failed; inspect server logs", True


async def _lock_staff_and_sessions(
    session,
    *,
    staff_ids: set[uuid.UUID],
    session_ids: set[uuid.UUID],
) -> None:
    for user_id in sorted(staff_ids, key=str):
        await lock_user_authority(session, user_id)
    ordered_sessions = tuple(sorted(session_ids, key=str))
    if not ordered_sessions:
        return
    await lock_session_authorities(session, ordered_sessions)
    await session.execute(
        select(models.AdminSession)
        .where(models.AdminSession.id.in_(ordered_sessions))
        .execution_options(populate_existing=True)
        .order_by(models.AdminSession.id)
        .with_for_update()
    )


def _principal_snapshot(principal: Principal) -> tuple[Any, ...]:
    return (
        principal.session_id,
        principal.user_id,
        principal.actor,
        principal.role,
        principal.allowed_tenants,
        principal.must_change_password,
        principal.authentication_kind,
        principal.is_superadmin,
    )


def _job_authority_snapshot(job: models.ProvisioningJob) -> tuple[Any, ...]:
    return (
        job.authority_kind,
        job.authority_version,
        job.initiator_user_id,
        job.initiator_session_id,
    )


async def _lock_claimed_processing_job(
    session,
    claimed_job: models.ProvisioningJob,
) -> models.ProvisioningJob | None:
    """Lock claim identities before refreshing the claimed Job row."""
    claim_status = claimed_job.status
    claim_attempt_count = claimed_job.attempt_count
    claim_authority = _job_authority_snapshot(claimed_job)
    if claim_status != "PROCESSING":
        return None

    staff_ids = (
        {claimed_job.initiator_user_id}
        if claimed_job.initiator_user_id is not None
        else set()
    )
    session_ids = (
        {claimed_job.initiator_session_id}
        if claimed_job.initiator_session_id is not None
        else set()
    )
    await _lock_staff_and_sessions(
        session,
        staff_ids=staff_ids,
        session_ids=session_ids,
    )
    live_job = (
        await session.execute(
            select(models.ProvisioningJob)
            .where(models.ProvisioningJob.id == claimed_job.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if (
        live_job is None
        or live_job.status != claim_status
        or live_job.attempt_count != claim_attempt_count
        or _job_authority_snapshot(live_job) != claim_authority
    ):
        return None
    return live_job


def _principal_is_current_for_tenant(principal: Principal | None, tenant_id: str) -> bool:
    return bool(
        principal is not None
        and principal.session_id is not None
        and tenant_id in principal.allowed_tenants
        and not principal.must_change_password
        and principal.has_capability("connect")
    )



async def _validate_job_authority(
    session,
    job: models.ProvisioningJob,
    *,
    validate_target: bool = True,
    authority_locks_held: bool = False,
):
    """Revalidate the durable authority recorded on a job.

    The kind/version fields are authoritative.  Session NULL-ness and actor
    text are only consistency checks; neither can grant machine authority.
    When ``authority_locks_held`` is true, staff/session rows were already
    locked in the global order and only the Job/target checks run here.
    The retry path acquires Job before the target account.
    """
    authority_kind = getattr(job, "authority_kind", "UNVERIFIED")
    authority_version = getattr(job, "authority_version", 0)
    if authority_kind not in _AUTHORITY_KINDS or authority_version != _AUTHORITY_VERSION:
        raise PermissionError("provisioning_authority_invalid")

    session_id = getattr(job, "initiator_session_id", None)
    user_id = getattr(job, "initiator_user_id", None)
    if authority_kind == "CONTROL_API":
        if (
            getattr(job, "operation", "CONNECT_ACCOUNT") != "CONNECT_ACCOUNT"
            or session_id is not None
            or user_id is not None
            or not _has_control_trust(getattr(job, "staging_secret", None))
        ):
            raise PermissionError("provisioning_authority_invalid")
        return None

    if session_id is None:
        raise PermissionError("initiator_session_invalid")
    if authority_kind == "STAFF_SESSION" and user_id is None:
        raise PermissionError("initiator_session_invalid")
    if authority_kind == "BOOTSTRAP_SESSION" and user_id is not None:
        raise PermissionError("initiator_session_invalid")
    if _has_control_trust(getattr(job, "staging_secret", None)):
        raise PermissionError("provisioning_authority_invalid")

    # The staff advisory lock must precede the session row lock.  This is the
    # same order used by staff lifecycle mutations.
    # The staff/session locks are normally acquired here; retry and
    # continuation pass authority_locks_held after acquiring the full set.
    if user_id is not None and not authority_locks_held:
        await lock_user_authority(session, user_id)
    principal = await principal_from_session_row(
        session,
        session_id,
        for_update=not authority_locks_held,
    )
    if (
        principal is None
        or principal.session_id != uuid.UUID(str(session_id))
        or principal.user_id != user_id
        or job.tenant_id not in principal.allowed_tenants
        or principal.must_change_password
        or not principal.has_capability("connect")
        or (authority_kind == "BOOTSTRAP_SESSION" and not principal.is_superadmin)
        or (
            authority_kind == "STAFF_SESSION"
            and principal.user_id is None
        )
    ):
        raise PermissionError("initiator_session_invalid")

    if validate_target and getattr(job, "operation", "CONNECT_ACCOUNT") == "REAUTHORIZE":
        target_id = getattr(job, "target_account_id", None)
        if target_id is None:
            raise ValueError("platform_account_target_required")
        account = (
            await session.execute(
                select(models.PlatformAccount)
                .where(
                    models.PlatformAccount.id == target_id,
                    models.PlatformAccount.tenant_id == job.tenant_id,
                    models.PlatformAccount.platform == job.platform,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if account is None:
            raise LookupError("platform_account_target_not_found")
        checkpoint = provisioning_checkpoint(job)
        checkpoint_applied = await _checkpoint_recovery_proof(session, job)
        if checkpoint_applied:
            if account.config_version < checkpoint_output_version(checkpoint):
                raise ValueError("provisioning_checkpoint_lost")
            expected_version = None
        else:
            expected_version = getattr(job, "expected_config_version", None)
        await require_reauthorization(
            session,
            principal=principal,
            account=account,
            expected_config_version=expected_version,
        )
    return principal


def canonical_provisioning_operation(operation: str) -> str:
    if operation == "REAUTHORIZE_ACCOUNT":
        return "REAUTHORIZE"
    if operation not in {"CONNECT_ACCOUNT", "REAUTHORIZE"}:
        raise ValueError("unsupported_provisioning_operation")
    return operation


async def submit_provisioning_job(
    *,
    tenant_id: str,
    brand_id: str,
    platform: str,
    actor: str,
    request: dict[str, Any],
    secrets: dict[str, str],
    operation: str = "CONNECT_ACCOUNT",
    target_account_id: uuid.UUID | str | None = None,
    expected_config_version: int | None = None,
    admin_session_id: uuid.UUID | str | None = None,
    _control_api_trust: object | None = None,
    _authority_token: object | None = None,
) -> uuid.UUID:
    if platform not in PROVISIONABLE_ACCOUNT_PLATFORMS:
        raise ValueError(f"unsupported_platform:{platform}")
    operation = canonical_provisioning_operation(operation)
    # A password-only bundle is validated only after an existing NEEDS_ACTION
    # job has been found and its retained username/fingerprint are checked.
    if platform == "email":
        secret_names = set(secrets)
        if secret_names == {"username", "password"}:
            _validate_email_secrets(secrets)
        elif secret_names != {"password"}:
            _validate_email_secrets(secrets)
    if (
        not tenant_id
        or len(tenant_id) > 64
        or not tenant_id.isascii()
        or not all(ch.isalnum() or ch in {"_", "-"} for ch in tenant_id)
    ):
        raise ValueError("invalid_tenant_id")
    if (
        not brand_id
        or len(brand_id) > 64
        or not brand_id.isascii()
        or not all(ch.isalnum() or ch in {"_", "-"} for ch in brand_id)
    ):
        raise ValueError("invalid_brand_id")
    try:
        target_uuid = uuid.UUID(str(target_account_id)) if target_account_id is not None else None
    except (TypeError, ValueError) as exc:
        raise ValueError("platform_account_target_invalid") from exc
    if operation == "REAUTHORIZE" and target_uuid is None:
        raise ValueError("platform_account_target_required")
    if operation == "CONNECT_ACCOUNT" and (
        target_uuid is not None or expected_config_version is not None
    ):
        raise ValueError("connect_target_not_allowed")
    if expected_config_version is not None and expected_config_version < 1:
        raise ValueError("invalid_expected_config_version")

    job_id = uuid.uuid4()
    owner_user_id: uuid.UUID | None = None
    initiator_user_id: uuid.UUID | None = None
    initiator_session_uuid: uuid.UUID | None = None
    safe_request = _safe_request(platform, request)
    existing_job = None
    control_api_trusted = False
    authority_kind = "UNVERIFIED"
    authority_version = 0
    async with get_session_factory()() as session:
        principal = None
        if admin_session_id is not None:
            if _authority_token is not None or _control_api_trust is not None:
                raise PermissionError("provisioning_authority_invalid")
            # Read only to discover the staff lock key.  The locked reload below
            # is authoritative and rejects any session/user identity change.
            initial_principal = await principal_from_session_row(
                session, admin_session_id, for_update=False
            )
            if initial_principal is None or tenant_id not in initial_principal.allowed_tenants:
                raise PermissionError("admin_session_invalid")
            initial_user_id = initial_principal.user_id
            if initial_user_id is not None:
                await lock_user_authority(session, initial_user_id)
            principal = await principal_from_session_row(session, admin_session_id, for_update=True)
            if (
                principal is None
                or principal.session_id != uuid.UUID(str(admin_session_id))
                or principal.user_id != initial_user_id
                or principal.must_change_password
                or tenant_id not in principal.allowed_tenants
                or not principal.has_capability("connect")
            ):
                raise PermissionError("admin_session_invalid")
            authority_kind = "BOOTSTRAP_SESSION" if principal.is_superadmin else "STAFF_SESSION"
            authority_version = _AUTHORITY_VERSION
            actor = principal.actor
            owner_user_id = principal.user_id
            initiator_user_id = principal.user_id
            initiator_session_uuid = principal.session_id
        else:
            if (
                _control_api_trust is not _CONTROL_API_TRUST_TOKEN
                or _authority_token is not _CONTROL_API_AUTHORITY_TOKEN
                or operation != "CONNECT_ACCOUNT"
            ):
                raise PermissionError("control_api_trust_required")
            control_api_trusted = True
            authority_kind = "CONTROL_API"
            authority_version = _AUTHORITY_VERSION
            actor = "service:control_api"
        key = _idempotency_key(
            tenant_id,
            platform,
            request,
            operation=operation,
            target_account_id=target_uuid,
            expected_config_version=expected_config_version,
        )
        if target_uuid is not None:
            if principal is None:
                raise PermissionError("reauthorization_requires_session")
            target_exists = await session.scalar(
                select(models.PlatformAccount.id).where(
                    models.PlatformAccount.id == target_uuid,
                    models.PlatformAccount.tenant_id == tenant_id,
                    models.PlatformAccount.platform == platform,
                )
            )
            if target_exists is None:
                raise LookupError("platform_account_target_not_found")
        input_fingerprint_result = _input_fingerprint_record(
            tenant_id=tenant_id,
            brand_id=brand_id,
            platform=platform,
            operation=operation,
            target_account_id=target_uuid,
            expected_config_version=expected_config_version,
            request=safe_request,
            secrets=secrets,
        )
        inserted = (
            await session.execute(
                pg_insert(models.ProvisioningJob)
                .values(
                    id=job_id,
                    tenant_id=tenant_id,
                    brand_id=brand_id,
                    platform=platform,
                    operation=operation,
                    actor=actor,
                    owner_user_id=owner_user_id,
                    initiator_user_id=initiator_user_id,
                    initiator_session_id=initiator_session_uuid,
                    authority_kind=authority_kind,
                    authority_version=authority_version,
                    target_account_id=target_uuid,
                    expected_config_version=expected_config_version,
                    idempotency_key=key,
                    request=safe_request,
                    staging_secret=encrypt_secret_bundle(
                        _staging_secrets(secrets, control_trusted=control_api_trusted)
                    ),
                    result=input_fingerprint_result,
                    status="PENDING",
                    current_step="QUEUED",
                )
                .on_conflict_do_nothing(index_elements=["tenant_id", "idempotency_key"])
                .returning(models.ProvisioningJob.id)
            )
        ).scalar_one_or_none()
        existing = None
        if inserted is None:
            existing = (
                await session.execute(
                    select(models.ProvisioningJob)
                    .where(
                        models.ProvisioningJob.tenant_id == tenant_id,
                        models.ProvisioningJob.idempotency_key == key,
                    )
                    .with_for_update()
                )
            ).scalar_one()
            if (
                existing.authority_kind != authority_kind
                or existing.authority_version != authority_version
            ):
                raise PermissionError("provisioning_authority_invalid")
            if existing.status == "COMPLETED" and not _input_fingerprint_matches_job(
                existing,
                request=safe_request,
                secrets=secrets,
                expected=(existing.result or {}).get(PRIVATE_INPUT_FINGERPRINT_KEY),
            ):
                raise ValueError("idempotency_key_payload_mismatch")

        if target_uuid is not None:
            target_account = (
                await session.execute(
                    select(models.PlatformAccount)
                    .where(
                        models.PlatformAccount.id == target_uuid,
                        models.PlatformAccount.tenant_id == tenant_id,
                        models.PlatformAccount.platform == platform,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if target_account is None:
                raise LookupError("platform_account_target_not_found")
            if not target_account.external_account_id:
                raise ValueError("platform_account_target_identity_missing")
            checkpoint_applied = (
                existing is not None
                and operation == "REAUTHORIZE"
                and await _checkpoint_recovery_proof(
                    session,
                    existing,
                    # Completed jobs erase staging secrets. Only an exact input
                    # replay may supply them for proof, never for persistence.
                    staged_secrets=secrets if existing.status == "COMPLETED" else None,
                )
            )
            if expected_config_version is None:
                expected_config_version = target_account.config_version
            if not checkpoint_applied:
                await require_reauthorization(
                    session,
                    principal=principal,
                    account=target_account,
                    expected_config_version=expected_config_version,
                )
            submitted_external_id = safe_request.get("external_account_id")
            if (
                submitted_external_id is not None
                and str(submitted_external_id) != target_account.external_account_id
            ):
                raise ValueError("platform_account_target_mismatch")
            input_fingerprint_result = _input_fingerprint_record(
                tenant_id=tenant_id,
                brand_id=brand_id,
                platform=platform,
                operation=operation,
                target_account_id=target_uuid,
                expected_config_version=expected_config_version,
                request=safe_request,
                secrets=secrets,
            )
            if inserted is not None:
                await session.execute(
                    update(models.ProvisioningJob)
                    .where(models.ProvisioningJob.id == inserted)
                    .values(
                        expected_config_version=expected_config_version,
                        result=input_fingerprint_result,
                    )
                )

        if inserted is not None:
            if platform == "email" and set(secrets) == {"password"}:
                raise ValueError("invalid_email_credentials")
        else:
            existing_checkpoint_recovery = await _checkpoint_recovery_proof(
                session,
                existing,
                staged_secrets=secrets if existing.status == "COMPLETED" else None,
            )
            if existing.platform == "x" and existing.status != "COMPLETED":
                existing_checkpoint_recovery = await _normalize_xchat_pin_state(
                    session,
                    existing,
                    checkpoint_recovery=existing_checkpoint_recovery,
                )
            if (
                existing.platform != platform
                or existing.brand_id != brand_id
                or existing.operation != operation
                or existing.target_account_id != target_uuid
                or (
                    existing.expected_config_version != expected_config_version
                    and not (
                        (
                            operation == "CONNECT_ACCOUNT"
                            and existing.account_id is not None
                            and provisioning_checkpoint(existing) is not None
                        )
                        or (operation == "REAUTHORIZE" and existing_checkpoint_recovery)
                    )
                )
                or dict(existing.request or {}) != safe_request
                or existing.owner_user_id != owner_user_id
                or existing.initiator_user_id != initiator_user_id
                or existing.initiator_session_id != initiator_session_uuid
            ):
                raise ValueError("idempotency_key_payload_mismatch")
            existing_result = dict(existing.result or {})
            full_fingerprint = existing_result.get(PRIVATE_INPUT_FINGERPRINT_KEY)
            full_input_matches = _input_fingerprint_matches_job(
                existing,
                request=safe_request,
                secrets=secrets,
                expected=full_fingerprint,
            )
            required_secret = str(existing_result.get("required_secret") or "")
            without_required = existing_result.get(
                PRIVATE_INPUT_FINGERPRINT_WITHOUT_REQUIRED_KEY
            )
            continuation_candidate = (
                (
                    await _required_secret_resubmission_allowed(session, existing)
                    and required_secret in {"xchat_pin", "password"}
                    and isinstance(without_required, dict)
                    and isinstance(without_required.get(required_secret), str)
                )
                or (
                    existing_checkpoint_recovery
                    and isinstance(without_required, dict)
                    and _input_fingerprint_matches_job(
                        existing,
                        request=safe_request,
                        secrets=secrets,
                        expected=without_required.get("xchat_pin"),
                        omit_secret="xchat_pin",
                    )
                )
            )
            if not full_input_matches and not continuation_candidate:
                raise ValueError("idempotency_key_payload_mismatch")
            job_id = existing.id
            existing_job = existing
        await session.commit()

    if (
        inserted is None
        and existing_job is not None
        and existing_job.status in {"FAILED", "NEEDS_ACTION"}
    ):
        async with get_session_factory()() as session:
            discovery = (
                await session.execute(
                    select(models.ProvisioningJob).where(
                        models.ProvisioningJob.id == job_id,
                        models.ProvisioningJob.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if discovery is None:
                raise LookupError("provisioning_job_not_found")
            initial_authority = (
                discovery.authority_kind,
                discovery.authority_version,
                discovery.initiator_user_id,
                discovery.initiator_session_id,
            )
            caller_before = None
            caller_session_uuid = None
            if admin_session_id is not None:
                caller_session_uuid = uuid.UUID(str(admin_session_id))
                caller_before = await principal_from_session_row(
                    session, caller_session_uuid, for_update=False
                )
                if not _principal_is_current_for_tenant(caller_before, tenant_id):
                    raise PermissionError("admin_session_invalid")
            staff_ids = {
                user_id
                for user_id in (
                    caller_before.user_id if caller_before else None,
                    discovery.initiator_user_id,
                )
                if user_id is not None
            }
            session_ids = {
                session_id
                for session_id in (
                    caller_session_uuid,
                    discovery.initiator_session_id,
                )
                if session_id is not None
            }
            await _lock_staff_and_sessions(
                session,
                staff_ids=staff_ids,
                session_ids=session_ids,
            )
            if caller_session_uuid is not None:
                caller_after = await principal_from_session_row(
                    session, caller_session_uuid, for_update=False
                )
                if (
                    not _principal_is_current_for_tenant(caller_after, tenant_id)
                    or caller_before is None
                    or caller_after is None
                    or _principal_snapshot(caller_before) != _principal_snapshot(caller_after)
                ):
                    raise PermissionError("admin_session_invalid")
            job = (
                await session.execute(
                    select(models.ProvisioningJob)
                    .where(
                        models.ProvisioningJob.id == job_id,
                        models.ProvisioningJob.tenant_id == tenant_id,
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if job is None:
                raise LookupError("provisioning_job_not_found")
            if initial_authority != (
                job.authority_kind,
                job.authority_version,
                job.initiator_user_id,
                job.initiator_session_id,
            ):
                raise PermissionError("provisioning_authority_invalid")
            await _validate_job_authority(
                session, job, authority_locks_held=True
            )
            checkpoint_recovery = await _checkpoint_recovery_proof(session, job)
            if job.platform == "x":
                checkpoint_recovery = await _normalize_xchat_pin_state(
                    session,
                    job,
                    checkpoint_recovery=checkpoint_recovery,
                )
            if job.status not in {"FAILED", "NEEDS_ACTION"}:
                raise ValueError("provisioning_job_not_retryable")
            control_api_trusted = job.authority_kind == "CONTROL_API"
            if control_api_trusted != _has_control_trust(job.staging_secret):
                raise PermissionError("provisioning_authority_invalid")
            result = dict(job.result or {})
            if requires_secret_resubmission(job):
                merged_secrets, _required_secret = await _merge_required_secret_submission(
                    session=session,
                    job=job,
                    submitted_secrets=secrets,
                )
                result.pop("requires_secret_resubmission", None)
                result.pop("required_secret", None)
                result.update(
                    _input_fingerprint_record(
                        tenant_id=job.tenant_id,
                        brand_id=job.brand_id,
                        platform=job.platform,
                        operation=job.operation,
                        target_account_id=job.target_account_id,
                        expected_config_version=job.expected_config_version,
                        request=dict(job.request or {}),
                        secrets=merged_secrets,
                    )
                )
                job.staging_secret = encrypt_secret_bundle(
                    _staging_secrets(merged_secrets, control_trusted=control_api_trusted)
                )
            job.status = "PENDING"
            job.current_step = "QUEUED"
            job.next_attempt_at = None
            job.result = result
            job.attempt_count = 0 if job.attempt_count >= _MAX_ATTEMPTS else job.attempt_count
            job.last_error_code = None
            job.last_error_message = None
            await session.commit()
    await record_account_management_audit(
        tenant_id=tenant_id,
        actor=actor,
        action="provisioning_submitted",
        subject_id=str(job_id),
        detail={
            "platform": platform,
            "brand_id": brand_id,
            "operation": operation,
            "target_account_id": str(target_uuid) if target_uuid else None,
            "expected_config_version": expected_config_version,
        },
    )
    return job_id


async def submit_control_provisioning_job(
    *,
    tenant_id: str,
    brand_id: str,
    platform: str,
    actor: str,
    request: dict[str, Any],
    secrets: dict[str, str],
) -> uuid.UUID:
    """Authenticated Control API adapter; only this path mints machine authority."""
    return await submit_provisioning_job(
        tenant_id=tenant_id,
        brand_id=brand_id,
        platform=platform,
        actor=actor,
        request=request,
        secrets=secrets,
        operation="CONNECT_ACCOUNT",
        _control_api_trust=_CONTROL_API_TRUST_TOKEN,
        _authority_token=_CONTROL_API_AUTHORITY_TOKEN,
    )


async def _claim_job(job_id: uuid.UUID) -> models.ProvisioningJob | None:
    now = datetime.now(UTC)
    async with get_session_factory()() as session:
        initial = (
            await session.execute(
                select(models.ProvisioningJob).where(
                    models.ProvisioningJob.id == job_id,
                    models.ProvisioningJob.status.in_(["PENDING", "FAILED"]),
                    models.ProvisioningJob.attempt_count < _MAX_ATTEMPTS,
                    or_(
                        models.ProvisioningJob.next_attempt_at.is_(None),
                        models.ProvisioningJob.next_attempt_at <= now,
                    ),
                )
            )
        ).scalar_one_or_none()
        if initial is None:
            await session.commit()
            return None

        initial_authority = _job_authority_snapshot(initial)
        current_authority = (
            await session.execute(
                select(
                    models.ProvisioningJob.authority_kind,
                    models.ProvisioningJob.authority_version,
                    models.ProvisioningJob.initiator_user_id,
                    models.ProvisioningJob.initiator_session_id,
                )
                .where(models.ProvisioningJob.id == job_id)
                .execution_options(populate_existing=True)
            )
        ).one_or_none()
        if current_authority is None:
            await session.commit()
            return None
        if tuple(current_authority) != initial_authority:
            await session.rollback()
            return None
        await _lock_staff_and_sessions(
            session,
            staff_ids={
                initial.initiator_user_id
            }
            if initial.initiator_user_id is not None
            else set(),
            session_ids={
                initial.initiator_session_id
            }
            if initial.initiator_session_id is not None
            else set(),
        )

        row = (
            await session.execute(
                select(models.ProvisioningJob)
                .where(
                    models.ProvisioningJob.id == job_id,
                    models.ProvisioningJob.status.in_(["PENDING", "FAILED"]),
                    models.ProvisioningJob.attempt_count < _MAX_ATTEMPTS,
                    or_(
                        models.ProvisioningJob.next_attempt_at.is_(None),
                        models.ProvisioningJob.next_attempt_at <= now,
                    ),
                )
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if row is None:
            await session.commit()
            return None
        if _job_authority_snapshot(row) != initial_authority:
            await session.rollback()
            return None
        authority_error: Exception | None = None
        try:
            await _validate_job_authority(
                session,
                row,
                validate_target=False,
                authority_locks_held=True,
            )
        except (LookupError, PermissionError, ValueError) as exc:
            authority_error = exc
        if authority_error is not None:
            row.status = "NEEDS_ACTION"
            row.current_step = "FAILED"
            row.next_attempt_at = None
            row.locked_at = None
            row.locked_by = None
            row.last_error_code = "PROVISIONING_AUTHORITY_INVALID"
            row.last_error_message = "Provisioning authority is missing, expired, or changed"
            await session.commit()
            return None

        settings = get_settings()
        if not settings.platform_integration_enabled(row.platform):
            error_code = settings.platform_disabled_code(row.platform) or "PLATFORM_DISABLED"
            if row.platform == "email":
                _mark_email_needs_action(
                    row,
                    error_code=error_code,
                    message="Email integration is disabled; resubmit account credentials",
                )
            else:
                row.status = _PLATFORM_DISABLED_STATUS
                row.current_step = _PLATFORM_DISABLED_STATUS
                row.next_attempt_at = None
                row.locked_at = None
                row.locked_by = None
                row.last_error_code = error_code
                row.last_error_message = "Platform integration is disabled"
        else:
            row.status = "PROCESSING"
            row.current_step = "VALIDATE_CREDENTIAL"
            row.locked_at = now
            row.locked_by = "provisioning-worker"
            row.attempt_count += 1
            row.last_error_code = None
            row.last_error_message = None
        await session.commit()
        return row


def _result_payload(result: AccountConnectionResult) -> dict[str, Any]:
    return {
        "account_id": str(result.account_id),
        "platform": result.platform,
        "external_account_id": result.external_account_id,
        "public_id": result.public_id,
        "webhook_url": result.webhook_url,
        "name": result.name,
        "provider_username": result.provider_username,
        "avatar_url": result.avatar_url,
        "profile_updated_at": (
            result.profile_updated_at.isoformat() if result.profile_updated_at is not None else None
        ),
        "automation_default": result.automation_default,
        "platform_app_id": str(result.platform_app_id) if result.platform_app_id else None,
        "app_public_id": result.app_public_id,
        "pending_update_count": result.pending_update_count,
        "last_webhook_error": result.last_webhook_error,
        "bot_name": result.bot_name,
        "bot_status": result.bot_status,
        "callback_url": result.webhook_url if result.platform == "feishu" else None,
        "manual_steps": list(result.manual_steps),
        "credential_updated": result.credential_updated,
        "connection_ready": result.connection_ready,
        "connection_status": result.connection_status,
        "checkpoint_phase": result.checkpoint_phase,
        "input_config_version": result.input_config_version,
        "output_config_version": result.output_config_version,
        "observed_config_version": result.observed_config_version,
        "configuration_changed_after_provisioning": result.configuration_changed_after_provisioning,
        "next_phase": result.next_phase,
    }


async def _connect(job: models.ProvisioningJob) -> AccountConnectionResult:
    settings = get_settings()
    request = dict(getattr(job, "request", {}) or {})
    operation = getattr(job, "operation", "CONNECT_ACCOUNT")
    authority_principal = None
    if operation != "REAUTHORIZE":
        # The worker rechecks the live identity before applying self-service policy.
        async with get_session_factory()() as session:
            authority_principal = await _validate_job_authority(session, job)
            await session.commit()
        validate_owner_provisioning_policy(
            platform=job.platform,
            owner_user_id=getattr(job, "owner_user_id", None),
            request=request,
            is_workspace_admin=bool(
                authority_principal is not None and authority_principal.is_workspace_admin
            ),
        )
    else:
        async with get_session_factory()() as session:
            authority_principal = await _validate_job_authority(session, job)
            await session.commit()
    checkpoint = provisioning_checkpoint(job)
    if checkpoint is not None:
        async with get_session_factory()() as recovery_session:
            live_job = await _lock_claimed_processing_job(recovery_session, job)
            if live_job is None:
                raise ValueError("provisioning_claim_lost")
            await _validate_job_authority(
                recovery_session,
                live_job,
                authority_locks_held=True,
            )
            live_checkpoint = provisioning_checkpoint(live_job)
            job = live_job
            if live_checkpoint is None:
                if operation == "REAUTHORIZE":
                    raise ValueError("provisioning_checkpoint_invalid")
                await recovery_session.commit()
            else:
                checkpoint_recovery = await _checkpoint_recovery_proof(
                    recovery_session,
                    live_job,
                )
                if checkpoint_recovery:
                    checkpoint_phase = live_checkpoint.get("phase")
                    if (
                        operation == "REAUTHORIZE"
                        and checkpoint_phase == "CREDENTIALS_APPLIED"
                    ) or (
                        operation == "CONNECT_ACCOUNT"
                        and checkpoint_phase == "SUBSCRIPTIONS_APPLIED"
                    ):
                        resumed = await resume_checkpointed_provisioning(
                            job=live_job,
                            checkpoint=live_checkpoint,
                            public_base_url=settings.public_base_url,
                        )
                        if not await _checkpoint_recovery_proof(
                            recovery_session,
                            live_job,
                        ):
                            raise ValueError("provisioning_checkpoint_invalid")
                        await recovery_session.commit()
                        return resumed
                if operation == "REAUTHORIZE":
                    raise ValueError("provisioning_checkpoint_invalid")
                await recovery_session.commit()
    if not settings.platform_integration_enabled(job.platform):
        raise ValueError(f"{job.platform}_integration_disabled")
    credentials = decrypt_secret_bundle(job.staging_secret)
    control_api_trusted = getattr(job, "authority_kind", "UNVERIFIED") == "CONTROL_API"
    if control_api_trusted:
        if credentials.pop(_CONTROL_API_SECRET_KEY, None) != _CONTROL_API_SECRET_VALUE:
            raise PermissionError("provisioning_authority_invalid")
    if (
        operation == "REAUTHORIZE"
        and not control_api_trusted
        and getattr(job, "initiator_session_id", None) is None
    ):
        raise PermissionError("reauthorization_requires_session")
    common = {
        "public_base_url": settings.public_base_url,
        "tenant_id": job.tenant_id,
        "brand_id": job.brand_id,
        "name": request.get("name"),
        "public_id": request.get("public_id"),
        "secrets_root": Path(settings.account_secrets_root),
        "automation_default": request.get("automation_default", "BOT_DRAFT_ONLY"),
        "owner_user_id": getattr(job, "owner_user_id", None),
        "operation": operation,
        "target_account_id": getattr(job, "target_account_id", None),
        "expected_config_version": getattr(job, "expected_config_version", None),
        "initiator_user_id": getattr(job, "initiator_user_id", None),
        "initiator_session_id": getattr(job, "initiator_session_id", None),
        "provisioning_job_id": getattr(job, "id", None),
        "trusted_control_api": control_api_trusted,
        "authority_kind": getattr(job, "authority_kind", "UNVERIFIED"),
        "authority_version": getattr(job, "authority_version", 0),
        "provisioning_attempt_count": getattr(job, "attempt_count", None),
    }
    if job.platform == "telegram":
        return await connect_telegram_account(
            token=credentials["token"],
            rotate_webhook_secret=bool(request.get("rotate_webhook_secret", False)),
            drop_pending_updates=bool(request.get("drop_pending_updates", False)),
            **common,
        )
    if job.platform in {"facebook", "instagram"}:
        return await connect_meta_account(
            platform=job.platform,
            external_account_id=str(request["external_account_id"]),
            access_token=credentials["access_token"],
            app_secret=credentials["app_secret"],
            app_id=request.get("app_id"),
            app_public_id=request.get("app_public_id"),
            app_name=request.get("app_name"),
            verify_token=credentials["verify_token"],
            api_version=request.get("api_version", "v23.0"),
            instagram_login_mode=request.get("instagram_login_mode", "facebook_login"),
            page_id=request.get("page_id"),
            enable_dm=_request_bool(request, "enable_dm", default=True),
            enable_comments=_request_bool(request, "enable_comments", default=False),
            **common,
        )
    if job.platform == "whatsapp":
        from social_reply.application.account_management.whatsapp import connect_whatsapp_account

        return await connect_whatsapp_account(
            external_account_id=str(request["external_account_id"]),
            access_token=credentials["access_token"],
            app_secret=credentials["app_secret"],
            app_id=request.get("app_id"),
            app_public_id=request.get("app_public_id"),
            app_name=request.get("app_name"),
            verify_token=credentials["verify_token"],
            api_version=request.get("api_version", "v23.0"),
            **common,
        )
    if job.platform == "feishu":
        from social_reply.application.account_management.feishu import connect_feishu_account

        return await connect_feishu_account(
            app_id=str(request["app_id"]),
            app_secret=credentials["app_secret"],
            verification_token=credentials["verification_token"],
            encrypt_key=credentials["encrypt_key"],
            api_base_url=str(request.get("api_base_url") or FEISHU_API_BASE_URL),
            group_mode=str(request.get("group_mode") or FEISHU_GROUP_MODE),
            **common,
        )
    if job.platform == "email":
        from social_reply.application.account_management.email import connect_email_account

        return await connect_email_account(
            email_address=str(request["email_address"]),
            username=credentials["username"],
            password=credentials["password"],
            imap_host=str(request["imap_host"]),
            imap_port=int(request.get("imap_port", 993)),
            mailbox=str(request.get("mailbox") or "INBOX"),
            smtp_host=str(request["smtp_host"]),
            smtp_port=int(request.get("smtp_port", 465)),
            smtp_security=str(request.get("smtp_security") or "ssl"),
            from_name=request.get("from_name"),
            internal_domain_policy=str(request.get("internal_domain_policy") or "ignore"),
            **common,
        )
    return await connect_x_account(
        consumer_key=credentials["consumer_key"],
        consumer_secret=credentials["consumer_secret"],
        access_token=credentials["access_token"],
        access_token_secret=credentials["access_token_secret"],
        environment=str(request.get("environment") or "oauth"),
        xchat_pin=credentials.get("xchat_pin"),
        **common,
    )


async def process_provisioning_job(job_id: str) -> str:
    jid = uuid.UUID(job_id)
    job = await _claim_job(jid)
    if job is None:
        return "SKIPPED_NOT_CLAIMABLE"
    if job.status != "PROCESSING":
        await record_account_management_audit(
            tenant_id=job.tenant_id,
            actor=job.actor,
            action=(
                "provisioning_paused"
                if job.status == _PLATFORM_DISABLED_STATUS
                else "provisioning_failed"
            ),
            subject_id=str(jid),
            detail={
                "platform": job.platform,
                "error_code": job.last_error_code,
                "status": job.status,
            },
        )
        return job.status
    try:
        result = await _connect(job)
    except Exception as exc:  # noqa: BLE001 - platform boundary is normalized below
        disabled_platform = _disabled_platform(exc)
        if disabled_platform is not None:
            error_code = get_settings().platform_disabled_code(disabled_platform)
            async with get_session_factory()() as session:
                latest = await _lock_claimed_processing_job(session, job)
                if latest is None:
                    await session.rollback()
                    logger.warning(
                        "provisioning pause lost claim job_id=%s attempt=%s",
                        jid,
                        job.attempt_count,
                    )
                    return "STALE_CLAIM"
                disabled_values: dict[str, Any] = {
                    "status": _PLATFORM_DISABLED_STATUS,
                    "current_step": _PLATFORM_DISABLED_STATUS,
                    "attempt_count": latest.attempt_count - 1,
                    "next_attempt_at": None,
                    "locked_at": None,
                    "locked_by": None,
                    "last_error_code": error_code,
                    "last_error_message": "Platform integration is disabled",
                }
                if disabled_platform == "email":
                    disabled_values.update(
                        status="NEEDS_ACTION",
                        current_step="FAILED",
                        last_error_message=(
                            "Email integration is disabled; resubmit account credentials"
                        ),
                        **_email_terminal_values(latest.result, latest.staging_secret),
                    )
                updated = (
                    await session.execute(
                        update(models.ProvisioningJob)
                        .where(
                            models.ProvisioningJob.id == jid,
                            models.ProvisioningJob.status == "PROCESSING",
                            models.ProvisioningJob.attempt_count == latest.attempt_count,
                        )
                        .values(**disabled_values)
                        .returning(models.ProvisioningJob.id)
                    )
                ).first()
                await session.commit()
            if updated is None:
                logger.warning(
                    "provisioning pause lost claim job_id=%s attempt=%s",
                    jid,
                    job.attempt_count,
                )
                return "STALE_CLAIM"
            terminal_status = (
                "NEEDS_ACTION" if disabled_platform == "email" else _PLATFORM_DISABLED_STATUS
            )
            await record_account_management_audit(
                tenant_id=job.tenant_id,
                actor=job.actor,
                action=(
                    "provisioning_failed"
                    if terminal_status == "NEEDS_ACTION"
                    else "provisioning_paused"
                ),
                subject_id=str(jid),
                detail={
                    "platform": job.platform,
                    "error_code": error_code,
                    "status": terminal_status,
                },
            )
            return terminal_status
        error_code, message, retryable = _error(exc)
        async with get_session_factory()() as session:
            latest = await _lock_claimed_processing_job(session, job)
            if latest is None:
                await session.rollback()
                logger.warning(
                    "provisioning failure lost claim job_id=%s attempt=%s",
                    jid,
                    job.attempt_count,
                )
                return "STALE_CLAIM"

            staging_secret = latest.staging_secret
            failure_result = dict(latest.result or {})
            checkpoint = provisioning_checkpoint(latest)
            checkpoint_recovery = False
            try:
                submitted_secrets = (
                    decrypt_secret_bundle(latest.staging_secret) if latest.staging_secret else {}
                )
            except (TypeError, ValueError):
                submitted_secrets = None
            checkpoint_recovery = await _checkpoint_recovery_proof(
                session,
                latest,
                staged_secrets=(
                    submitted_secrets if isinstance(submitted_secrets, dict) else None
                ),
            )
            if latest.platform == "x":
                await _normalize_xchat_pin_state(
                    session,
                    latest,
                    staged_secrets=(
                        submitted_secrets if isinstance(submitted_secrets, dict) else None
                    ),
                    checkpoint_recovery=checkpoint_recovery,
                )
            staging_secret = latest.staging_secret
            failure_result = dict(latest.result or {})

            if requires_secret_resubmission(latest) or (
                checkpoint is not None and not checkpoint_recovery
            ):
                retryable = False
            next_attempt_at = None
            if retryable and latest.attempt_count >= _MAX_ATTEMPTS:
                retryable = False
                error_code = "RETRY_EXHAUSTED"
                message = "Provisioning retry limit exhausted"
            status = "FAILED" if retryable else "NEEDS_ACTION"
            if latest.platform == "email" and status == "NEEDS_ACTION":
                staging_secret = _email_resubmission_staging_secret(staging_secret)
                failure_result = _email_resubmission_result(failure_result)
            if retryable:
                delay = min(30 * 2 ** max(latest.attempt_count, 1), _MAX_BACKOFF_SECONDS)
                next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay)
            updated = (
                await session.execute(
                    update(models.ProvisioningJob)
                    .where(
                        models.ProvisioningJob.id == jid,
                        models.ProvisioningJob.status == "PROCESSING",
                        models.ProvisioningJob.attempt_count == latest.attempt_count,
                    )
                    .values(
                        status=status,
                        current_step=(
                            str(checkpoint.get("phase"))
                            if checkpoint_recovery and checkpoint is not None
                            else "FAILED"
                        ),
                        next_attempt_at=next_attempt_at,
                        locked_at=None,
                        locked_by=None,
                        last_error_code=error_code,
                        last_error_message=message,
                        staging_secret=staging_secret,
                        result=failure_result,
                    )
                    .returning(models.ProvisioningJob.id)
                )
            ).first()
            await session.commit()
        if updated is None:
            logger.warning(
                "provisioning failure lost claim job_id=%s attempt=%s",
                jid,
                latest.attempt_count,
            )
            return "STALE_CLAIM"
        await record_account_management_audit(
            tenant_id=latest.tenant_id,
            actor=latest.actor,
            action="provisioning_failed",
            subject_id=str(jid),
            detail={"platform": latest.platform, "error_code": error_code, "status": status},
        )
        return status
    payload = _result_payload(result)
    needs_action = not result.connection_ready
    merged_result: dict[str, Any] = dict(payload)
    checkpoint_phase = result.checkpoint_phase
    async with get_session_factory()() as session:
        latest = await _lock_claimed_processing_job(session, job)
        if latest is None:
            await session.rollback()
            logger.warning(
                "provisioning completion lost claim job_id=%s attempt=%s",
                jid,
                job.attempt_count,
            )
            return "STALE_CLAIM"
        checkpoint = provisioning_checkpoint(latest)
        if checkpoint is not None and checkpoint_matches_job(latest, checkpoint):
            account = await session.get(
                models.PlatformAccount,
                uuid.UUID(str(checkpoint["account_id"])),
                with_for_update=True,
            )
            if account is None:
                raise ValueError("provisioning_checkpoint_lost")
            output_version = checkpoint_output_version(checkpoint)
            try:
                current_credentials = decrypt_secret_bundle(account.credential_bundle)
            except (TypeError, ValueError):
                current_credentials = None
            observed_checkpoint = {
                **checkpoint,
                "observed_config_version": account.config_version,
                "applied_credentials_current": (
                    current_credentials is not None
                    and credential_fingerprint_matches(
                        current_credentials,
                        checkpoint.get("credential_fingerprint"),
                    )
                ),
                "superseded_by_later_config": account.config_version > output_version,
            }
            checkpoint_phase = observed_checkpoint.get("phase")
            merged_result["checkpoint"] = observed_checkpoint
            merged_result["observed_config_version"] = account.config_version
            merged_result["configuration_changed_after_provisioning"] = (
                account.config_version > output_version
            )
        pin_checkpoint_recovery = False
        if latest.platform == "x":
            try:
                completion_staged_secrets = (
                    decrypt_secret_bundle(latest.staging_secret)
                    if latest.staging_secret
                    else {}
                )
            except (TypeError, ValueError):
                completion_staged_secrets = None
            pin_checkpoint_recovery = await _checkpoint_recovery_proof(
                session,
                latest,
                staged_secrets=(
                    completion_staged_secrets
                    if isinstance(completion_staged_secrets, dict)
                    else None
                ),
            )
            await _normalize_xchat_pin_state(
                session,
                latest,
                staged_secrets=(
                    completion_staged_secrets
                    if isinstance(completion_staged_secrets, dict)
                    else None
                ),
                checkpoint_recovery=pin_checkpoint_recovery,
                mark_missing_pin_on_failure=(
                    latest.operation == "REAUTHORIZE" and checkpoint is not None
                ),
            )
        values: dict[str, Any] = {
            "status": "NEEDS_ACTION" if needs_action else "COMPLETED",
            "current_step": checkpoint_phase or ("NEEDS_ACTION" if needs_action else "COMPLETED"),
            "account_id": result.account_id,
            "platform_app_id": result.platform_app_id,
            "result": {**dict(latest.result or {}), **merged_result},
            "locked_at": None,
            "locked_by": None,
            "next_attempt_at": None,
            "completed_at": None if needs_action else datetime.now(UTC),
            "staging_secret": (
                _email_resubmission_staging_secret(latest.staging_secret)
                if needs_action and latest.platform == "email"
                else latest.staging_secret
                if needs_action
                else None
            ),
            "last_error_code": "PROVISIONING_NEEDS_ACTION" if needs_action else None,
            "last_error_message": (
                "Credentials were applied; provider or manual follow-up is still required"
                if needs_action
                else None
            ),
        }
        updated = (
            await session.execute(
                update(models.ProvisioningJob)
                .where(
                    models.ProvisioningJob.id == jid,
                    models.ProvisioningJob.status == "PROCESSING",
                    models.ProvisioningJob.attempt_count == job.attempt_count,
                )
                .values(**values)
                .returning(models.ProvisioningJob.id)
            )
        ).first()
        await session.commit()
    if updated is None:
        logger.warning(
            "provisioning completion lost claim job_id=%s attempt=%s",
            jid,
            job.attempt_count,
        )
        return "STALE_CLAIM"
    await record_account_management_audit(
        tenant_id=job.tenant_id,
        actor=job.actor,
        action="provisioning_needs_action" if needs_action else "provisioning_completed",
        subject_id=str(result.account_id),
        detail={
            "platform": job.platform,
            "job_id": str(jid),
            "public_id": result.public_id,
            "connection_status": result.connection_status,
            "checkpoint_phase": checkpoint_phase,
        },
    )
    return "NEEDS_ACTION" if needs_action else "COMPLETED"


def requires_secret_resubmission(job: models.ProvisioningJob) -> bool:
    return bool((job.result or {}).get("requires_secret_resubmission"))


def provisioning_job_is_in_flight(
    job: models.ProvisioningJob,
    *,
    now: datetime | None = None,
) -> bool:
    if job.status in {"PENDING", "PROCESSING"}:
        return True
    if job.status != "FAILED" or job.next_attempt_at is None:
        return False
    current = now or datetime.now(UTC)
    return job.next_attempt_at >= current - _RETRY_DISPLAY_GRACE


async def _retry_provisioning_job_transaction(
    job_id: uuid.UUID,
    *,
    tenant_id: str | None,
    caller: Principal | None,
    control_actor: str | None = None,
    control_token: object | None = None,
) -> None:
    control_retry = control_token is _CONTROL_API_RETRY_TOKEN
    if control_retry and caller is not None:
        raise PermissionError("provisioning_authority_invalid")
    if not control_retry and caller is None:
        raise PermissionError("retry_caller_required")

    async with get_session_factory()() as session:
        initial_statement = select(models.ProvisioningJob).where(
            models.ProvisioningJob.id == job_id
        )
        if tenant_id is not None:
            initial_statement = initial_statement.where(
                models.ProvisioningJob.tenant_id == tenant_id
            )
        initial = (await session.execute(initial_statement)).scalar_one_or_none()
        if initial is None:
            raise LookupError("provisioning_job_not_found")
        initial_authority = (
            initial.authority_kind,
            initial.authority_version,
            initial.initiator_user_id,
            initial.initiator_session_id,
        )
        if control_retry and initial.authority_kind != "CONTROL_API":
            raise PermissionError("provisioning_authority_invalid")

        caller_before = None
        caller_session_uuid = None
        if not control_retry:
            assert caller is not None
            if caller.session_id is None:
                raise PermissionError("admin_session_invalid")
            caller_session_uuid = uuid.UUID(str(caller.session_id))
            caller_before = await principal_from_session_row(
                session, caller_session_uuid, for_update=False
            )
            if not _principal_is_current_for_tenant(caller_before, initial.tenant_id):
                raise PermissionError("admin_session_invalid")

        staff_ids = {
            user_id
            for user_id in (
                initial.initiator_user_id,
                caller.user_id if caller is not None else None,
                caller_before.user_id if caller_before is not None else None,
            )
            if user_id is not None
        }
        session_ids = {
            session_id
            for session_id in (
                initial.initiator_session_id,
                caller_session_uuid,
            )
            if session_id is not None
        }
        await _lock_staff_and_sessions(
            session,
            staff_ids=staff_ids,
            session_ids=session_ids,
        )

        current_caller = None
        if not control_retry:
            assert caller is not None and caller_session_uuid is not None
            current_caller = await principal_from_session_row(
                session, caller_session_uuid, for_update=False
            )
            if (
                caller_before is None
                or current_caller is None
                or not _principal_is_current_for_tenant(current_caller, initial.tenant_id)
                or _principal_snapshot(caller) != _principal_snapshot(current_caller)
                or _principal_snapshot(caller_before) != _principal_snapshot(current_caller)
            ):
                raise PermissionError("admin_session_invalid")

        job = (
            await session.execute(
                select(models.ProvisioningJob)
                .where(models.ProvisioningJob.id == job_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if job is None or (tenant_id is not None and job.tenant_id != tenant_id):
            raise LookupError("provisioning_job_not_found")
        if initial_authority != (
            job.authority_kind,
            job.authority_version,
            job.initiator_user_id,
            job.initiator_session_id,
        ):
            raise PermissionError("provisioning_authority_invalid")
        if not control_retry:
            assert current_caller is not None
            if not current_caller.is_workspace_admin and (
                current_caller.user_id is None
                or job.owner_user_id != current_caller.user_id
            ):
                raise LookupError("provisioning_job_not_found")
            validate_owner_provisioning_policy(
                platform=job.platform,
                owner_user_id=(
                    None if current_caller.is_workspace_admin else current_caller.user_id
                ),
                request=dict(job.request or {}),
                is_workspace_admin=current_caller.is_workspace_admin,
            )
        await _validate_job_authority(
            session, job, authority_locks_held=True
        )
        checkpoint_recovery = await _checkpoint_recovery_proof(session, job)
        if job.platform == "x":
            checkpoint_recovery = await _normalize_xchat_pin_state(
                session,
                job,
                checkpoint_recovery=checkpoint_recovery,
            )
        if job.status not in {"FAILED", "NEEDS_ACTION"}:
            raise ValueError("provisioning_job_not_retryable")
        if requires_secret_resubmission(job):
            raise ValueError("provisioning_secret_resubmission_required")
        if not get_settings().platform_integration_enabled(job.platform):
            raise ValueError(f"{job.platform}_integration_disabled")

        previous_status = job.status
        previous_attempt_count = job.attempt_count
        if job.attempt_count >= _MAX_ATTEMPTS:
            job.attempt_count = 0
        job.status = "PENDING"
        job.current_step = "QUEUED"
        job.next_attempt_at = None
        job.last_error_code = None
        job.last_error_message = None
        audit_actor = control_actor if control_retry else current_caller.actor
        session.add(
            models.AuditLog(
                tenant_id=job.tenant_id,
                category="account_management",
                actor=audit_actor or "service:control_api",
                action="RETRY_PROVISIONING_JOB",
                subject_type="provisioning_job",
                subject_id=str(job.id),
                detail={
                    "platform": job.platform,
                    "previous_status": previous_status,
                    "previous_attempt_count": previous_attempt_count,
                    "status": "PENDING",
                },
            )
        )
        await session.commit()


async def retry_provisioning_job(
    job_id: uuid.UUID,
    *,
    tenant_id: str | None = None,
    caller: Principal | None = None,
    actor: str | None = None,
) -> None:
    """Retry a staff-owned job using the authenticated caller Principal.

    ``actor`` is retained only to fail closed for stale callers during the
    parent-agent UI migration; it can never grant authority or choose an owner.
    """
    if caller is None:
        raise PermissionError("retry_caller_required")
    if actor is not None and actor != caller.actor:
        raise PermissionError("retry_caller_mismatch")
    await _retry_provisioning_job_transaction(
        job_id,
        tenant_id=tenant_id,
        caller=caller,
    )


async def retry_control_provisioning_job(
    job_id: uuid.UUID,
    *,
    tenant_id: str,
    actor: str = "service:control_api",
) -> None:
    """Retry a Control API job through an explicit machine-authority proof."""
    await _retry_provisioning_job_transaction(
        job_id,
        tenant_id=tenant_id,
        caller=None,
        control_actor=actor,
        control_token=_CONTROL_API_RETRY_TOKEN,
    )


def _sweep_job_columns():
    return (
        models.ProvisioningJob.id,
        models.ProvisioningJob.status,
        models.ProvisioningJob.locked_at,
        models.ProvisioningJob.next_attempt_at,
        models.ProvisioningJob.attempt_count,
        models.ProvisioningJob.authority_kind,
        models.ProvisioningJob.authority_version,
        models.ProvisioningJob.initiator_user_id,
        models.ProvisioningJob.initiator_session_id,
    )


def _sweep_candidate_snapshot(row: Any, *, kind: str) -> dict[str, Any]:
    return {
        "id": row["id"],
        "kind": kind,
        "status": row["status"],
        "locked_at": row["locked_at"],
        "next_attempt_at": row["next_attempt_at"],
        "attempt_count": row["attempt_count"],
        "authority": (
            row["authority_kind"],
            row["authority_version"],
            row["initiator_user_id"],
            row["initiator_session_id"],
        ),
    }


def _sweep_snapshot_matches_mapping(row: Any, snapshot: dict[str, Any]) -> bool:
    return (
        row["status"] == snapshot["status"]
        and row["locked_at"] == snapshot["locked_at"]
        and row["next_attempt_at"] == snapshot["next_attempt_at"]
        and row["attempt_count"] == snapshot["attempt_count"]
        and (
            row["authority_kind"],
            row["authority_version"],
            row["initiator_user_id"],
            row["initiator_session_id"],
        )
        == snapshot["authority"]
    )


def _sweep_snapshot_matches_job(
    job: models.ProvisioningJob, snapshot: dict[str, Any]
) -> bool:
    return (
        job.status == snapshot["status"]
        and job.locked_at == snapshot["locked_at"]
        and job.next_attempt_at == snapshot["next_attempt_at"]
        and job.attempt_count == snapshot["attempt_count"]
        and _job_authority_snapshot(job) == snapshot["authority"]
    )


def _sweep_authority_shape_valid(job: models.ProvisioningJob) -> bool:
    if job.authority_kind not in _AUTHORITY_KINDS or job.authority_version != _AUTHORITY_VERSION:
        return False
    try:
        marker_values = decrypt_secret_bundle(job.staging_secret) if job.staging_secret else {}
    except (TypeError, ValueError):
        return False
    has_marker = marker_values.get(_CONTROL_API_SECRET_KEY) == _CONTROL_API_SECRET_VALUE
    return (
        job.authority_kind == "CONTROL_API"
        and job.operation == "CONNECT_ACCOUNT"
        and job.initiator_user_id is None
        and job.initiator_session_id is None
        and has_marker
    ) or (
        job.authority_kind != "CONTROL_API"
        and job.initiator_session_id is not None
        and not has_marker
    )


def _mark_sweep_authority_invalid(
    job: models.ProvisioningJob,
    *,
    current_step: str,
    message: str,
) -> None:
    job.status = "NEEDS_ACTION"
    job.current_step = current_step
    job.next_attempt_at = None
    job.locked_at = None
    job.locked_by = None
    job.last_error_code = "PROVISIONING_AUTHORITY_INVALID"
    job.last_error_message = message


async def _sweep_provisioning_job_candidate(
    snapshot: dict[str, Any],
    *,
    settings,
    now: datetime,
) -> None:
    async with get_session_factory()() as session:
        identity = (
            await session.execute(
                select(*_sweep_job_columns()).where(
                    models.ProvisioningJob.id == snapshot["id"]
                )
            )
        ).mappings().one_or_none()
        if identity is None or not _sweep_snapshot_matches_mapping(identity, snapshot):
            await session.rollback()
            return

        staff_ids = {
            user_id
            for user_id in (identity["initiator_user_id"],)
            if user_id is not None
        }
        session_ids = {
            session_id
            for session_id in (identity["initiator_session_id"],)
            if session_id is not None
        }
        await _lock_staff_and_sessions(
            session,
            staff_ids=staff_ids,
            session_ids=session_ids,
        )
        job = (
            await session.execute(
                select(models.ProvisioningJob)
                .where(models.ProvisioningJob.id == snapshot["id"])
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if job is None or not _sweep_snapshot_matches_job(job, snapshot):
            await session.rollback()
            return

        is_stale = snapshot["kind"] == "stale"
        checkpoint = provisioning_checkpoint(job) if is_stale else None
        checkpoint_recovery = False
        authority_step = (
            str(checkpoint.get("phase"))
            if checkpoint is not None
            else "FAILED"
        )
        if not _sweep_authority_shape_valid(job):
            _mark_sweep_authority_invalid(
                job,
                current_step=authority_step,
                message="Provisioning authority is missing or expired",
            )
            await session.commit()
            return
        try:
            await _validate_job_authority(
                session,
                job,
                validate_target=False,
                authority_locks_held=True,
            )
        except (LookupError, PermissionError, ValueError):
            _mark_sweep_authority_invalid(
                job,
                current_step=authority_step,
                message="Provisioning authority is missing, expired, or changed",
            )
            await session.commit()
            return

        if not is_stale:
            if job.platform == "email":
                _mark_email_needs_action(
                    job,
                    error_code=settings.platform_disabled_code("email") or "EMAIL_DISABLED",
                    message="Email integration was disabled; resubmit account credentials",
                )
            elif not settings.platform_integration_enabled(job.platform):
                await session.commit()
                return
            else:
                job.status = "PENDING"
                job.current_step = "QUEUED"
                job.next_attempt_at = None
                job.last_error_code = None
                job.last_error_message = None
            await session.commit()
            return

        if not settings.platform_integration_enabled(job.platform):
            error_code = settings.platform_disabled_code(job.platform) or "PLATFORM_DISABLED"
            if job.platform == "email":
                _mark_email_needs_action(
                    job,
                    error_code=error_code,
                    message="Email integration was disabled; resubmit account credentials",
                )
            else:
                job.status = _PLATFORM_DISABLED_STATUS
                job.current_step = _PLATFORM_DISABLED_STATUS
                job.next_attempt_at = None
                job.locked_at = None
                job.locked_by = None
                job.last_error_code = error_code
                job.last_error_message = "Platform integration is disabled"
            await session.commit()
            return

        try:
            submitted_secrets = (
                decrypt_secret_bundle(job.staging_secret) if job.staging_secret else {}
            )
        except (TypeError, ValueError):
            _mark_sweep_authority_invalid(
                job,
                current_step="FAILED",
                message="Provisioning staging secret is unreadable",
            )
            await session.commit()
            return
        checkpoint_recovery = await _checkpoint_recovery_proof(
            session,
            job,
            staged_secrets=submitted_secrets,
        )
        if job.platform == "x":
            checkpoint_recovery = await _normalize_xchat_pin_state(
                session,
                job,
                staged_secrets=submitted_secrets,
                checkpoint_recovery=checkpoint_recovery,
            )

        if requires_secret_resubmission(job):
            job.status = "NEEDS_ACTION"
            job.next_attempt_at = None
            if _xchat_resubmission_marked(job):
                job.last_error_code = "STALE_PROCESSING_SECRET_RESUBMISSION_REQUIRED"
                job.last_error_message = (
                    "Stale XChat provisioning requires the PIN to be resubmitted"
                )
            else:
                job.last_error_code = job.last_error_code or "STALE_PROCESSING_NEEDS_ACTION"
                job.last_error_message = job.last_error_message or (
                    "Stale provisioning requires operator action"
                )
        elif checkpoint is not None and not checkpoint_recovery:
            job.status = "NEEDS_ACTION"
            job.next_attempt_at = None
            job.current_step = "FAILED"
            job.last_error_code = "STALE_PROCESSING_CHECKPOINT_INVALID"
            job.last_error_message = "Stale provisioning checkpoint could not be proven"
        elif job.attempt_count >= _MAX_ATTEMPTS:
            if job.platform == "email":
                _mark_email_needs_action(
                    job,
                    error_code="RETRY_EXHAUSTED",
                    message="Provisioning retry limit exhausted",
                )
            else:
                job.status = "NEEDS_ACTION"
                job.next_attempt_at = None
                job.last_error_code = "RETRY_EXHAUSTED"
                job.last_error_message = "Provisioning retry limit exhausted"
        elif checkpoint_recovery:
            job.status = "FAILED"
            job.next_attempt_at = now
            job.last_error_code = "STALE_PROCESSING"
            job.last_error_message = "Stale provisioning job recovered by scheduler"
        elif job.last_error_code:
            job.status = "NEEDS_ACTION"
            job.next_attempt_at = None
        else:
            job.status = "FAILED"
            job.next_attempt_at = now
            job.last_error_code = "STALE_PROCESSING"
            job.last_error_message = "Stale provisioning job recovered by scheduler"
        if not checkpoint_recovery:
            job.current_step = "FAILED"
        job.locked_at = None
        job.locked_by = None
        await session.commit()


async def sweep_provisioning_jobs() -> list[uuid.UUID]:
    now = datetime.now(UTC)
    stale_before = now - _STALE_AFTER
    settings = get_settings()
    async with get_session_factory()() as session:
        paused_rows = (
            await session.execute(
                select(*_sweep_job_columns())
                .where(models.ProvisioningJob.status == _PLATFORM_DISABLED_STATUS)
                .order_by(models.ProvisioningJob.id)
            )
        ).mappings().all()
        stale_rows = (
            await session.execute(
                select(*_sweep_job_columns())
                .where(
                    models.ProvisioningJob.status == "PROCESSING",
                    models.ProvisioningJob.locked_at < stale_before,
                )
                .order_by(models.ProvisioningJob.id)
            )
        ).mappings().all()
        await session.rollback()

    candidates = [
        *(_sweep_candidate_snapshot(row, kind="paused") for row in paused_rows),
        *(_sweep_candidate_snapshot(row, kind="stale") for row in stale_rows),
    ]
    for candidate in candidates:
        await _sweep_provisioning_job_candidate(
            candidate,
            settings=settings,
            now=now,
        )

    async with get_session_factory()() as session:
        exhausted_jobs = list(
            (
                await session.execute(
                    select(models.ProvisioningJob)
                    .where(
                        models.ProvisioningJob.status.in_(["PENDING", "FAILED"]),
                        models.ProvisioningJob.attempt_count >= _MAX_ATTEMPTS,
                    )
                    .with_for_update(skip_locked=True)
                )
            ).scalars()
        )
        for job in exhausted_jobs:
            if job.platform == "email":
                _mark_email_needs_action(
                    job,
                    error_code="RETRY_EXHAUSTED",
                    message="Provisioning retry limit exhausted",
                )
            else:
                job.status = "NEEDS_ACTION"
                job.current_step = "FAILED"
                job.next_attempt_at = None
                job.locked_at = None
                job.locked_by = None
                job.last_error_code = "RETRY_EXHAUSTED"
                job.last_error_message = "Provisioning retry limit exhausted"
        rows = list(
            (
                await session.execute(
                    select(models.ProvisioningJob.id).where(
                        models.ProvisioningJob.status.in_(["PENDING", "FAILED"]),
                        models.ProvisioningJob.attempt_count < _MAX_ATTEMPTS,
                        or_(
                            models.ProvisioningJob.next_attempt_at.is_(None),
                            models.ProvisioningJob.next_attempt_at <= now,
                        ),
                    )
                )
            ).scalars()
        )
        await session.commit()
    from social_reply.application.account_management.actors import process_platform_provisioning

    dispatched: list[uuid.UUID] = []
    for pending_id in rows:
        try:
            await dispatch_actor(process_platform_provisioning, str(pending_id))
        except Exception:  # noqa: BLE001 - the durable row remains eligible for recovery
            logger.exception("provisioning dispatch failed job_id=%s", pending_id)
        else:
            dispatched.append(pending_id)
    return dispatched


def _public_result(value: Any) -> Any:
    sensitive = {
        "verify_token",
        "token",
        "access_token",
        "access_token_secret",
        "app_secret",
        "verification_token",
        "encrypt_key",
        "consumer_key",
        "consumer_secret",
        "xchat_pin",
        "xchat_private_keys_b64",
        "xchat_signing_key_version",
        "username",
        "password",
        "credential_fingerprint",
        "bound_credential_fingerprint",
        PRIVATE_INPUT_FINGERPRINT_KEY,
        PRIVATE_INPUT_FINGERPRINT_WITHOUT_REQUIRED_KEY,
    }
    if isinstance(value, dict):
        return {
            key: _public_result(item)
            for key, item in value.items()
            if key not in sensitive and not key.startswith("__")
        }
    if isinstance(value, list):
        return [_public_result(item) for item in value]
    return value


def public_job(job: models.ProvisioningJob) -> dict[str, Any]:
    return {
        "id": str(job.id),
        "tenant_id": job.tenant_id,
        "brand_id": job.brand_id,
        "platform": job.platform,
        "operation": job.operation,
        "status": job.status,
        "current_step": job.current_step,
        "attempt_count": job.attempt_count,
        "account_id": str(job.account_id) if job.account_id else None,
        "platform_app_id": str(job.platform_app_id) if job.platform_app_id else None,
        "result": _public_result(dict(job.result or {})),
        "last_error_code": job.last_error_code,
        # Provider diagnostics remain server-side. Each HTTP surface maps the
        # stable error code to approved operator copy for its audience.
        "last_error_message": None,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }
