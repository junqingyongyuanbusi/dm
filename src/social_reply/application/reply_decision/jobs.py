import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, update

from social_reply.application.reply_decision.pipeline import DecisionSnapshot
from social_reply.application.reply_decision.runner import run_and_persist_decision
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory

logger = logging.getLogger(__name__)

_STALE_PROCESSING = timedelta(minutes=5)
_MAX_BACKOFF_SECONDS = 300


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
        await run_and_persist_decision(
            snapshot_from_dict(claimed.snapshot),
            claimed.conversation_id,
            claimed.message_id,
            claimed.account_id,
        )
    except Exception as exc:
        retry_at = datetime.now(UTC) + timedelta(
            seconds=min(2 ** min(claimed.attempt_count, 8), _MAX_BACKOFF_SECONDS)
        )
        async with get_session_factory()() as session:
            await session.execute(
                update(models.DecisionJob)
                .where(
                    models.DecisionJob.id == jid,
                    models.DecisionJob.status == "PROCESSING",
                )
                .values(
                    status="FAILED",
                    next_attempt_at=retry_at,
                    locked_at=None,
                    last_error=repr(exc)[:2000],
                )
            )
            await session.commit()
        raise

    async with get_session_factory()() as session:
        await session.execute(
            update(models.DecisionJob)
            .where(
                models.DecisionJob.id == jid,
                models.DecisionJob.status == "PROCESSING",
            )
            .values(
                status="COMPLETED",
                completed_at=datetime.now(UTC),
                next_attempt_at=None,
                locked_at=None,
                last_error=None,
            )
        )
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
                    .where(models.RawEvent.id == claimed.raw_event_id)
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
        await session.execute(
            update(models.DecisionJob)
            .where(
                models.DecisionJob.status == "PROCESSING",
                models.DecisionJob.locked_at < now - _STALE_PROCESSING,
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

    for pending_id in job_ids:
        process_reply_decision.send(str(pending_id))
    return job_ids
