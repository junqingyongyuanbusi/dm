import uuid

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.application.reply_decision.jobs import snapshot_to_dict
from social_reply.application.reply_decision.pipeline import DecisionSnapshot
from social_reply.domain.automation.state_machine import ensure_state
from social_reply.domain.messages.canonical import CanonicalEvent, CanonicalEventKind
from social_reply.domain.platform_accounts import is_active_account_status
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.queue.dispatch import dispatch_actor


async def _mark_raw_event_account_inactive(
    session: AsyncSession,
    raw_event_id: uuid.UUID,
    *,
    claim_token: str | None,
) -> None:
    raw_event = (
        await session.execute(
            select(models.RawEvent).where(models.RawEvent.id == raw_event_id).with_for_update()
        )
    ).scalar_one_or_none()
    if raw_event is None:
        return
    if claim_token is not None and str(raw_event.processing_claim_token or "") != claim_token:
        return
    raw_event.processing_status = "IGNORED_ACCOUNT_INACTIVE"


async def ingest_canonical_event(
    event: CanonicalEvent,
    *,
    raw_event_id: uuid.UUID | None = None,
    raw_event_claim_token: str | None = None,
) -> uuid.UUID | None:
    """平台直连事件的通用 Inbox：同事务保存消息、状态快照和 DecisionJob。"""
    if event.event_kind is not CanonicalEventKind.MESSAGE:
        raise ValueError(f"canonical_event_not_reply_eligible:{event.event_kind}")
    account_uuid = uuid.UUID(event.platform_account_key)
    initial_dispatch_claim = False
    async with get_session_factory()() as session:
        account = (
            await session.execute(
                select(models.PlatformAccount).where(
                    models.PlatformAccount.id == account_uuid,
                    models.PlatformAccount.platform == event.platform,
                )
            )
        ).scalar_one_or_none()
        if account is None:
            raise LookupError(f"unknown_{event.platform}_account")
        if raw_event_id is not None:
            raw_event = (
                await session.execute(
                    select(models.RawEvent)
                    .where(models.RawEvent.id == raw_event_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if raw_event is None:
                raise LookupError(f"raw_event_not_found:{raw_event_id}")
            if (
                raw_event.platform_account_id is not None
                and raw_event.platform_account_id != account.id
            ):
                raise PermissionError("raw_event_platform_account_mismatch")
            if raw_event.tenant_id is not None and raw_event.tenant_id != account.tenant_id:
                raise PermissionError("raw_event_tenant_mismatch")
            if raw_event_claim_token is not None:
                if str(raw_event.processing_claim_token or "") != raw_event_claim_token:
                    await session.rollback()
                    return None
                initial_dispatch_claim = raw_event.processing_status == "INITIAL_DISPATCHING"
                if initial_dispatch_claim:
                    database_now = await session.scalar(select(func.clock_timestamp()))
                    if (
                        raw_event.processing_claim_expires_at is None
                        or raw_event.processing_claim_expires_at <= database_now
                    ):
                        await session.rollback()
                        return None
        if not is_active_account_status(account.status):
            if raw_event_id is not None and not initial_dispatch_claim:
                await session.execute(
                    update(models.RawEvent)
                    .where(models.RawEvent.id == raw_event_id)
                    .values(processing_status="IGNORED_ACCOUNT_INACTIVE")
                )
            await session.commit()
            return None

        if raw_event_id is not None and not initial_dispatch_claim:
            await session.execute(
                update(models.RawEvent)
                .where(models.RawEvent.id == raw_event_id)
                .values(processing_status="PROCESSING")
            )

        managed_echo = None
        if event.platform == "x":
            event_uuid = None
            try:
                event_uuid = uuid.UUID(event.external_event_id)
            except ValueError:
                pass
            echo_conditions = [models.OutboxMessage.platform_message_id == event.external_event_id]
            if event_uuid is not None:
                echo_conditions.append(models.OutboxMessage.id == event_uuid)
            managed_echo = (
                await session.execute(
                    select(
                        models.OutboxMessage.id,
                        models.OutboxMessage.platform_account_id,
                    )
                    .join(
                        models.PlatformAccount,
                        models.PlatformAccount.id == models.OutboxMessage.platform_account_id,
                    )
                    .where(
                        models.OutboxMessage.tenant_id == account.tenant_id,
                        models.OutboxMessage.platform_account_id != account.id,
                        models.PlatformAccount.platform == "x",
                        or_(*echo_conditions),
                    )
                    .limit(1)
                )
            ).one_or_none()
        if managed_echo is not None:
            if raw_event_id is not None and not initial_dispatch_claim:
                await session.execute(
                    update(models.RawEvent)
                    .where(models.RawEvent.id == raw_event_id)
                    .values(processing_status="IGNORED_MANAGED_OUTBOX_ECHO")
                )
            session.add(
                models.AuditLog(
                    tenant_id=account.tenant_id,
                    category="ingestion",
                    actor="system",
                    action="managed_x_outbox_echo_ignored",
                    subject_type="raw_event" if raw_event_id is not None else "platform_event",
                    subject_id=str(raw_event_id or event.external_event_id),
                    detail={
                        "target_account_id": str(account.id),
                        "source_account_id": str(managed_echo.platform_account_id),
                        "source_outbox_id": str(managed_echo.id),
                        "external_event_id": event.external_event_id,
                    },
                )
            )
            await session.commit()
            return None

        normalized_id = (
            await session.execute(
                pg_insert(models.NormalizedEvent)
                .values(
                    tenant_id=account.tenant_id,
                    platform=event.platform,
                    platform_account_id=account.id,
                    external_event_id=event.external_event_id,
                    event_type=f"{event.channel_type}.message.created",
                    raw_event_id=raw_event_id,
                    external_conversation_id=event.external_conversation_id,
                    event_metadata={
                        "event_namespace": event.event_namespace or event.platform,
                        **event.event_metadata,
                    },
                    occurred_at=event.occurred_at,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        "tenant_id",
                        "platform",
                        "platform_account_id",
                        "external_event_id",
                    ]
                )
                .returning(models.NormalizedEvent.id)
            )
        ).scalar_one_or_none()
        if normalized_id is None:
            if raw_event_id is not None and not initial_dispatch_claim:
                await session.execute(
                    update(models.RawEvent)
                    .where(models.RawEvent.id == raw_event_id)
                    .values(processing_status="SKIPPED_DUPLICATE")
                )
            await session.commit()
            return None

        contact = (
            await session.execute(
                pg_insert(models.Contact)
                .values(
                    id=uuid.uuid4(),
                    tenant_id=account.tenant_id,
                    platform=event.platform,
                    platform_account_id=account.id,
                    external_user_id=event.external_user_id,
                )
                .on_conflict_do_nothing(index_elements=["platform_account_id", "external_user_id"])
                .returning(models.Contact.id)
            )
        ).scalar_one_or_none()
        if contact is None:
            contact = (
                await session.execute(
                    select(models.Contact.id).where(
                        models.Contact.platform_account_id == account.id,
                        models.Contact.external_user_id == event.external_user_id,
                    )
                )
            ).scalar_one()

        await session.execute(
            pg_insert(models.Conversation)
            .values(
                id=uuid.uuid4(),
                tenant_id=account.tenant_id,
                brand_id=account.brand_id,
                platform=event.platform,
                platform_account_id=account.id,
                contact_id=contact,
                conversation_key=event.conversation_key,
                channel_type=event.channel_type,
            )
            .on_conflict_do_nothing(index_elements=["tenant_id", "conversation_key"])
        )
        conversation = (
            await session.execute(
                select(models.Conversation).where(
                    models.Conversation.tenant_id == account.tenant_id,
                    models.Conversation.conversation_key == event.conversation_key,
                )
            )
        ).scalar_one()
        await ensure_state(session, conversation.id, account.automation_default)

        message_id = uuid.uuid4()
        await session.execute(
            pg_insert(models.Message).values(
                id=message_id,
                conversation_id=conversation.id,
                direction="inbound",
                sender_type="contact",
                text=event.text,
                platform_message_id=event.external_event_id,
                reply_target=event.reply_target,
                occurred_at=event.occurred_at,
            )
        )
        await session.execute(
            models.NormalizedEvent.__table__.update()
            .where(models.NormalizedEvent.id == normalized_id)
            .values(conversation_id=conversation.id, message_id=message_id)
        )
        state = (
            await session.execute(
                select(models.AutomationState.state, models.AutomationState.state_version).where(
                    models.AutomationState.conversation_id == conversation.id
                )
            )
        ).one()
        snapshot = DecisionSnapshot(
            text=event.text,
            platform=event.platform,
            tenant_id=account.tenant_id,
            brand_id=account.brand_id,
            account_id=str(account.id),
            conversation_key=conversation.conversation_key,
            automation_state=state.state,
            state_version=state.state_version,
        )
        job_id = (
            await session.execute(
                pg_insert(models.DecisionJob)
                .values(
                    raw_event_id=raw_event_id,
                    conversation_id=conversation.id,
                    message_id=message_id,
                    account_id=account.id,
                    snapshot=snapshot_to_dict(snapshot),
                    status="PENDING",
                )
                .on_conflict_do_nothing(index_elements=["message_id"])
                .returning(models.DecisionJob.id)
            )
        ).scalar_one_or_none()
        if job_id is None:
            job_id = (
                await session.execute(
                    select(models.DecisionJob.id).where(models.DecisionJob.message_id == message_id)
                )
            ).scalar_one()
        if raw_event_id is not None and not initial_dispatch_claim:
            await session.execute(
                update(models.RawEvent)
                .where(
                    models.RawEvent.id == raw_event_id,
                    models.RawEvent.processing_status == "PROCESSING",
                )
                .values(processing_status="DECISION_PENDING")
            )
        latest_status = await session.scalar(
            select(models.PlatformAccount.status)
            .where(models.PlatformAccount.id == account.id)
            .with_for_update()
        )
        if latest_status is None or not is_active_account_status(latest_status):
            await session.rollback()
            if raw_event_id is not None and not initial_dispatch_claim:
                await _mark_raw_event_account_inactive(
                    session,
                    raw_event_id,
                    claim_token=raw_event_claim_token,
                )
                await session.commit()
            return None
        await session.commit()

    from social_reply.application.reply_decision.actors import process_reply_decision
    from social_reply.application.reply_decision.jobs import process_decision_job

    await dispatch_actor(
        process_reply_decision,
        str(job_id),
        inline=lambda: process_decision_job(str(job_id)),
    )
    return job_id
