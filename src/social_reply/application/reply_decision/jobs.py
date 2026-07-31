import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.application.message_delivery.intents import OutboxActor, OutboxOrigin
from social_reply.application.reply_decision.persist import (
    ChatwootDecisionDeferred,
    DecisionDeliveryConfigurationError,
    ensure_decision_delivery_available,
)
from social_reply.application.reply_decision.pipeline import DecisionSnapshot
from social_reply.application.reply_decision.runner import (
    DecisionContextScopeError,
    DecisionSuperseded,
    run_and_persist_decision,
)
from social_reply.domain.messages.canonical import ChannelType
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.advisory_locks import (
    acquire_conversation_delivery_xact_lock,
)
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.queue.dispatch import dispatch_actor
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
_ACTIVE_JOB_STATUSES = ("PENDING", "FAILED", "PROCESSING", "DEFERRED_CHATWOOT")
_TERMINAL_JOB_STATUSES = {"COMPLETED", "SUPERSEDED"}


def snapshot_to_dict(snapshot: DecisionSnapshot) -> dict[str, str | int | bool | None]:
    return {
        "text": snapshot.text,
        "platform": snapshot.platform,
        "tenant_id": snapshot.tenant_id,
        "brand_id": snapshot.brand_id,
        "account_id": snapshot.account_id,
        "conversation_key": snapshot.conversation_key,
        "automation_state": snapshot.automation_state,
        "state_version": snapshot.state_version,
        "channel_type": snapshot.channel_type.value,
        "has_unsupported_attachment": snapshot.has_unsupported_attachment,
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
        channel_type=ChannelType(value.get("channel_type", ChannelType.DM)),
        has_unsupported_attachment=bool(value.get("has_unsupported_attachment", False)),
    )


async def reserve_conversation_generation(
    session: AsyncSession,
    conversation_id: uuid.UUID,
) -> int:
    """Advance one reply-eligible input generation and retire stale bot work."""
    await acquire_conversation_delivery_xact_lock(session, conversation_id)
    generation = await session.scalar(
        update(models.Conversation)
        .where(models.Conversation.id == conversation_id)
        .values(decision_generation=models.Conversation.decision_generation + 1)
        .returning(models.Conversation.decision_generation)
    )
    if generation is None:
        raise DecisionContextScopeError("decision_conversation_missing")

    superseded_raw_event_ids = list(
        (
            await session.execute(
                update(models.DecisionJob)
                .where(
                    models.DecisionJob.conversation_id == conversation_id,
                    models.DecisionJob.decision_generation < generation,
                    models.DecisionJob.status.in_(_ACTIVE_JOB_STATUSES),
                )
                .values(
                    status="SUPERSEDED",
                    next_attempt_at=None,
                    locked_at=None,
                    claim_token=None,
                    completed_at=func.clock_timestamp(),
                    last_error="superseded by a newer inbound message",
                )
                .returning(models.DecisionJob.raw_event_id)
            )
        ).scalars()
    )
    stale_outboxes = select(models.ReplyDecision.outbox_id).where(
        models.ReplyDecision.conversation_id == conversation_id,
        models.ReplyDecision.decision_generation < generation,
        models.ReplyDecision.outbox_id.is_not(None),
    )
    await session.execute(
        update(models.OutboxMessage)
        .where(
            models.OutboxMessage.id.in_(stale_outboxes),
            models.OutboxMessage.origin_kind == OutboxOrigin.DECISION,
            models.OutboxMessage.actor_kind == OutboxActor.BOT,
            models.OutboxMessage.status.in_(("PENDING", "FAILED")),
        )
        .values(
            status="CANCELLED",
            last_error_code="STALE_CONVERSATION_INPUT",
            last_error_message="superseded by a newer inbound message",
        )
    )
    for superseded_raw_event_id in {
        value for value in superseded_raw_event_ids if value is not None
    }:
        await aggregate_raw_event_decisions(session, superseded_raw_event_id)
    return generation


async def reserve_decision_job(
    session: AsyncSession,
    *,
    raw_event_id: uuid.UUID | None,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    account_id: uuid.UUID,
    snapshot: DecisionSnapshot,
    decision_generation: int | None = None,
) -> uuid.UUID:
    """Persist the durable job for an already reserved message generation."""
    await acquire_conversation_delivery_xact_lock(session, conversation_id)
    existing = (
        await session.execute(
            select(
                models.DecisionJob.id,
                models.DecisionJob.conversation_id,
                models.DecisionJob.account_id,
            ).where(models.DecisionJob.message_id == message_id)
        )
    ).one_or_none()
    if existing is not None:
        if existing.conversation_id != conversation_id or existing.account_id != account_id:
            raise DecisionContextScopeError("decision_job_message_scope_mismatch")
        return existing.id

    if decision_generation is None:
        decision_generation = await session.scalar(
            select(models.Message.decision_generation).where(models.Message.id == message_id)
        )
    if decision_generation is None:
        raise DecisionContextScopeError("decision_message_generation_missing")

    return (
        await session.execute(
            pg_insert(models.DecisionJob)
            .values(
                raw_event_id=raw_event_id,
                conversation_id=conversation_id,
                message_id=message_id,
                account_id=account_id,
                snapshot=snapshot_to_dict(snapshot),
                decision_generation=decision_generation,
                status="PENDING",
            )
            .returning(models.DecisionJob.id)
        )
    ).scalar_one()


def raw_event_decision_status(statuses: set[str]) -> str:
    """Map decision-job states to their durable RawEvent aggregate."""
    if not statuses:
        return "PROCESSED"
    if "NEEDS_REVIEW" in statuses:
        return "DECISION_NEEDS_REVIEW"
    if "DEFERRED_CHATWOOT" in statuses:
        return "DECISION_DEFERRED"
    if statuses and statuses <= _TERMINAL_JOB_STATUSES:
        return "PROCESSED"
    return "DECISION_PENDING"


async def aggregate_raw_event_decisions(session: AsyncSession, raw_event_id: uuid.UUID) -> None:
    """Aggregate all decision jobs into the RawEvent processing state."""
    statuses = set(
        (
            await session.execute(
                select(models.DecisionJob.status).where(
                    models.DecisionJob.raw_event_id == raw_event_id
                )
            )
        ).scalars()
    )
    if not statuses:
        return
    processing_status = raw_event_decision_status(statuses)
    now = datetime.now(UTC)
    values = {
        "processing_status": processing_status,
        "processed_at": (
            None if processing_status in {"DECISION_PENDING", "DECISION_DEFERRED"} else now
        ),
    }
    await session.execute(
        update(models.RawEvent)
        .where(
            models.RawEvent.id == raw_event_id,
            models.RawEvent.processing_status.not_in(_INITIAL_DISPATCH_ACTIVE_STATUSES),
        )
        .values(**values)
    )


async def _finish_claim(
    job_id: uuid.UUID,
    claim_token: uuid.UUID,
    *,
    status: str,
    last_error: str | None,
    next_attempt_at: datetime | None = None,
) -> bool:
    async with get_session_factory()() as session:
        row = (
            await session.execute(
                update(models.DecisionJob)
                .where(
                    models.DecisionJob.id == job_id,
                    models.DecisionJob.status == "PROCESSING",
                    models.DecisionJob.claim_token == claim_token,
                )
                .values(
                    status=status,
                    next_attempt_at=next_attempt_at,
                    locked_at=None,
                    claim_token=None,
                    completed_at=(
                        datetime.now(UTC) if status in {"NEEDS_REVIEW", "SUPERSEDED"} else None
                    ),
                    last_error=last_error,
                )
                .returning(models.DecisionJob.raw_event_id)
            )
        ).first()
        if row is not None and row.raw_event_id is not None:
            await aggregate_raw_event_decisions(session, row.raw_event_id)
        await session.commit()
    return row is not None


async def process_decision_job(job_id: str) -> bool:
    """Claim and run one durable decision job with a random lease fence."""
    jid = uuid.UUID(job_id)
    now = datetime.now(UTC)
    token = uuid.uuid4()
    async with get_session_factory()() as session:
        superseded_raw_event_id = await session.scalar(
            update(models.DecisionJob)
            .where(
                models.DecisionJob.id == jid,
                models.DecisionJob.status.in_(("PENDING", "FAILED", "DEFERRED_CHATWOOT")),
                models.DecisionJob.decision_generation
                < select(models.Conversation.decision_generation)
                .where(models.Conversation.id == models.DecisionJob.conversation_id)
                .scalar_subquery(),
            )
            .values(
                status="SUPERSEDED",
                next_attempt_at=None,
                locked_at=None,
                claim_token=None,
                completed_at=now,
                last_error="superseded before claim",
            )
            .returning(models.DecisionJob.raw_event_id)
        )
        if superseded_raw_event_id is not None:
            await aggregate_raw_event_decisions(session, superseded_raw_event_id)
        claimed = (
            await session.execute(
                update(models.DecisionJob)
                .where(
                    models.DecisionJob.id == jid,
                    models.DecisionJob.status.in_(("PENDING", "FAILED")),
                    models.DecisionJob.attempt_count < _MAX_ATTEMPTS,
                    or_(
                        models.DecisionJob.next_attempt_at.is_(None),
                        models.DecisionJob.next_attempt_at <= now,
                    ),
                    models.DecisionJob.decision_generation
                    == select(models.Conversation.decision_generation)
                    .where(models.Conversation.id == models.DecisionJob.conversation_id)
                    .scalar_subquery(),
                )
                .values(
                    status="PROCESSING",
                    attempt_count=models.DecisionJob.attempt_count + 1,
                    locked_at=now,
                    claim_token=token,
                    last_error=None,
                )
                .returning(
                    models.DecisionJob.snapshot,
                    models.DecisionJob.conversation_id,
                    models.DecisionJob.message_id,
                    models.DecisionJob.account_id,
                    models.DecisionJob.raw_event_id,
                    models.DecisionJob.attempt_count,
                    models.DecisionJob.decision_generation,
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
            decision_job_id=jid,
            decision_generation=claimed.decision_generation,
            claim_token=token,
            raw_event_id=claimed.raw_event_id,
        )
    except DecisionSuperseded:
        return False
    except (DecisionContextScopeError, DecisionDeliveryConfigurationError) as exc:
        updated = await _finish_claim(jid, token, status="NEEDS_REVIEW", last_error=str(exc)[:2000])
        if updated:
            logger.error("decision requires review job_id=%s error=%s", jid, exc)
        return False
    except ChatwootDecisionDeferred as exc:
        updated = await _finish_claim(
            jid, token, status="DEFERRED_CHATWOOT", last_error=str(exc)[:2000]
        )
        if updated:
            logger.info("decision deferred while Chatwoot is disabled job_id=%s", jid)
        return False
    except Exception as exc:
        exhausted = claimed.attempt_count >= _MAX_ATTEMPTS
        retry_at = None
        if not exhausted:
            retry_at = datetime.now(UTC) + timedelta(
                seconds=min(2 ** min(claimed.attempt_count, 8), _MAX_BACKOFF_SECONDS)
            )
        updated = await _finish_claim(
            jid,
            token,
            status="NEEDS_REVIEW" if exhausted else "FAILED",
            next_attempt_at=retry_at,
            last_error=(f"RETRY_EXHAUSTED: {repr(exc)}" if exhausted else repr(exc))[:2000],
        )
        if not updated:
            return False
        if exhausted:
            logger.exception("decision retry limit exhausted job_id=%s", jid)
            return False
        raise

    async with get_session_factory()() as session:
        status = await session.scalar(
            select(models.DecisionJob.status).where(models.DecisionJob.id == jid)
        )
        if status == "COMPLETED":
            return True
        completed = (
            await session.execute(
                update(models.DecisionJob)
                .where(
                    models.DecisionJob.id == jid,
                    models.DecisionJob.status == "PROCESSING",
                    models.DecisionJob.claim_token == token,
                )
                .values(
                    status="COMPLETED",
                    completed_at=datetime.now(UTC),
                    next_attempt_at=None,
                    locked_at=None,
                    claim_token=None,
                    last_error=None,
                )
                .returning(models.DecisionJob.raw_event_id)
            )
        ).first()
        if completed is not None and completed.raw_event_id is not None:
            await aggregate_raw_event_decisions(session, completed.raw_event_id)
        await session.commit()
    return completed is not None


async def sweep_decision_jobs() -> list[uuid.UUID]:
    """Supersede stale generations, recover expired claims, and dispatch due jobs."""
    now = datetime.now(UTC)
    current_generation = (
        select(models.Conversation.decision_generation)
        .where(models.Conversation.id == models.DecisionJob.conversation_id)
        .scalar_subquery()
    )
    async with get_session_factory()() as session:
        superseded_raw_event_ids = list(
            (
                await session.execute(
                    update(models.DecisionJob)
                    .where(
                        models.DecisionJob.status.in_(_ACTIVE_JOB_STATUSES),
                        models.DecisionJob.decision_generation < current_generation,
                    )
                    .values(
                        status="SUPERSEDED",
                        next_attempt_at=None,
                        locked_at=None,
                        claim_token=None,
                        completed_at=now,
                        last_error="superseded by decision sweep",
                    )
                    .returning(models.DecisionJob.raw_event_id)
                )
            ).scalars()
        )
        if get_settings().chatwoot_enabled:
            await session.execute(
                update(models.DecisionJob)
                .where(
                    models.DecisionJob.status == "DEFERRED_CHATWOOT",
                    models.DecisionJob.decision_generation == current_generation,
                )
                .values(status="PENDING", next_attempt_at=now, last_error=None)
            )
        exhausted = list(
            (
                await session.execute(
                    update(models.DecisionJob)
                    .where(
                        or_(
                            (
                                (models.DecisionJob.status == "PROCESSING")
                                & (models.DecisionJob.locked_at < now - _STALE_PROCESSING)
                            ),
                            models.DecisionJob.status.in_(("PENDING", "FAILED")),
                        ),
                        models.DecisionJob.attempt_count >= _MAX_ATTEMPTS,
                        models.DecisionJob.decision_generation == current_generation,
                    )
                    .values(
                        status="NEEDS_REVIEW",
                        next_attempt_at=None,
                        locked_at=None,
                        claim_token=None,
                        completed_at=now,
                        last_error="RETRY_EXHAUSTED: recovered by sweep",
                    )
                    .returning(models.DecisionJob.raw_event_id)
                )
            ).scalars()
        )
        await session.execute(
            update(models.DecisionJob)
            .where(
                models.DecisionJob.status == "PROCESSING",
                models.DecisionJob.locked_at < now - _STALE_PROCESSING,
                models.DecisionJob.attempt_count < _MAX_ATTEMPTS,
                models.DecisionJob.decision_generation == current_generation,
            )
            .values(
                status="FAILED",
                next_attempt_at=now,
                locked_at=None,
                claim_token=None,
                last_error="stale PROCESSING recovered by sweep",
            )
        )
        raw_event_ids = {
            value for value in (*superseded_raw_event_ids, *exhausted) if value is not None
        }
        for raw_event_id in raw_event_ids:
            await aggregate_raw_event_decisions(session, raw_event_id)
        job_ids = list(
            (
                await session.execute(
                    select(models.DecisionJob.id).where(
                        models.DecisionJob.status.in_(("PENDING", "FAILED")),
                        models.DecisionJob.attempt_count < _MAX_ATTEMPTS,
                        models.DecisionJob.decision_generation == current_generation,
                        or_(
                            models.DecisionJob.next_attempt_at.is_(None),
                            models.DecisionJob.next_attempt_at <= now,
                        ),
                    )
                )
            ).scalars()
        )
        await session.commit()

    from social_reply.application.reply_decision.actors import process_reply_decision

    dispatched: list[uuid.UUID] = []
    for pending_id in job_ids:
        try:
            await dispatch_actor(process_reply_decision, str(pending_id))
        except Exception:  # noqa: BLE001 - durable state remains eligible for recovery
            logger.exception("decision dispatch failed job_id=%s", pending_id)
        else:
            dispatched.append(pending_id)
    return dispatched
