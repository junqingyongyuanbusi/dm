import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, update

from social_reply.application.reply_decision.persist import (
    ChatwootDecisionDeferred,
    DecisionDeliveryConfigurationError,
    ensure_decision_delivery_available,
)
from social_reply.application.reply_decision.pipeline import DecisionSnapshot
from social_reply.application.reply_decision.runner import (
    DecisionContextScopeError,
    run_and_persist_decision,
)
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.shared.config import get_settings

logger = logging.getLogger(__name__)

_STALE_PROCESSING = timedelta(minutes=5)
_MAX_BACKOFF_SECONDS = 300
_MAX_ATTEMPTS = 8
_INITIAL_DISPATCH_ACTIVE_STATUSES = (
    "PENDING",
    "INITIAL_DISPATCH_RETRY",
    "INITIAL_DISPATCHING",
)


def snapshot_to_dict(snapshot: DecisionSnapshot) -> dict[str, str | int | None]:
    return {
        "text": snapshot.text,
        "platform": snapshot.platform,
        "tenant_id": snapshot.tenant_id,
        "brand_id": snapshot.brand_id,
        "account_id": snapshot.account_id,
        "conversation_key": snapshot.conversation_key,
        "automation_state": snapshot.automation_state,
        "state_version": snapshot.state_version,
    }


def snapshot_from_dict(value: dict) -> DecisionSnapshot:
    return DecisionSnapshot(
        text=value.get("text"),
        platform=value["platform"],
        tenant_id=value.get("tenant_id", "default"),
        brand_id=value["brand_id"],
        account_id=value["account_id"],
        conversation_key=value["conversation_key"],
        automation_state=value["automation_state"],
        state_version=int(value["state_version"]),
    )


async def process_decision_job(job_id: str) -> bool:
    """原子认领并执行一个持久决策任务；失败状态落库后交给补扫重试。"""
    jid = uuid.UUID(job_id)
    now = datetime.now(UTC)
    async with get_session_factory()() as session:
        claimed = (
            await session.execute(
                update(models.DecisionJob)
                .where(
                    models.DecisionJob.id == jid,
                    models.DecisionJob.status.in_(["PENDING", "FAILED"]),
                    models.DecisionJob.attempt_count < _MAX_ATTEMPTS,
                    or_(
                        models.DecisionJob.next_attempt_at.is_(None),
                        models.DecisionJob.next_attempt_at <= now,
                    ),
                )
                .values(
                    status="PROCESSING",
                    attempt_count=models.DecisionJob.attempt_count + 1,
                    locked_at=now,
                    last_error=None,
                )
                .returning(
                    models.DecisionJob.snapshot,
                    models.DecisionJob.conversation_id,
                    models.DecisionJob.message_id,
                    models.DecisionJob.account_id,
                    models.DecisionJob.raw_event_id,
                    models.DecisionJob.attempt_count,
                )
            )
        ).first()
        await session.commit()

    if claimed is None:
        return False

    try:
        async with get_session_factory()() as session:
            account = (
                await session.execute(
                    select(
                        models.PlatformAccount.config,
                        models.PlatformAccount.chatwoot_inbox_id,
                    ).where(models.PlatformAccount.id == claimed.account_id)
                )
            ).one()
        ensure_decision_delivery_available(
            account_config=dict(account.config or {}),
            chatwoot_inbox_id=account.chatwoot_inbox_id,
            chatwoot_enabled=get_settings().chatwoot_enabled,
        )
        await run_and_persist_decision(
            snapshot_from_dict(claimed.snapshot),
            claimed.conversation_id,
            claimed.message_id,
            claimed.account_id,
        )
    except DecisionContextScopeError as exc:
        async with get_session_factory()() as session:
            updated = (
                await session.execute(
                    update(models.DecisionJob)
                    .where(
                        models.DecisionJob.id == jid,
                        models.DecisionJob.status == "PROCESSING",
                        models.DecisionJob.attempt_count == claimed.attempt_count,
                    )
                    .values(
                        status="NEEDS_REVIEW",
                        next_attempt_at=None,
                        locked_at=None,
                        last_error=str(exc)[:2000],
                    )
                    .returning(models.DecisionJob.id)
                )
            ).first()
            if updated is not None and claimed.raw_event_id is not None:
                await session.execute(
                    update(models.RawEvent)
                    .where(
                        models.RawEvent.id == claimed.raw_event_id,
                        models.RawEvent.processing_status.not_in(_INITIAL_DISPATCH_ACTIVE_STATUSES),
                    )
                    .values(
                        processing_status="DECISION_NEEDS_REVIEW",
                        processed_at=datetime.now(UTC),
                    )
                )
            await session.commit()
        logger.error("decision context scope mismatch job_id=%s error=%s", jid, exc)
        return False
    except DecisionDeliveryConfigurationError as exc:
        async with get_session_factory()() as session:
            updated = (
                await session.execute(
                    update(models.DecisionJob)
                    .where(
                        models.DecisionJob.id == jid,
                        models.DecisionJob.status == "PROCESSING",
                        models.DecisionJob.attempt_count == claimed.attempt_count,
                    )
                    .values(
                        status="NEEDS_REVIEW",
                        next_attempt_at=None,
                        locked_at=None,
                        last_error=str(exc)[:2000],
                    )
                    .returning(models.DecisionJob.id)
                )
            ).first()
            if updated is not None and claimed.raw_event_id is not None:
                await session.execute(
                    update(models.RawEvent)
                    .where(
                        models.RawEvent.id == claimed.raw_event_id,
                        models.RawEvent.processing_status.not_in(_INITIAL_DISPATCH_ACTIVE_STATUSES),
                    )
                    .values(
                        processing_status="DECISION_NEEDS_REVIEW",
                        processed_at=datetime.now(UTC),
                    )
                )
            await session.commit()
        logger.error("decision delivery configuration invalid job_id=%s error=%s", jid, exc)
        return False
    except ChatwootDecisionDeferred as exc:
        async with get_session_factory()() as session:
            updated = (
                await session.execute(
                    update(models.DecisionJob)
                    .where(
                        models.DecisionJob.id == jid,
                        models.DecisionJob.status == "PROCESSING",
                        models.DecisionJob.attempt_count == claimed.attempt_count,
                    )
                    .values(
                        status="DEFERRED_CHATWOOT",
                        next_attempt_at=None,
                        locked_at=None,
                        last_error=str(exc)[:2000],
                    )
                    .returning(models.DecisionJob.id)
                )
            ).first()
            if updated is not None and claimed.raw_event_id is not None:
                await session.execute(
                    update(models.RawEvent)
                    .where(
                        models.RawEvent.id == claimed.raw_event_id,
                        models.RawEvent.processing_status.not_in(_INITIAL_DISPATCH_ACTIVE_STATUSES),
                    )
                    .values(
                        processing_status="DECISION_DEFERRED",
                        processed_at=None,
                    )
                )
            await session.commit()
        logger.info("decision deferred while Chatwoot is disabled job_id=%s", jid)
        return False
    except Exception as exc:
        exhausted = claimed.attempt_count >= _MAX_ATTEMPTS
        retry_at = None
        if not exhausted:
            retry_at = datetime.now(UTC) + timedelta(
                seconds=min(2 ** min(claimed.attempt_count, 8), _MAX_BACKOFF_SECONDS)
            )
        async with get_session_factory()() as session:
            updated = (
                await session.execute(
                    update(models.DecisionJob)
                    .where(
                        models.DecisionJob.id == jid,
                        models.DecisionJob.status == "PROCESSING",
                        models.DecisionJob.attempt_count == claimed.attempt_count,
                    )
                    .values(
                        status="NEEDS_REVIEW" if exhausted else "FAILED",
                        next_attempt_at=retry_at,
                        locked_at=None,
                        last_error=(f"RETRY_EXHAUSTED: {repr(exc)}" if exhausted else repr(exc))[
                            :2000
                        ],
                    )
                    .returning(models.DecisionJob.id)
                )
            ).first()
            if exhausted and updated is not None and claimed.raw_event_id is not None:
                await session.execute(
                    update(models.RawEvent)
                    .where(
                        models.RawEvent.id == claimed.raw_event_id,
                        models.RawEvent.processing_status.not_in(_INITIAL_DISPATCH_ACTIVE_STATUSES),
                    )
                    .values(
                        processing_status="DECISION_NEEDS_REVIEW",
                        processed_at=datetime.now(UTC),
                    )
                )
            await session.commit()
        if exhausted:
            logger.exception("decision retry limit exhausted job_id=%s", jid)
            return False
        raise

    async with get_session_factory()() as session:
        completed = (
            await session.execute(
                update(models.DecisionJob)
                .where(
                    models.DecisionJob.id == jid,
                    models.DecisionJob.status == "PROCESSING",
                    models.DecisionJob.attempt_count == claimed.attempt_count,
                )
                .values(
                    status="COMPLETED",
                    completed_at=datetime.now(UTC),
                    next_attempt_at=None,
                    locked_at=None,
                    last_error=None,
                )
                .returning(models.DecisionJob.id)
            )
        ).first()
        if completed is None:
            await session.commit()
            logger.warning(
                "decision finalization lost claim job_id=%s attempt=%s",
                jid,
                claimed.attempt_count,
            )
            return False
        if claimed.raw_event_id is not None:
            remaining = (
                await session.execute(
                    select(models.DecisionJob.id)
                    .where(
                        models.DecisionJob.raw_event_id == claimed.raw_event_id,
                        models.DecisionJob.id != jid,
                        models.DecisionJob.status != "COMPLETED",
                    )
                    .limit(1)
                )
            ).first()
            if remaining is None:
                await session.execute(
                    update(models.RawEvent)
                    .where(
                        models.RawEvent.id == claimed.raw_event_id,
                        models.RawEvent.processing_status.not_in(_INITIAL_DISPATCH_ACTIVE_STATUSES),
                    )
                    .values(
                        processing_status="PROCESSED",
                        processed_at=datetime.now(UTC),
                    )
                )
        await session.commit()
    return True


async def sweep_decision_jobs() -> list[uuid.UUID]:
    """恢复崩溃的 PROCESSING，并返回所有待执行任务供 scheduler 重新入队。"""
    now = datetime.now(UTC)
    async with get_session_factory()() as session:
        if get_settings().chatwoot_enabled:
            await session.execute(
                update(models.DecisionJob)
                .where(models.DecisionJob.status == "DEFERRED_CHATWOOT")
                .values(
                    status="PENDING",
                    next_attempt_at=now,
                    locked_at=None,
                    last_error=None,
                )
            )
        exhausted_raw_event_ids = list(
            (
                await session.execute(
                    update(models.DecisionJob)
                    .where(
                        or_(
                            (
                                (models.DecisionJob.status == "PROCESSING")
                                & (models.DecisionJob.locked_at < now - _STALE_PROCESSING)
                            ),
                            models.DecisionJob.status.in_(["PENDING", "FAILED"]),
                        ),
                        models.DecisionJob.attempt_count >= _MAX_ATTEMPTS,
                    )
                    .values(
                        status="NEEDS_REVIEW",
                        next_attempt_at=None,
                        locked_at=None,
                        last_error="RETRY_EXHAUSTED: recovered by sweep",
                    )
                    .returning(models.DecisionJob.raw_event_id)
                )
            ).scalars()
        )
        raw_event_ids = [raw_event_id for raw_event_id in exhausted_raw_event_ids if raw_event_id]
        if raw_event_ids:
            await session.execute(
                update(models.RawEvent)
                .where(
                    models.RawEvent.id.in_(raw_event_ids),
                    models.RawEvent.processing_status.not_in(_INITIAL_DISPATCH_ACTIVE_STATUSES),
                )
                .values(
                    processing_status="DECISION_NEEDS_REVIEW",
                    processed_at=now,
                )
            )
        await session.execute(
            update(models.DecisionJob)
            .where(
                models.DecisionJob.status == "PROCESSING",
                models.DecisionJob.locked_at < now - _STALE_PROCESSING,
                models.DecisionJob.attempt_count < _MAX_ATTEMPTS,
            )
            .values(
                status="FAILED",
                next_attempt_at=now,
                locked_at=None,
                last_error="stale PROCESSING recovered by sweep",
            )
        )
        rows = (
            (
                await session.execute(
                    select(models.DecisionJob.id).where(
                        models.DecisionJob.status.in_(["PENDING", "FAILED"]),
                        models.DecisionJob.attempt_count < _MAX_ATTEMPTS,
                        or_(
                            models.DecisionJob.next_attempt_at.is_(None),
                            models.DecisionJob.next_attempt_at <= now,
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        job_ids = list(rows)
        await session.commit()

    from social_reply.application.reply_decision.actors import process_reply_decision

    dispatched: list[uuid.UUID] = []
    for pending_id in job_ids:
        try:
            process_reply_decision.send(str(pending_id))
        except Exception:  # noqa: BLE001 - the durable row remains eligible for recovery
            logger.exception("decision dispatch failed job_id=%s", pending_id)
        else:
            dispatched.append(pending_id)
    return dispatched
