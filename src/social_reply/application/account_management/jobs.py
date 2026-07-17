import logging
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from social_reply.application.account_management.audit import record_account_management_audit
from social_reply.application.account_management.service import (
    AccountConnectionResult,
    connect_meta_account,
    connect_telegram_account,
    connect_x_account,
)
from social_reply.application.account_management.submissions import split_submission
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.shared.config import get_settings

logger = logging.getLogger(__name__)
_MAX_BACKOFF_SECONDS = 300
_MAX_ATTEMPTS = 8
_STALE_AFTER = timedelta(minutes=5)


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
) -> uuid.UUID:
    if platform not in {"telegram", "facebook", "instagram", "whatsapp", "x"}:
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
                    staging_secret=secrets,
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
    if inserted is None and existing_job is not None and existing_job.status in {
        "FAILED",
        "NEEDS_ACTION",
    }:
        async with get_session_factory()() as session:
            await session.execute(
                update(models.ProvisioningJob)
                .where(models.ProvisioningJob.id == job_id)
                .values(
                    staging_secret=secrets,
                    status="PENDING",
                    current_step="QUEUED",
                    next_attempt_at=None,
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
    async with get_session_factory()() as session:
        row = (
            await session.execute(
                update(models.ProvisioningJob)
                .where(
                    models.ProvisioningJob.id == job_id,
                    models.ProvisioningJob.status.in_(["PENDING", "FAILED"]),
                    or_(
                        models.ProvisioningJob.next_attempt_at.is_(None),
                        models.ProvisioningJob.next_attempt_at <= datetime.now(UTC),
                    ),
                )
                .values(
                    status="PROCESSING",
                    current_step="VALIDATE_CREDENTIAL",
                    locked_at=datetime.now(UTC),
                    locked_by="provisioning-worker",
                    attempt_count=models.ProvisioningJob.attempt_count + 1,
                    last_error_code=None,
                    last_error_message=None,
                )
                .returning(models.ProvisioningJob)
            )
        ).scalar_one_or_none()
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
        "manual_steps": list(result.manual_steps),
    }


async def _connect(job: models.ProvisioningJob) -> AccountConnectionResult:
    settings = get_settings()
    request = dict(job.request or {})
    credentials = dict(job.staging_secret or {})
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
            enable_dm=bool(request.get("enable_dm", True)),
            enable_comments=bool(request.get("enable_comments", True)),
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
    return await connect_x_account(
        consumer_key=credentials["consumer_key"],
        consumer_secret=credentials["consumer_secret"],
        access_token=credentials["access_token"],
        access_token_secret=credentials["access_token_secret"],
        environment=str(request["environment"]),
        **common,
    )


async def process_provisioning_job(job_id: str) -> str:
    jid = uuid.UUID(job_id)
    job = await _claim_job(jid)
    if job is None:
        return "SKIPPED_NOT_CLAIMABLE"
    try:
        result = await _connect(job)
    except Exception as exc:  # noqa: BLE001 - platform boundary is normalized below
        error_code, message, retryable = _error(exc)
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
            await session.execute(
                update(models.ProvisioningJob)
                .where(
                    models.ProvisioningJob.id == jid,
                    models.ProvisioningJob.status == "PROCESSING",
                )
                .values(
                    status=status,
                    current_step="FAILED",
                    next_attempt_at=next_attempt_at,
                    locked_at=None,
                    locked_by=None,
                    last_error_code=error_code,
                    last_error_message=message,
                )
            )
            await session.commit()
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
        await session.execute(
            update(models.ProvisioningJob)
            .where(
                models.ProvisioningJob.id == jid,
                models.ProvisioningJob.status == "PROCESSING",
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
        )
        await session.commit()
    await record_account_management_audit(
        tenant_id=job.tenant_id,
        actor=job.actor,
        action="provisioning_completed",
        subject_id=str(result.account_id),
        detail={"platform": job.platform, "job_id": str(jid), "public_id": result.public_id},
    )
    return "COMPLETED"


async def retry_provisioning_job(job_id: uuid.UUID) -> None:
    async with get_session_factory()() as session:
        result = await session.execute(
            update(models.ProvisioningJob)
            .where(
                models.ProvisioningJob.id == job_id,
                models.ProvisioningJob.status.in_(["FAILED", "NEEDS_ACTION"]),
            )
            .values(
                status="PENDING",
                current_step="QUEUED",
                next_attempt_at=None,
                last_error_code=None,
                last_error_message=None,
            )
        )
        await session.commit()
        if result.rowcount == 0:
            raise ValueError("provisioning_job_not_retryable")


async def sweep_provisioning_jobs() -> list[uuid.UUID]:
    now = datetime.now(UTC)
    stale_before = now - _STALE_AFTER
    async with get_session_factory()() as session:
        await session.execute(
            update(models.ProvisioningJob)
            .where(
                models.ProvisioningJob.status == "PROCESSING",
                models.ProvisioningJob.locked_at < stale_before,
            )
            .values(
                status="FAILED",
                current_step="FAILED",
                next_attempt_at=now,
                locked_at=None,
                locked_by=None,
                last_error_code="STALE_PROCESSING",
                last_error_message="Stale provisioning job recovered by scheduler",
            )
        )
        rows = list(
            (
                await session.execute(
                    select(models.ProvisioningJob.id).where(
                        models.ProvisioningJob.status.in_(["PENDING", "FAILED"]),
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

    for pending_id in rows:
        process_platform_provisioning.send(str(pending_id))
    return rows


def _public_result(value: Any) -> Any:
    sensitive = {
        "verify_token",
        "token",
        "access_token",
        "access_token_secret",
        "app_secret",
        "consumer_key",
        "consumer_secret",
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
