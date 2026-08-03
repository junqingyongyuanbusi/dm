import logging
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from social_reply.application.account_management.audit import record_account_management_audit
from social_reply.application.account_management.auth import principal_from_session_row
from social_reply.application.account_management.service import (
    AccountConnectionResult,
    connect_meta_account,
    connect_telegram_account,
    connect_x_account,
)
from social_reply.application.account_management.submissions import split_submission
from social_reply.application.account_management.xchat_activation import XChatActivationError
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


def _idempotency_key(tenant_id: str, platform: str, request: dict[str, Any]) -> str:
    explicit = request.get("idempotency_key")
    if explicit:
        return str(explicit)
    return uuid.uuid4().hex


def _safe_request(platform: str, request: dict[str, Any]) -> dict[str, Any]:
    public, _secrets = split_submission(platform, request)
    return public


def _error(exc: Exception) -> tuple[str, str, bool]:
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


async def submit_provisioning_job(
    *,
    tenant_id: str,
    brand_id: str,
    platform: str,
    actor: str,
    request: dict[str, Any],
    secrets: dict[str, str],
    admin_session_id: uuid.UUID | str | None = None,
) -> uuid.UUID:
    if platform not in PROVISIONABLE_ACCOUNT_PLATFORMS:
        raise ValueError(f"unsupported_platform:{platform}")
    if not tenant_id or not all(ch.isalnum() or ch in {"_", "-"} for ch in tenant_id):
        raise ValueError("invalid_tenant_id")
    if not brand_id or not all(ch.isalnum() or ch in {"_", "-"} for ch in brand_id):
        raise ValueError("invalid_brand_id")
    key = _idempotency_key(tenant_id, platform, request)
    job_id = uuid.uuid4()
    # Secret 内联暂存进 provisioning_jobs 行；job 完成后置 NULL（见 process_provisioning_job）
    safe_request = _safe_request(platform, request)
    async with get_session_factory()() as session:
        if admin_session_id is not None:
            principal = await principal_from_session_row(session, admin_session_id, for_update=True)
            if principal is None or tenant_id not in principal.allowed_tenants:
                raise PermissionError("admin_session_invalid")
            actor = principal.actor
        inserted = (
            await session.execute(
                pg_insert(models.ProvisioningJob)
                .values(
                    id=job_id,
                    tenant_id=tenant_id,
                    brand_id=brand_id,
                    platform=platform,
                    operation="CONNECT_ACCOUNT",
                    actor=actor,
                    idempotency_key=key,
                    request=safe_request,
                    staging_secret=encrypt_secret_bundle(secrets),
                    status="PENDING",
                    current_step="QUEUED",
                )
                .on_conflict_do_nothing(index_elements=["tenant_id", "idempotency_key"])
                .returning(models.ProvisioningJob.id)
            )
        ).scalar_one_or_none()
        existing_job = None
        if inserted is None:
            existing = (
                await session.execute(
                    select(models.ProvisioningJob).where(
                        models.ProvisioningJob.tenant_id == tenant_id,
                        models.ProvisioningJob.idempotency_key == key,
                    )
                )
            ).scalar_one()
            if (
                existing.platform != platform
                or existing.brand_id != brand_id
                or dict(existing.request or {}) != safe_request
            ):
                raise ValueError("idempotency_key_payload_mismatch")
            job_id = existing.id
            existing_job = existing
        await session.commit()
    if (
        inserted is None
        and existing_job is not None
        and existing_job.status
        in {
            "FAILED",
            "NEEDS_ACTION",
        }
    ):
        if requires_secret_resubmission(existing_job):
            required_secret = str((existing_job.result or {}).get("required_secret") or "")
            supplied_secret = secrets.get(required_secret) if required_secret else None
            if not isinstance(supplied_secret, str) or not supplied_secret.strip():
                raise ValueError("provisioning_secret_resubmission_required")
        async with get_session_factory()() as session:
            await session.execute(
                update(models.ProvisioningJob)
                .where(models.ProvisioningJob.id == job_id)
                .values(
                    staging_secret=encrypt_secret_bundle(secrets),
                    status="PENDING",
                    current_step="QUEUED",
                    next_attempt_at=None,
                    result={},
                    attempt_count=(
                        0
                        if existing_job.attempt_count >= _MAX_ATTEMPTS
                        else existing_job.attempt_count
                    ),
                    last_error_code=None,
                    last_error_message=None,
                )
            )
            await session.commit()
    await record_account_management_audit(
        tenant_id=tenant_id,
        actor=actor,
        action="provisioning_submitted",
        subject_id=str(job_id),
        detail={"platform": platform, "brand_id": brand_id},
    )
    return job_id


async def _claim_job(job_id: uuid.UUID) -> models.ProvisioningJob | None:
    now = datetime.now(UTC)
    async with get_session_factory()() as session:
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
            )
        ).scalar_one_or_none()
        if row is None:
            await session.commit()
            return None
        settings = get_settings()
        if not settings.platform_integration_enabled(row.platform):
            row.status = _PLATFORM_DISABLED_STATUS
            row.current_step = _PLATFORM_DISABLED_STATUS
            row.next_attempt_at = None
            row.locked_at = None
            row.locked_by = None
            row.last_error_code = settings.platform_disabled_code(row.platform)
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
        "automation_default": result.automation_default,
        "platform_app_id": str(result.platform_app_id) if result.platform_app_id else None,
        "app_public_id": result.app_public_id,
        "pending_update_count": result.pending_update_count,
        "last_webhook_error": result.last_webhook_error,
        "bot_name": result.bot_name,
        "bot_status": result.bot_status,
        "callback_url": result.webhook_url if result.platform == "feishu" else None,
        "manual_steps": list(result.manual_steps),
    }


async def _connect(job: models.ProvisioningJob) -> AccountConnectionResult:
    settings = get_settings()
    if not settings.platform_integration_enabled(job.platform):
        raise ValueError(f"{job.platform}_integration_disabled")
    request = dict(job.request or {})
    credentials = decrypt_secret_bundle(job.staging_secret)
    common = {
        "public_base_url": settings.public_base_url,
        "tenant_id": job.tenant_id,
        "brand_id": job.brand_id,
        "name": request.get("name"),
        "public_id": request.get("public_id"),
        "secrets_root": Path(settings.account_secrets_root),
        "automation_default": request.get("automation_default", "BOT_DRAFT_ONLY"),
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
    if job.status == _PLATFORM_DISABLED_STATUS:
        await record_account_management_audit(
            tenant_id=job.tenant_id,
            actor=job.actor,
            action="provisioning_paused",
            subject_id=str(jid),
            detail={"platform": job.platform, "error_code": job.last_error_code},
        )
        return _PLATFORM_DISABLED_STATUS
    try:
        result = await _connect(job)
    except Exception as exc:  # noqa: BLE001 - platform boundary is normalized below
        disabled_platform = _disabled_platform(exc)
        if disabled_platform is not None:
            error_code = get_settings().platform_disabled_code(disabled_platform)
            async with get_session_factory()() as session:
                updated = (
                    await session.execute(
                        update(models.ProvisioningJob)
                        .where(
                            models.ProvisioningJob.id == jid,
                            models.ProvisioningJob.status == "PROCESSING",
                            models.ProvisioningJob.attempt_count == job.attempt_count,
                        )
                        .values(
                            status=_PLATFORM_DISABLED_STATUS,
                            current_step=_PLATFORM_DISABLED_STATUS,
                            attempt_count=models.ProvisioningJob.attempt_count - 1,
                            next_attempt_at=None,
                            locked_at=None,
                            locked_by=None,
                            last_error_code=error_code,
                            last_error_message="Platform integration is disabled",
                        )
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
            await record_account_management_audit(
                tenant_id=job.tenant_id,
                actor=job.actor,
                action="provisioning_paused",
                subject_id=str(jid),
                detail={"platform": job.platform, "error_code": error_code},
            )
            return _PLATFORM_DISABLED_STATUS
        error_code, message, retryable = _error(exc)
        staging_secret = job.staging_secret
        failure_result = dict(job.result or {})
        if job.platform == "x":
            submitted_secrets = decrypt_secret_bundle(job.staging_secret)
            if submitted_secrets.get("xchat_pin"):
                # A PIN is a one-time unlock input. Never retain it or retry without it.
                submitted_secrets.pop("xchat_pin", None)
                staging_secret = encrypt_secret_bundle(submitted_secrets)
                retryable = False
                failure_result = {
                    **failure_result,
                    "requires_secret_resubmission": True,
                    "required_secret": "xchat_pin",
                }
        next_attempt_at = None
        if retryable and job.attempt_count >= _MAX_ATTEMPTS:
            retryable = False
            error_code = "RETRY_EXHAUSTED"
            message = "Provisioning retry limit exhausted"
        status = "FAILED" if retryable else "NEEDS_ACTION"
        if retryable:
            delay = min(30 * 2 ** max(job.attempt_count, 1), _MAX_BACKOFF_SECONDS)
            next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay)
        async with get_session_factory()() as session:
            updated = (
                await session.execute(
                    update(models.ProvisioningJob)
                    .where(
                        models.ProvisioningJob.id == jid,
                        models.ProvisioningJob.status == "PROCESSING",
                        models.ProvisioningJob.attempt_count == job.attempt_count,
                    )
                    .values(
                        status=status,
                        current_step="FAILED",
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
                job.attempt_count,
            )
            return "STALE_CLAIM"
        await record_account_management_audit(
            tenant_id=job.tenant_id,
            actor=job.actor,
            action="provisioning_failed",
            subject_id=str(jid),
            detail={"platform": job.platform, "error_code": error_code, "status": status},
        )
        return status

    payload = _result_payload(result)
    async with get_session_factory()() as session:
        updated = (
            await session.execute(
                update(models.ProvisioningJob)
                .where(
                    models.ProvisioningJob.id == jid,
                    models.ProvisioningJob.status == "PROCESSING",
                    models.ProvisioningJob.attempt_count == job.attempt_count,
                )
                .values(
                    status="COMPLETED",
                    current_step="COMPLETED",
                    account_id=result.account_id,
                    platform_app_id=result.platform_app_id,
                    result=payload,
                    locked_at=None,
                    locked_by=None,
                    next_attempt_at=None,
                    completed_at=datetime.now(UTC),
                    # 连接完成即清除内联暂存 secret，与状态更新同事务原子完成
                    staging_secret=None,
                )
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
        action="provisioning_completed",
        subject_id=str(result.account_id),
        detail={"platform": job.platform, "job_id": str(jid), "public_id": result.public_id},
    )
    return "COMPLETED"


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


async def retry_provisioning_job(job_id: uuid.UUID) -> None:
    async with get_session_factory()() as session:
        job = (
            await session.execute(
                select(models.ProvisioningJob)
                .where(models.ProvisioningJob.id == job_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if job is None or job.status not in {"FAILED", "NEEDS_ACTION"}:
            raise ValueError("provisioning_job_not_retryable")
        if requires_secret_resubmission(job):
            raise ValueError("provisioning_secret_resubmission_required")
        if job.attempt_count >= _MAX_ATTEMPTS:
            job.attempt_count = 0
        job.status = "PENDING"
        job.current_step = "QUEUED"
        job.next_attempt_at = None
        job.last_error_code = None
        job.last_error_message = None
        await session.commit()


async def sweep_provisioning_jobs() -> list[uuid.UUID]:
    now = datetime.now(UTC)
    stale_before = now - _STALE_AFTER
    settings = get_settings()
    async with get_session_factory()() as session:
        paused_jobs = list(
            (
                await session.execute(
                    select(models.ProvisioningJob)
                    .where(models.ProvisioningJob.status == _PLATFORM_DISABLED_STATUS)
                    .with_for_update(skip_locked=True)
                )
            ).scalars()
        )
        for job in paused_jobs:
            if not settings.platform_integration_enabled(job.platform):
                continue
            job.status = "PENDING"
            job.current_step = "QUEUED"
            job.next_attempt_at = None
            job.last_error_code = None
            job.last_error_message = None
        stale_jobs = list(
            (
                await session.execute(
                    select(models.ProvisioningJob)
                    .where(
                        models.ProvisioningJob.status == "PROCESSING",
                        models.ProvisioningJob.locked_at < stale_before,
                    )
                    .with_for_update(skip_locked=True)
                )
            ).scalars()
        )
        for job in stale_jobs:
            if not settings.platform_integration_enabled(job.platform):
                job.status = _PLATFORM_DISABLED_STATUS
                job.current_step = _PLATFORM_DISABLED_STATUS
                job.next_attempt_at = None
                job.locked_at = None
                job.locked_by = None
                job.last_error_code = settings.platform_disabled_code(job.platform)
                job.last_error_message = "Platform integration is disabled"
                continue
            submitted_secrets = (
                decrypt_secret_bundle(job.staging_secret) if job.staging_secret else {}
            )
            if job.platform == "x" and submitted_secrets.get("xchat_pin"):
                submitted_secrets.pop("xchat_pin", None)
                job.staging_secret = encrypt_secret_bundle(submitted_secrets)
                job.status = "NEEDS_ACTION"
                job.next_attempt_at = None
                job.result = {
                    **dict(job.result or {}),
                    "requires_secret_resubmission": True,
                    "required_secret": "xchat_pin",
                }
                job.last_error_code = "STALE_PROCESSING_SECRET_RESUBMISSION_REQUIRED"
                job.last_error_message = (
                    "Stale XChat provisioning requires the PIN to be resubmitted"
                )
            elif job.attempt_count >= _MAX_ATTEMPTS:
                job.status = "NEEDS_ACTION"
                job.next_attempt_at = None
                job.last_error_code = "RETRY_EXHAUSTED"
                job.last_error_message = "Provisioning retry limit exhausted"
            else:
                job.status = "FAILED"
                job.next_attempt_at = now
                job.last_error_code = "STALE_PROCESSING"
                job.last_error_message = "Stale provisioning job recovered by scheduler"
            job.current_step = "FAILED"
            job.locked_at = None
            job.locked_by = None
        await session.execute(
            update(models.ProvisioningJob)
            .where(
                models.ProvisioningJob.status.in_(["PENDING", "FAILED"]),
                models.ProvisioningJob.attempt_count >= _MAX_ATTEMPTS,
            )
            .values(
                status="NEEDS_ACTION",
                current_step="FAILED",
                next_attempt_at=None,
                locked_at=None,
                locked_by=None,
                last_error_code="RETRY_EXHAUSTED",
                last_error_message="Provisioning retry limit exhausted",
            )
        )
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
    }
    if isinstance(value, dict):
        return {key: _public_result(item) for key, item in value.items() if key not in sensitive}
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
        "last_error_message": job.last_error_message,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }
