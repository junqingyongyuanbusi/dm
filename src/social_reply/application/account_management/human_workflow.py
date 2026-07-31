import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.application.message_delivery.intents import (
    OutboxActor,
    OutboxIdempotencyConflict,
    OutboxOrigin,
    create_or_get_outbox_intent,
    find_outbox_intent,
)
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.advisory_locks import (
    acquire_conversation_delivery_xact_lock,
)
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.queue.dispatch import dispatch_actor
from social_reply.shared.config import get_settings


class HumanWorkflowError(ValueError):
    pass


class HumanWorkflowConflict(HumanWorkflowError):
    pass


def require_work_conversation_tenant(
    work: models.HumanWorkItem, *, conversation_tenant_id: str
) -> None:
    if work.tenant_id != conversation_tenant_id:
        raise HumanWorkflowConflict("human_work_item_tenant_mismatch")


async def ensure_open_human_work_item(
    session: AsyncSession,
    *,
    tenant_id: str,
    conversation_id: uuid.UUID,
    reason_code: str,
    priority: int = 0,
) -> models.HumanWorkItem:
    conversation_tenant_id = await session.scalar(
        select(models.Conversation.tenant_id).where(models.Conversation.id == conversation_id)
    )
    if conversation_tenant_id is None:
        raise HumanWorkflowError("conversation_not_found")
    if conversation_tenant_id != tenant_id:
        raise HumanWorkflowError("conversation_tenant_mismatch")

    candidate_id = uuid.uuid4()
    inserted_id = (
        await session.execute(
            pg_insert(models.HumanWorkItem)
            .values(
                id=candidate_id,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                status="WAITING",
                reason_code=reason_code or "HANDOFF",
                priority=priority,
                due_at=datetime.now(UTC) + timedelta(minutes=30),
                version=1,
            )
            .on_conflict_do_nothing(
                index_elements=["conversation_id"],
                index_where=models.HumanWorkItem.status.in_(["WAITING", "CLAIMED"]),
            )
            .returning(models.HumanWorkItem.id)
        )
    ).scalar_one_or_none()
    work = (
        await session.get(models.HumanWorkItem, inserted_id)
        if inserted_id is not None
        else (
            await session.execute(
                select(models.HumanWorkItem).where(
                    models.HumanWorkItem.conversation_id == conversation_id,
                    models.HumanWorkItem.status.in_(["WAITING", "CLAIMED"]),
                )
            )
        ).scalar_one()
    )
    if work is None:
        raise HumanWorkflowError("human_work_item_not_found")
    require_work_conversation_tenant(work, conversation_tenant_id=conversation_tenant_id)
    return work


async def _load_work_for_actor(
    session: AsyncSession,
    *,
    work_item_id: uuid.UUID,
    allowed_tenants: frozenset[str],
) -> models.HumanWorkItem:
    work = (
        await session.execute(
            select(models.HumanWorkItem)
            .where(
                models.HumanWorkItem.id == work_item_id,
                models.HumanWorkItem.tenant_id.in_(allowed_tenants),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if work is None:
        raise HumanWorkflowError("human_work_item_not_found")
    conversation_tenant_id = await session.scalar(
        select(models.Conversation.tenant_id).where(models.Conversation.id == work.conversation_id)
    )
    if conversation_tenant_id is None:
        raise HumanWorkflowError("conversation_not_found")
    require_work_conversation_tenant(work, conversation_tenant_id=conversation_tenant_id)
    return work


async def claim_human_work_item(
    *,
    work_item_id: uuid.UUID,
    allowed_tenants: frozenset[str],
    actor: str,
    user_id: uuid.UUID | None,
    expected_version: int,
) -> None:
    async with get_session_factory()() as session:
        work = await _load_work_for_actor(
            session, work_item_id=work_item_id, allowed_tenants=allowed_tenants
        )
        if work.status != "WAITING" or work.version != expected_version:
            raise HumanWorkflowConflict("human_work_item_version_conflict")
        now = datetime.now(UTC)
        work.status = "CLAIMED"
        work.assigned_user_id = user_id
        work.assigned_actor = actor
        work.claimed_at = now
        work.version += 1
        session.add(
            models.AuditLog(
                tenant_id=work.tenant_id,
                category="human_work",
                actor=actor,
                action="CLAIM",
                subject_type="human_work_item",
                subject_id=str(work.id),
                detail={"conversation_id": str(work.conversation_id)},
            )
        )
        await session.commit()


def _require_assignee(work: models.HumanWorkItem, *, actor: str, allow_override: bool) -> None:
    if work.status != "CLAIMED":
        raise HumanWorkflowConflict("human_work_item_not_claimed")
    if not allow_override and work.assigned_actor != actor:
        raise HumanWorkflowConflict("human_work_item_assigned_to_another_user")


async def resolve_human_work_item(
    *,
    work_item_id: uuid.UUID,
    allowed_tenants: frozenset[str],
    actor: str,
    expected_version: int,
    allow_override: bool,
) -> None:
    async with get_session_factory()() as session:
        work = await _load_work_for_actor(
            session, work_item_id=work_item_id, allowed_tenants=allowed_tenants
        )
        if work.version != expected_version:
            raise HumanWorkflowConflict("human_work_item_version_conflict")
        _require_assignee(work, actor=actor, allow_override=allow_override)
        work.status = "RESOLVED"
        work.resolved_at = datetime.now(UTC)
        work.version += 1
        session.add(
            models.AuditLog(
                tenant_id=work.tenant_id,
                category="human_work",
                actor=actor,
                action="RESOLVE",
                subject_type="human_work_item",
                subject_id=str(work.id),
                detail={"conversation_id": str(work.conversation_id)},
            )
        )
        await session.commit()


async def resume_bot(
    *,
    conversation_id: uuid.UUID,
    allowed_tenants: frozenset[str],
    actor: str,
    target: str,
) -> None:
    if target not in {"BOT_DRAFT_ONLY", "BOT_ACTIVE"}:
        raise HumanWorkflowError("resume_target_invalid")
    async with get_session_factory()() as session:
        conversation = (
            await session.execute(
                select(models.Conversation)
                .where(
                    models.Conversation.id == conversation_id,
                    models.Conversation.tenant_id.in_(allowed_tenants),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if conversation is None:
            raise HumanWorkflowError("conversation_not_found")
        account = await session.get(models.PlatformAccount, conversation.platform_account_id)
        if account is None or account.tenant_id != conversation.tenant_id:
            raise HumanWorkflowError("conversation_account_scope_mismatch")
        if not get_settings().meta_automation_default_allowed(account.platform, target):
            raise HumanWorkflowError("meta_requires_bot_draft_only")
        open_work = (
            await session.execute(
                select(models.HumanWorkItem).where(
                    models.HumanWorkItem.conversation_id == conversation_id,
                    models.HumanWorkItem.status.in_(["WAITING", "CLAIMED"]),
                )
            )
        ).scalar_one_or_none()
        if open_work is not None:
            require_work_conversation_tenant(
                open_work, conversation_tenant_id=conversation.tenant_id
            )
            raise HumanWorkflowConflict("human_work_item_still_open")
        await acquire_conversation_delivery_xact_lock(session, conversation_id)
        changed = await session.execute(
            update(models.AutomationState)
            .where(
                models.AutomationState.conversation_id == conversation_id,
                models.AutomationState.state.in_(["HUMAN_ACTIVE", "BOT_COOLDOWN"]),
            )
            .values(
                state=target,
                state_version=models.AutomationState.state_version + 1,
                human_agent_id=None,
                state_changed_reason="human_work_resumed",
            )
        )
        if changed.rowcount != 1:
            raise HumanWorkflowConflict("conversation_not_human_active")
        session.add(
            models.AuditLog(
                tenant_id=conversation.tenant_id,
                category="state_transition",
                actor=actor,
                action=target,
                subject_type="conversation",
                subject_id=str(conversation_id),
                detail={"reason": "human_work_resumed"},
            )
        )
        await session.commit()


async def send_human_reply(
    *,
    conversation_id: uuid.UUID,
    reply_to_message_id: uuid.UUID,
    text: str,
    idempotency_key: str,
    allowed_tenants: frozenset[str],
    actor: str,
    user_id: uuid.UUID | None,
    allow_override: bool,
    work_item_id: uuid.UUID | None = None,
    expected_version: int | None = None,
) -> uuid.UUID:
    async with get_session_factory()() as session:
        conversation = (
            await session.execute(
                select(models.Conversation)
                .where(
                    models.Conversation.id == conversation_id,
                    models.Conversation.tenant_id.in_(allowed_tenants),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if conversation is None:
            raise HumanWorkflowError("conversation_not_found")
        await acquire_conversation_delivery_xact_lock(session, conversation_id)

        existing_intent = await find_outbox_intent(
            session,
            tenant_id=conversation.tenant_id,
            conversation_id=conversation.id,
            idempotency_key=idempotency_key,
        )
        if existing_intent is not None:
            same_intent = (
                existing_intent.platform_account_id == conversation.platform_account_id
                and existing_intent.reply_to_message_id == reply_to_message_id
                and existing_intent.origin_kind == OutboxOrigin.MANUAL_REPLY
                and existing_intent.actor_kind == OutboxActor.ADMIN_HUMAN
                and existing_intent.actor_id == actor
                and isinstance(existing_intent.payload, dict)
                and existing_intent.payload.get("text") == text.strip()
            )
            if not same_intent:
                raise OutboxIdempotencyConflict("idempotency_key_reused_with_different_intent")
            outbox_id = existing_intent.id
            await session.commit()
        else:
            outbox_id = None

        work = (
            await session.execute(
                select(models.HumanWorkItem)
                .where(
                    models.HumanWorkItem.conversation_id == conversation_id,
                    models.HumanWorkItem.status.in_(["WAITING", "CLAIMED"]),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if work is not None:
            require_work_conversation_tenant(work, conversation_tenant_id=conversation.tenant_id)
        if outbox_id is None and work is None:
            work = await ensure_open_human_work_item(
                session,
                tenant_id=conversation.tenant_id,
                conversation_id=conversation.id,
                reason_code="ADMIN_MANUAL",
            )
        if outbox_id is None and work_item_id is not None and work.id != work_item_id:
            raise HumanWorkflowConflict("human_work_item_scope_mismatch")
        if outbox_id is None and expected_version is not None and work.version != expected_version:
            raise HumanWorkflowConflict("human_work_item_version_conflict")
        if outbox_id is None and work.status == "WAITING":
            work.status = "CLAIMED"
            work.assigned_user_id = user_id
            work.assigned_actor = actor
            work.claimed_at = datetime.now(UTC)
            work.version += 1
        elif outbox_id is None:
            _require_assignee(work, actor=actor, allow_override=allow_override)

        if outbox_id is None:
            state = await session.get(models.AutomationState, conversation_id, with_for_update=True)
            if state is None or state.state == "CLOSED":
                raise HumanWorkflowConflict("conversation_not_sendable")
            if state.state != "HUMAN_ACTIVE":
                state.state = "HUMAN_ACTIVE"
                state.state_version += 1
                state.human_agent_id = actor
                state.state_changed_reason = "admin_human_reply"

        if outbox_id is None:
            await session.execute(
                update(models.OutboxMessage)
                .where(
                    models.OutboxMessage.conversation_id == conversation_id,
                    models.OutboxMessage.status.in_(["PENDING", "FAILED"]),
                    models.OutboxMessage.actor_kind == OutboxActor.BOT,
                )
                .values(status="CANCELLED", last_error_code="TAKEOVER")
            )
            outbox_id = await create_or_get_outbox_intent(
                session,
                conversation_id=conversation.id,
                platform_account_id=conversation.platform_account_id,
                reply_to_message_id=reply_to_message_id,
                text=text,
                origin_kind=OutboxOrigin.MANUAL_REPLY,
                actor_kind=OutboxActor.ADMIN_HUMAN,
                actor_id=actor,
                idempotency_key=idempotency_key,
            )
            session.add(
                models.AuditLog(
                    tenant_id=conversation.tenant_id,
                    category="human_work",
                    actor=actor,
                    action="SEND_REPLY",
                    subject_type="conversation",
                    subject_id=str(conversation.id),
                    detail={
                        "human_work_item_id": str(work.id),
                        "outbox_id": str(outbox_id),
                        "reply_to_message_id": str(reply_to_message_id),
                    },
                )
            )
            await session.commit()

    from social_reply.application.message_delivery.actors import deliver_outbox_message
    from social_reply.application.message_delivery.outbox import deliver_outbox

    await dispatch_actor(
        deliver_outbox_message,
        str(outbox_id),
        inline=lambda: deliver_outbox(str(outbox_id)),
    )
    return outbox_id
