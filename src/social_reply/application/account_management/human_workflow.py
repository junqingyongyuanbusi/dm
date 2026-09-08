from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.application.account_management.access import (
    lock_session_authorities,
    lock_user_authority,
    user_can_access_account,
)
from social_reply.application.account_management.auth import (
    Principal,
    authenticated_principal_context,
    principal_from_session_row,
)
from social_reply.application.handoff_notifications.service import (
    advance_handoff_notification_for_work,
    lock_handoff_notification_action,
)
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


@dataclass(frozen=True)
class _HumanWorkSnapshot:
    work_id: uuid.UUID
    tenant_id: str
    conversation_id: uuid.UUID
    status: str
    version: int
    assigned_user_id: uuid.UUID | None
    assigned_actor: str | None
    assigned_session_id: uuid.UUID | None


@dataclass(frozen=True)
class _StaffSnapshot:
    user_id: uuid.UUID
    tenant_id: str
    username: str
    status: str
    role: str
    must_change_password: bool


def _work_snapshot(work: models.HumanWorkItem) -> _HumanWorkSnapshot:
    return _HumanWorkSnapshot(
        work_id=work.id,
        tenant_id=work.tenant_id,
        conversation_id=work.conversation_id,
        status=work.status,
        version=work.version,
        assigned_user_id=work.assigned_user_id,
        assigned_actor=work.assigned_actor,
        assigned_session_id=work.assigned_session_id,
    )


async def _read_work_snapshot(
    session: AsyncSession,
    *,
    work_item_id: uuid.UUID,
    allowed_tenants: frozenset[str],
) -> _HumanWorkSnapshot:
    work = await session.scalar(
        select(models.HumanWorkItem)
        .where(
            models.HumanWorkItem.id == work_item_id,
            models.HumanWorkItem.tenant_id.in_(allowed_tenants),
        )
        .execution_options(populate_existing=True)
    )
    if work is None:
        raise HumanWorkflowError("human_work_item_not_found")
    return _work_snapshot(work)


async def _read_staff_snapshot(
    session: AsyncSession,
    user_id: uuid.UUID,
) -> _StaffSnapshot | None:
    user = await session.scalar(
        select(models.AdminUser)
        .where(models.AdminUser.id == user_id)
        .execution_options(populate_existing=True)
    )
    if user is None:
        return None
    return _StaffSnapshot(
        user_id=user.id,
        tenant_id=user.tenant_id,
        username=user.username,
        status=user.status,
        role=user.role,
        must_change_password=user.must_change_password,
    )


def _staff_snapshot_matches(snapshot: _StaffSnapshot, user: models.AdminUser) -> bool:
    return (
        snapshot.user_id == user.id
        and snapshot.tenant_id == user.tenant_id
        and snapshot.username == user.username
        and snapshot.status == user.status
        and snapshot.role == user.role
        and snapshot.must_change_password == user.must_change_password
    )


def _work_snapshot_matches(snapshot: _HumanWorkSnapshot, work: models.HumanWorkItem) -> bool:
    return _work_snapshot(work) == snapshot


async def _lock_staff_ids(session: AsyncSession, staff_ids: set[uuid.UUID]) -> None:
    for staff_id in sorted(staff_ids, key=str):
        await lock_user_authority(session, staff_id)


async def _lock_session_ids(session: AsyncSession, session_ids: set[uuid.UUID]) -> None:
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


async def _refresh_principal(
    session: AsyncSession,
    principal: Principal | None,
    *,
    work_item_id: uuid.UUID | None = None,
    expected_action: str | None = None,
) -> Principal | None:
    if principal is None:
        return None
    if not principal.is_feishu_action:
        if principal.session_id is None:
            raise HumanWorkflowError("principal_session_invalid")
        current = await principal_from_session_row(session, principal.session_id, for_update=True)
        if current is None:
            raise HumanWorkflowError("principal_session_invalid")
        return current

    proof = principal.action_proof
    if (
        proof is None
        or principal.session_id is not None
        or principal.user_id is None
        or expected_action not in {"CLAIM", "RESOLVE"}
        or proof.action != expected_action
    ):
        raise HumanWorkflowError("feishu_action_proof_invalid")
    return principal


async def _verify_feishu_action_proof(
    session: AsyncSession,
    *,
    principal: Principal,
    work: models.HumanWorkItem,
    account: models.PlatformAccount,
    notification_account: models.PlatformAccount | None = None,
    expected_action: str | None,
) -> None:
    proof = principal.action_proof
    if (
        proof is None
        or not principal.is_feishu_action
        or expected_action not in {"CLAIM", "RESOLVE"}
        or proof.action != expected_action
        or proof.customer_account_id != account.id
        or proof.tenant_id != work.tenant_id
        or proof.tenant_id != principal.tenant_id
        or notification_account is None
        or notification_account.id != proof.notification_account_id
        or notification_account.tenant_id != work.tenant_id
        or notification_account.platform != "feishu"
        or notification_account.status != "active"
    ):
        raise HumanWorkflowError("feishu_action_proof_invalid")
    receipt = await session.scalar(
        select(models.FeishuCardActionReceipt)
        .where(models.FeishuCardActionReceipt.id == proof.receipt_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if (
        receipt is None
        or receipt.outcome != "PROCESSING"
        or receipt.tenant_id != proof.tenant_id
        or receipt.feishu_platform_account_id != proof.notification_account_id
        or receipt.notification_intent_id != proof.notification_intent_id
        or receipt.operator_open_id != proof.operator_open_id
        or receipt.action != proof.action
        or receipt.request_digest != proof.request_digest
    ):
        raise HumanWorkflowError("feishu_action_proof_invalid")
    intent = await session.scalar(
        select(models.HandoffNotificationIntent)
        .where(
            models.HandoffNotificationIntent.id == proof.notification_intent_id,
            models.HandoffNotificationIntent.tenant_id == proof.tenant_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if (
        intent is None
        or intent.human_work_item_id != work.id
        or intent.conversation_id != work.conversation_id
        or intent.feishu_platform_account_id != proof.notification_account_id
        or intent.provider_message_id != proof.provider_message_id
        or intent.action_nonce != proof.action_nonce
    ):
        raise HumanWorkflowError("feishu_action_proof_invalid")
    operator = await session.scalar(
        select(models.FeishuHandoffOperator)
        .where(
            models.FeishuHandoffOperator.tenant_id == proof.tenant_id,
            models.FeishuHandoffOperator.feishu_platform_account_id
            == proof.notification_account_id,
            models.FeishuHandoffOperator.operator_open_id == proof.operator_open_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if (
        operator is None
        or operator.status != "ACTIVE"
        or operator.admin_user_id != principal.user_id
        or (proof.action == "CLAIM" and not operator.can_claim)
        or (proof.action == "RESOLVE" and not operator.can_resolve)
    ):
        raise HumanWorkflowError("feishu_action_operator_invalid")


async def _lock_work_context(
    session: AsyncSession,
    *,
    work_item_id: uuid.UUID,
    allowed_tenants: frozenset[str],
    principal: Principal | None = None,
    owner_user_id: uuid.UUID | None = None,
    authority_user_id: uuid.UUID | None = None,
    authority_user_ids: tuple[uuid.UUID, ...] = (),
    expected_action: str | None = None,
) -> tuple[
    models.Conversation,
    models.PlatformAccount,
    models.HumanWorkItem,
    models.AutomationState,
    Principal | None,
    dict[uuid.UUID, models.AdminUser],
]:
    snapshot = await _read_work_snapshot(
        session,
        work_item_id=work_item_id,
        allowed_tenants=allowed_tenants,
    )
    staff_ids = {
        value
        for value in (
            authority_user_id,
            snapshot.assigned_user_id,
            *(authority_user_ids or ()),
            principal.user_id if principal is not None else None,
        )
        if value is not None
    }
    await _lock_staff_ids(session, staff_ids)
    session_ids = {
        value
        for value in (
            snapshot.assigned_session_id,
            principal.session_id if principal is not None else None,
        )
        if value is not None
    }
    await _lock_session_ids(session, session_ids)
    current_principal = await _refresh_principal(
        session,
        principal,
        work_item_id=work_item_id,
        expected_action=expected_action,
    )
    await acquire_conversation_delivery_xact_lock(session, snapshot.conversation_id)

    conversation = await session.scalar(
        select(models.Conversation)
        .where(
            models.Conversation.id == snapshot.conversation_id,
            models.Conversation.tenant_id.in_(allowed_tenants),
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if conversation is None:
        raise HumanWorkflowError("conversation_not_found")
    if snapshot.tenant_id != conversation.tenant_id:
        raise HumanWorkflowConflict("human_work_item_tenant_mismatch")
    account_ids = {conversation.platform_account_id}
    notification_account_id = None
    if current_principal is not None and current_principal.is_feishu_action:
        proof = current_principal.action_proof
        notification_account_id = proof.notification_account_id if proof is not None else None
        if notification_account_id is not None:
            account_ids.add(notification_account_id)
    account_rows = {
        account_row.id: account_row
        for account_row in (
            await session.scalars(
                select(models.PlatformAccount)
                .where(
                    models.PlatformAccount.id.in_(account_ids),
                    models.PlatformAccount.tenant_id == conversation.tenant_id,
                )
                .execution_options(populate_existing=True)
                .order_by(models.PlatformAccount.id)
                .with_for_update()
            )
        ).all()
    }
    account = account_rows.get(conversation.platform_account_id)
    if account is None:
        raise HumanWorkflowError("conversation_account_scope_mismatch")
    notification_account = (
        account_rows.get(notification_account_id) if notification_account_id is not None else None
    )
    if current_principal is not None and current_principal.is_feishu_action:
        if (
            notification_account is None
            or notification_account.platform != "feishu"
            or notification_account.status != "active"
        ):
            raise HumanWorkflowError("feishu_action_proof_invalid")
    if owner_user_id is not None and account.owner_user_id != owner_user_id:
        raise HumanWorkflowError("human_work_item_not_found")

    staff_rows = {
        user.id: user
        for user in (
            await session.scalars(
                select(models.AdminUser)
                .where(models.AdminUser.id.in_(staff_ids))
                .execution_options(populate_existing=True)
                .with_for_update()
                .order_by(models.AdminUser.id)
            )
        ).all()
    }
    work = await session.scalar(
        select(models.HumanWorkItem)
        .where(models.HumanWorkItem.id == work_item_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if work is None:
        raise HumanWorkflowError("human_work_item_not_found")
    require_work_conversation_tenant(work, conversation_tenant_id=conversation.tenant_id)
    if work.conversation_id != conversation.id:
        raise HumanWorkflowConflict("human_work_item_scope_mismatch")
    if not _work_snapshot_matches(snapshot, work):
        raise HumanWorkflowConflict("human_work_item_snapshot_conflict")
    state = await session.get(
        models.AutomationState,
        conversation.id,
        with_for_update=True,
        populate_existing=True,
    )
    if state is None:
        raise HumanWorkflowError("automation_state_not_found")
    if current_principal is not None and current_principal.is_feishu_action:
        await _verify_feishu_action_proof(
            session,
            principal=current_principal,
            work=work,
            account=account,
            notification_account=notification_account,
            expected_action=expected_action,
        )
    return conversation, account, work, state, current_principal, staff_rows


async def _lock_conversation_context(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    allowed_tenants: frozenset[str],
    principal: Principal,
    owner_user_id: uuid.UUID | None = None,
    expected_action: str | None = None,
) -> tuple[
    models.Conversation,
    models.PlatformAccount,
    models.HumanWorkItem | None,
    models.AutomationState,
    Principal,
    dict[uuid.UUID, models.AdminUser],
]:
    identity = (
        await session.execute(
            select(
                models.Conversation.tenant_id,
                models.Conversation.platform_account_id,
            ).where(
                models.Conversation.id == conversation_id,
                models.Conversation.tenant_id.in_(allowed_tenants),
            )
        )
    ).one_or_none()
    if identity is None:
        raise HumanWorkflowError("conversation_not_found")
    tenant_id, platform_account_id = identity
    initial_work = await session.scalar(
        select(models.HumanWorkItem)
        .where(
            models.HumanWorkItem.conversation_id == conversation_id,
            models.HumanWorkItem.tenant_id == tenant_id,
            models.HumanWorkItem.status.in_(["WAITING", "CLAIMED"]),
        )
        .execution_options(populate_existing=True)
    )
    initial_snapshot = _work_snapshot(initial_work) if initial_work is not None else None
    staff_ids = {principal.user_id} if principal.user_id is not None else set()
    if initial_snapshot is not None and initial_snapshot.assigned_user_id is not None:
        staff_ids.add(initial_snapshot.assigned_user_id)
    await _lock_staff_ids(session, staff_ids)
    session_ids = {
        value
        for value in (
            initial_snapshot.assigned_session_id if initial_snapshot is not None else None,
            principal.session_id,
        )
        if value is not None
    }
    await _lock_session_ids(session, session_ids)
    current = await _refresh_principal(
        session,
        principal,
        work_item_id=initial_snapshot.work_id if initial_snapshot is not None else None,
        expected_action=expected_action,
    )
    if current is None:
        raise HumanWorkflowError("human_principal_required")
    await acquire_conversation_delivery_xact_lock(session, conversation_id)
    conversation = await session.scalar(
        select(models.Conversation)
        .where(
            models.Conversation.id == conversation_id,
            models.Conversation.tenant_id.in_(allowed_tenants),
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if conversation is None or conversation.tenant_id != tenant_id:
        raise HumanWorkflowConflict("conversation_tenant_mismatch")
    if conversation.platform_account_id != platform_account_id:
        raise HumanWorkflowConflict("conversation_snapshot_conflict")
    account = await session.scalar(
        select(models.PlatformAccount)
        .where(
            models.PlatformAccount.id == platform_account_id,
            models.PlatformAccount.tenant_id == tenant_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if account is None:
        raise HumanWorkflowError("conversation_account_scope_mismatch")
    if owner_user_id is not None and account.owner_user_id != owner_user_id:
        raise HumanWorkflowError("conversation_not_found")
    staff_rows = {
        user.id: user
        for user in (
            await session.scalars(
                select(models.AdminUser)
                .where(models.AdminUser.id.in_(staff_ids))
                .execution_options(populate_existing=True)
                .with_for_update()
                .order_by(models.AdminUser.id)
            )
        ).all()
    }
    work = await session.scalar(
        select(models.HumanWorkItem)
        .where(
            models.HumanWorkItem.conversation_id == conversation_id,
            models.HumanWorkItem.tenant_id == tenant_id,
            models.HumanWorkItem.status.in_(["WAITING", "CLAIMED"]),
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if initial_snapshot is None:
        if work is not None:
            raise HumanWorkflowConflict("human_work_item_snapshot_conflict")
    elif work is None or not _work_snapshot_matches(initial_snapshot, work):
        raise HumanWorkflowConflict("human_work_item_snapshot_conflict")
    state = await session.get(
        models.AutomationState,
        conversation_id,
        with_for_update=True,
        populate_existing=True,
    )
    if state is None:
        raise HumanWorkflowError("automation_state_not_found")
    return conversation, account, work, state, current, staff_rows


async def _authorize_human_actor(
    *,
    principal: Principal | None,
    current_principal: Principal | None,
    staff_user: models.AdminUser | None,
    account: models.PlatformAccount,
    tenant_id: str,
    actor: str,
    user_id: uuid.UUID | None,
) -> tuple[Principal, models.AdminUser | None, uuid.UUID | None, str]:
    if principal is None or current_principal is None:
        raise HumanWorkflowError("human_principal_required")
    current = current_principal
    if (
        tenant_id not in current.allowed_tenants
        or current.tenant_id not in {None, tenant_id}
        or current.must_change_password
        or not current.can_access_account(account)
    ):
        raise HumanWorkflowError("human_account_access_denied")
    if actor != current.actor:
        raise HumanWorkflowError("human_actor_identity_mismatch")
    if user_id not in {None, current.user_id}:
        raise HumanWorkflowError("human_user_identity_mismatch")

    if current.user_id is None:
        if not current.is_superadmin or current.session_id is None or current.is_feishu_action:
            raise HumanWorkflowError("human_staff_identity_required")
        if user_id is not None:
            raise HumanWorkflowError("human_user_identity_mismatch")
        return current, None, None, current.actor

    if staff_user is None:
        raise HumanWorkflowError("human_staff_identity_required")
    if (
        staff_user.id != current.user_id
        or staff_user.tenant_id != tenant_id
        or staff_user.username != current.username
        or staff_user.status != "active"
        or staff_user.role not in {"USER", "WORKSPACE_ADMIN"}
        or staff_user.must_change_password != current.must_change_password
        or not user_can_access_account(staff_user, account)
    ):
        raise HumanWorkflowError("human_account_access_denied")
    expected_actor = f"user:{staff_user.username}"
    if actor != expected_actor:
        raise HumanWorkflowError("human_actor_identity_mismatch")
    if current.is_feishu_action:
        proof = current.action_proof
        if proof is None or proof.customer_account_id != account.id:
            raise HumanWorkflowError("feishu_action_proof_invalid")
    return current, staff_user, staff_user.id, expected_actor


async def claim_human_work_item_in_session(
    session: AsyncSession,
    *,
    work_item_id: uuid.UUID,
    allowed_tenants: frozenset[str],
    actor: str,
    user_id: uuid.UUID | None,
    expected_version: int,
    notification_public_id: uuid.UUID | None = None,
    expected_card_revision: int | None = None,
    expected_action_nonce: uuid.UUID | None = None,
    principal: Principal | None = None,
    owner_user_id: uuid.UUID | None = None,
) -> tuple[
    models.Conversation,
    models.PlatformAccount,
    models.HumanWorkItem,
    models.AutomationState,
]:
    (
        conversation,
        account,
        work,
        state,
        current_principal,
        staff_rows,
    ) = await _lock_work_context(
        session,
        work_item_id=work_item_id,
        allowed_tenants=allowed_tenants,
        principal=principal,
        owner_user_id=owner_user_id,
        authority_user_id=principal.user_id if principal is not None else user_id,
        expected_action="CLAIM",
    )
    current_principal, _staff, effective_user_id, effective_actor = await _authorize_human_actor(
        principal=principal,
        current_principal=current_principal,
        staff_user=(
            staff_rows.get(current_principal.user_id)
            if current_principal is not None and current_principal.user_id is not None
            else None
        ),
        account=account,
        tenant_id=conversation.tenant_id,
        actor=actor,
        user_id=user_id,
    )
    if account.status != "active":
        raise HumanWorkflowConflict("human_account_access_denied")
    if notification_public_id is not None:
        if expected_card_revision is None or expected_action_nonce is None:
            raise HumanWorkflowError("handoff_notification_action_fence_incomplete")
        await lock_handoff_notification_action(
            session,
            work=work,
            notification_public_id=notification_public_id,
            expected_card_revision=expected_card_revision,
            expected_action_nonce=expected_action_nonce,
        )
    if work.status != "WAITING" or work.version != expected_version:
        raise HumanWorkflowConflict("human_work_item_version_conflict")
    if state.state not in {"HANDOFF_PENDING", "HUMAN_ACTIVE"}:
        raise HumanWorkflowConflict("conversation_not_handoff_pending")

    now = datetime.now(UTC)
    work.status = "CLAIMED"
    work.assigned_user_id = effective_user_id
    work.assigned_actor = effective_actor
    work.assigned_session_id = current_principal.session_id if effective_user_id is None else None
    work.claimed_at = now
    work.version += 1
    source = state.state
    state.state = "HUMAN_ACTIVE"
    state.state_version += 1
    state.human_agent_id = effective_actor
    state.state_changed_reason = "human_work_claimed"
    session.add(
        models.AuditLog(
            tenant_id=conversation.tenant_id,
            category="state_transition",
            actor=effective_actor,
            action="HUMAN_ACTIVE",
            subject_type="conversation",
            subject_id=str(conversation.id),
            detail={"reason": "human_work_claimed", "source": source},
        )
    )
    await session.execute(
        update(models.OutboxMessage)
        .where(
            models.OutboxMessage.conversation_id == conversation.id,
            models.OutboxMessage.status.in_(["PENDING", "FAILED"]),
            models.OutboxMessage.actor_kind == OutboxActor.BOT,
            models.OutboxMessage.origin_kind == OutboxOrigin.DECISION,
        )
        .values(status="CANCELLED", last_error_code="TAKEOVER")
    )
    await advance_handoff_notification_for_work(session, work=work)
    session.add(
        models.AuditLog(
            tenant_id=work.tenant_id,
            category="human_work",
            actor=effective_actor,
            action="CLAIM",
            subject_type="human_work_item",
            subject_id=str(work.id),
            detail={"conversation_id": str(work.conversation_id)},
        )
    )
    return conversation, account, work, state


async def claim_human_work_item(
    *,
    work_item_id: uuid.UUID,
    allowed_tenants: frozenset[str],
    actor: str,
    user_id: uuid.UUID | None,
    expected_version: int,
    principal: Principal | None = None,
    owner_user_id: uuid.UUID | None = None,
) -> None:
    async with get_session_factory()() as session:
        await claim_human_work_item_in_session(
            session,
            work_item_id=work_item_id,
            allowed_tenants=allowed_tenants,
            actor=actor,
            user_id=user_id,
            expected_version=expected_version,
            principal=principal,
            owner_user_id=owner_user_id,
        )
        await session.commit()


def _require_assignee(
    work: models.HumanWorkItem,
    *,
    actor: str,
    user_id: uuid.UUID | None,
    principal: Principal | None,
    allow_override: bool,
) -> None:
    if work.status != "CLAIMED":
        raise HumanWorkflowConflict("human_work_item_not_claimed")
    if principal is not None:
        if allow_override and principal.is_workspace_admin:
            return
        if principal.user_id is not None:
            assigned = work.assigned_user_id == principal.user_id
        else:
            assigned = (
                principal.is_superadmin
                and principal.session_id is not None
                and work.assigned_user_id is None
                and work.assigned_session_id == principal.session_id
                and work.assigned_actor == principal.actor
            )
    elif user_id is not None:
        assigned = work.assigned_user_id == user_id
    else:
        assigned = False
    if not assigned:
        raise HumanWorkflowConflict("human_work_item_assigned_to_another_user")


async def _cancel_superseded_human_replies(
    session: AsyncSession,
    *,
    work: models.HumanWorkItem,
    principal: Principal,
) -> None:
    if work.status != "CLAIMED":
        return
    if work.assigned_user_id is not None:
        identity = models.OutboxMessage.initiator_user_id == work.assigned_user_id
    elif work.assigned_session_id is not None:
        identity = and_(
            models.OutboxMessage.initiator_user_id.is_(None),
            models.OutboxMessage.initiator_session_id == work.assigned_session_id,
        )
    else:
        return
    cancelled_ids = list(
        await session.scalars(
            update(models.OutboxMessage)
            .where(
                models.OutboxMessage.tenant_id == work.tenant_id,
                models.OutboxMessage.conversation_id == work.conversation_id,
                models.OutboxMessage.human_work_item_version == work.version,
                models.OutboxMessage.status.in_(("PENDING", "FAILED")),
                models.OutboxMessage.origin_kind == OutboxOrigin.MANUAL_REPLY,
                models.OutboxMessage.actor_kind == OutboxActor.ADMIN_HUMAN,
                identity,
            )
            .values(
                status="CANCELLED",
                last_error_code="HUMAN_WORK_TRANSFERRED",
            )
            .returning(models.OutboxMessage.id)
        )
    )
    for outbox_id in cancelled_ids:
        session.add(
            models.AuditLog(
                tenant_id=work.tenant_id,
                category="human_work",
                actor=principal.actor,
                action="CANCEL_SUPERSEDED_HUMAN_REPLY",
                subject_type="outbox_message",
                subject_id=str(outbox_id),
                detail={
                    "work_item_id": str(work.id),
                    "previous_work_version": work.version,
                    "previous_user_id": str(work.assigned_user_id)
                    if work.assigned_user_id
                    else None,
                    "previous_session_id": str(work.assigned_session_id)
                    if work.assigned_session_id
                    else None,
                    "actor_user_id": str(principal.user_id) if principal.user_id else None,
                    "actor_session_id": str(principal.session_id) if principal.session_id else None,
                },
            )
        )


async def has_unfinished_human_send(
    session: AsyncSession, *, tenant_id: str, conversation_id: uuid.UUID
) -> bool:
    return (
        await session.scalar(
            select(models.OutboxMessage.id)
            .where(
                models.OutboxMessage.tenant_id == tenant_id,
                models.OutboxMessage.conversation_id == conversation_id,
                # Unrecognized legacy states must not be treated as completed sends.
                models.OutboxMessage.status.not_in(("SENT", "CANCELLED")),
                or_(
                    models.OutboxMessage.actor_kind == OutboxActor.ADMIN_HUMAN,
                    models.OutboxMessage.origin_kind.in_(
                        (OutboxOrigin.MANUAL_REPLY, OutboxOrigin.DRAFT_APPROVAL)
                    ),
                ),
            )
            .limit(1)
        )
        is not None
    )


async def resolve_human_work_item_in_session(
    session: AsyncSession,
    *,
    work_item_id: uuid.UUID,
    allowed_tenants: frozenset[str],
    actor: str,
    user_id: uuid.UUID | None = None,
    expected_version: int,
    allow_override: bool,
    resolution_evidence: str,
    resolution_outbox_id: uuid.UUID | None = None,
    notification_public_id: uuid.UUID | None = None,
    expected_card_revision: int | None = None,
    expected_action_nonce: uuid.UUID | None = None,
    principal: Principal | None = None,
    owner_user_id: uuid.UUID | None = None,
) -> tuple[
    models.Conversation,
    models.PlatformAccount,
    models.HumanWorkItem,
    models.AutomationState,
]:
    if resolution_evidence not in {
        "REPLY_CORE_CONFIRMED",
        "FEISHU_OPERATOR_ATTESTED",
        "ADMIN_OPERATOR_ATTESTED",
        "SUPERVISOR_OVERRIDE",
    }:
        raise HumanWorkflowError("resolution_evidence_invalid")
    (
        conversation,
        account,
        work,
        state,
        current_principal,
        staff_rows,
    ) = await _lock_work_context(
        session,
        work_item_id=work_item_id,
        allowed_tenants=allowed_tenants,
        principal=principal,
        owner_user_id=owner_user_id,
        authority_user_id=principal.user_id if principal is not None else user_id,
        expected_action="RESOLVE",
    )
    current_principal, _staff, effective_user_id, effective_actor = await _authorize_human_actor(
        principal=principal,
        current_principal=current_principal,
        staff_user=(
            staff_rows.get(current_principal.user_id)
            if current_principal is not None and current_principal.user_id is not None
            else None
        ),
        account=account,
        tenant_id=conversation.tenant_id,
        actor=actor,
        user_id=user_id,
    )
    if notification_public_id is not None:
        if expected_card_revision is None or expected_action_nonce is None:
            raise HumanWorkflowError("handoff_notification_action_fence_incomplete")
        await lock_handoff_notification_action(
            session,
            work=work,
            notification_public_id=notification_public_id,
            expected_card_revision=expected_card_revision,
            expected_action_nonce=expected_action_nonce,
        )
    if work.version != expected_version:
        raise HumanWorkflowConflict("human_work_item_version_conflict")
    _require_assignee(
        work,
        actor=effective_actor,
        user_id=effective_user_id,
        principal=current_principal,
        allow_override=allow_override,
    )
    if state.state not in {"HANDOFF_PENDING", "HUMAN_ACTIVE"}:
        raise HumanWorkflowConflict("conversation_not_human_active")
    if account.automation_default not in {"BOT_DRAFT_ONLY", "BOT_ACTIVE"}:
        raise HumanWorkflowError("account_automation_default_invalid")

    if await has_unfinished_human_send(
        session, tenant_id=conversation.tenant_id, conversation_id=conversation.id
    ):
        raise HumanWorkflowConflict("human_reply_delivery_pending")
    target = account.automation_default
    reason = "human_work_resolved_account_policy"
    if not get_settings().automation_default_allowed(account.platform, target):
        target = "BOT_DRAFT_ONLY"
        reason = "human_work_resolved_platform_fallback"

    source = state.state
    work.status = "RESOLVED"
    work.resolved_at = datetime.now(UTC)
    work.resolved_actor = effective_actor
    work.resolution_evidence = resolution_evidence
    work.resolution_outbox_id = resolution_outbox_id
    work.version += 1
    state.state = target
    state.state_version += 1
    state.human_agent_id = None
    state.state_changed_reason = reason
    await advance_handoff_notification_for_work(session, work=work)
    session.add_all(
        [
            models.AuditLog(
                tenant_id=work.tenant_id,
                category="human_work",
                actor=effective_actor,
                action="RESOLVE",
                subject_type="human_work_item",
                subject_id=str(work.id),
                detail={
                    "conversation_id": str(work.conversation_id),
                    "resolution_evidence": resolution_evidence,
                    "resolution_outbox_id": (
                        str(resolution_outbox_id) if resolution_outbox_id else None
                    ),
                },
            ),
            models.AuditLog(
                tenant_id=conversation.tenant_id,
                category="state_transition",
                actor=effective_actor,
                action=target,
                subject_type="conversation",
                subject_id=str(conversation.id),
                detail={
                    "reason": reason,
                    "source": source,
                    "account_policy": account.automation_default,
                },
            ),
        ]
    )
    return conversation, account, work, state


async def resolve_human_work_item(
    *,
    work_item_id: uuid.UUID,
    allowed_tenants: frozenset[str],
    actor: str,
    user_id: uuid.UUID | None = None,
    expected_version: int,
    allow_override: bool,
    resolution_evidence: str = "ADMIN_OPERATOR_ATTESTED",
    resolution_outbox_id: uuid.UUID | None = None,
    principal: Principal | None = None,
    owner_user_id: uuid.UUID | None = None,
) -> None:
    async with get_session_factory()() as session:
        await resolve_human_work_item_in_session(
            session,
            work_item_id=work_item_id,
            allowed_tenants=allowed_tenants,
            actor=actor,
            user_id=user_id,
            expected_version=expected_version,
            allow_override=allow_override,
            resolution_evidence=resolution_evidence,
            resolution_outbox_id=resolution_outbox_id,
            principal=principal,
            owner_user_id=owner_user_id,
        )
        await session.commit()


async def transfer_human_work_item(
    *,
    work_item_id: uuid.UUID,
    allowed_tenants: frozenset[str],
    actor: str,
    user_id: uuid.UUID | None,
    target_user_id: uuid.UUID,
    expected_version: int,
    principal: Principal | None = None,
) -> None:
    async with get_session_factory()() as session:
        target_snapshot = await _read_staff_snapshot(session, target_user_id)
        if target_snapshot is None:
            raise HumanWorkflowError("human_transfer_target_not_accessible")
        (
            conversation,
            account,
            work,
            state,
            current_principal,
            staff_rows,
        ) = await _lock_work_context(
            session,
            work_item_id=work_item_id,
            allowed_tenants=allowed_tenants,
            principal=principal,
            authority_user_id=principal.user_id if principal is not None else user_id,
            authority_user_ids=(target_user_id,),
        )
        (
            current_principal,
            _staff,
            effective_user_id,
            effective_actor,
        ) = await _authorize_human_actor(
            principal=principal,
            current_principal=current_principal,
            staff_user=(
                staff_rows.get(current_principal.user_id)
                if current_principal is not None and current_principal.user_id is not None
                else None
            ),
            account=account,
            tenant_id=conversation.tenant_id,
            actor=actor,
            user_id=user_id,
        )
        if account.status != "active":
            raise HumanWorkflowConflict("human_account_access_denied")
        target = staff_rows.get(target_user_id)
        if target is None or not _staff_snapshot_matches(target_snapshot, target):
            raise HumanWorkflowConflict("human_transfer_target_snapshot_conflict")
        if (
            target.tenant_id != conversation.tenant_id
            or target.status != "active"
            or target.role not in {"USER", "WORKSPACE_ADMIN"}
            or not user_can_access_account(target, account)
        ):
            raise HumanWorkflowError("human_transfer_target_not_accessible")
        if state.state not in {"HANDOFF_PENDING", "HUMAN_ACTIVE"}:
            raise HumanWorkflowConflict("conversation_not_handoff_pending")
        is_workspace_admin = current_principal.is_workspace_admin
        if not is_workspace_admin:
            if work.status != "CLAIMED" or work.assigned_user_id != effective_user_id:
                raise HumanWorkflowConflict("human_work_item_assigned_to_another_user")
        if work.status not in {"WAITING", "CLAIMED"}:
            raise HumanWorkflowConflict("human_work_item_not_transferable")
        if work.version != expected_version:
            raise HumanWorkflowConflict("human_work_item_version_conflict")
        if work.assigned_user_id == target.id:
            raise HumanWorkflowConflict("human_transfer_target_is_current_assignee")

        await _cancel_superseded_human_replies(session, work=work, principal=current_principal)
        work.status = "CLAIMED"
        work.assigned_user_id = target.id
        work.assigned_actor = f"user:{target.username}"
        work.assigned_session_id = None
        work.claimed_at = work.claimed_at or datetime.now(UTC)
        work.version += 1
        if state.state != "HUMAN_ACTIVE":
            state.state = "HUMAN_ACTIVE"
            state.state_version += 1
        state.human_agent_id = work.assigned_actor
        state.state_changed_reason = "human_work_transferred"
        await advance_handoff_notification_for_work(session, work=work, force_refresh=True)
        session.add(
            models.AuditLog(
                tenant_id=work.tenant_id,
                category="human_work",
                actor=effective_actor,
                action="TRANSFER",
                subject_type="human_work_item",
                subject_id=str(work.id),
                detail={
                    "target_user_id": str(target.id),
                    "target_actor": work.assigned_actor,
                    "version": work.version,
                },
            )
        )
        await session.commit()


async def start_human_reception(
    *,
    conversation_id: uuid.UUID,
    principal: Principal | None,
    expected_state: str | None = None,
) -> uuid.UUID:
    """Explicitly start human reception for a conversation.

    This is the only normal-conversation entry point that creates a work item.  It
    revalidates account access, pauses automation, and claims the item in one transaction;
    the conversation delivery lock makes concurrent starters resolve by CAS rather than
    producing two assignees.
    """
    if principal is None:
        raise HumanWorkflowError("human_principal_required")
    async with get_session_factory()() as session:
        (
            conversation,
            account,
            work,
            state,
            current_principal,
            staff_rows,
        ) = await _lock_conversation_context(
            session,
            conversation_id=conversation_id,
            allowed_tenants=principal.allowed_tenants,
            principal=principal,
            expected_action="START",
        )
        (
            current_principal,
            _staff,
            effective_user_id,
            effective_actor,
        ) = await _authorize_human_actor(
            principal=principal,
            current_principal=current_principal,
            staff_user=(
                staff_rows.get(current_principal.user_id)
                if current_principal.user_id is not None
                else None
            ),
            account=account,
            tenant_id=conversation.tenant_id,
            actor=current_principal.actor,
            user_id=current_principal.user_id,
        )
        if account.status != "active":
            raise HumanWorkflowConflict("human_account_access_denied")
        if expected_state is not None and state.state != expected_state:
            raise HumanWorkflowConflict("automation_state_version_conflict")
        if state.state == "CLOSED":
            raise HumanWorkflowConflict("conversation_not_sendable")
        if work is None:
            if state.state not in {"BOT_DRAFT_ONLY", "BOT_ACTIVE", "BOT_COOLDOWN"}:
                raise HumanWorkflowConflict("conversation_reception_state_invalid")
            work = models.HumanWorkItem(
                tenant_id=conversation.tenant_id,
                conversation_id=conversation.id,
                status="WAITING",
                reason_code="ADMIN_START_RECEPTION",
                priority=0,
                due_at=datetime.now(UTC) + timedelta(minutes=30),
                version=1,
            )
            session.add(work)
            await session.flush()
        elif state.state not in {"HANDOFF_PENDING", "HUMAN_ACTIVE"}:
            raise HumanWorkflowConflict("conversation_reception_state_invalid")

        assigned_to_current = (
            work.assigned_user_id == effective_user_id
            if effective_user_id is not None
            else (
                current_principal.is_superadmin
                and current_principal.session_id is not None
                and work.assigned_user_id is None
                and work.assigned_session_id == current_principal.session_id
                and work.assigned_actor == effective_actor
            )
        )
        if work.status == "CLAIMED" and assigned_to_current:
            raise HumanWorkflowConflict("human_reception_already_started")
        if work.status == "CLAIMED" and not current_principal.is_workspace_admin:
            raise HumanWorkflowConflict("human_work_item_assigned_to_another_user")
        if work.status not in {"WAITING", "CLAIMED"}:
            raise HumanWorkflowConflict("human_work_item_not_startable")

        await _cancel_superseded_human_replies(session, work=work, principal=current_principal)
        source = state.state
        work.status = "CLAIMED"
        work.assigned_user_id = effective_user_id
        work.assigned_actor = effective_actor
        work.assigned_session_id = (
            current_principal.session_id if effective_user_id is None else None
        )
        work.claimed_at = work.claimed_at or datetime.now(UTC)
        work.version += 1
        state.state = "HUMAN_ACTIVE"
        state.state_version += 1
        state.human_agent_id = effective_actor
        state.state_changed_reason = "human_reception_started"
        await session.execute(
            update(models.OutboxMessage)
            .where(
                models.OutboxMessage.conversation_id == conversation.id,
                models.OutboxMessage.status.in_(["PENDING", "FAILED"]),
                models.OutboxMessage.actor_kind == OutboxActor.BOT,
            )
            .values(status="CANCELLED", last_error_code="TAKEOVER")
        )
        await advance_handoff_notification_for_work(session, work=work, force_refresh=True)
        session.add_all(
            [
                models.AuditLog(
                    tenant_id=conversation.tenant_id,
                    category="state_transition",
                    actor=effective_actor,
                    action="HUMAN_ACTIVE",
                    subject_type="conversation",
                    subject_id=str(conversation.id),
                    detail={"reason": "human_reception_started", "source": source},
                ),
                models.AuditLog(
                    tenant_id=conversation.tenant_id,
                    category="human_work",
                    actor=effective_actor,
                    action="START_RECEPTION",
                    subject_type="human_work_item",
                    subject_id=str(work.id),
                    detail={"conversation_id": str(conversation.id), "version": work.version},
                ),
            ]
        )
        await session.commit()
        return work.id


async def resume_bot(
    *,
    conversation_id: uuid.UUID,
    allowed_tenants: frozenset[str],
    actor: str,
    target: str,
    principal: Principal | None = None,
    expected_state: str | None = None,
) -> None:
    allowed_targets = {"BOT_DRAFT_ONLY", "BOT_ACTIVE"}
    if expected_state is not None:
        allowed_targets.add("BOT_COOLDOWN")
    if target not in allowed_targets:
        raise HumanWorkflowError("resume_target_invalid")
    if principal is None:
        principal = authenticated_principal_context()
    if principal is None:
        raise HumanWorkflowError("human_principal_required")
    async with get_session_factory()() as session:
        (
            conversation,
            account,
            open_work,
            state,
            current_principal,
            staff_rows,
        ) = await _lock_conversation_context(
            session,
            conversation_id=conversation_id,
            allowed_tenants=allowed_tenants,
            principal=principal,
        )
        (
            current_principal,
            _staff,
            _effective_user_id,
            effective_actor,
        ) = await _authorize_human_actor(
            principal=principal,
            current_principal=current_principal,
            staff_user=(
                staff_rows.get(current_principal.user_id)
                if current_principal.user_id is not None
                else None
            ),
            account=account,
            tenant_id=conversation.tenant_id,
            actor=actor,
            user_id=principal.user_id,
        )
        if not current_principal.is_workspace_admin:
            raise HumanWorkflowError("human_admin_required")
        if open_work is not None:
            require_work_conversation_tenant(
                open_work,
                conversation_tenant_id=conversation.tenant_id,
            )
            raise HumanWorkflowConflict("human_work_item_still_open")
        if not get_settings().automation_default_allowed(account.platform, target):
            raise HumanWorkflowError("automation_default_not_allowed")
        if expected_state is not None and state.state != expected_state:
            raise HumanWorkflowConflict("automation_state_version_conflict")
        allowed_states = {"HANDOFF_PENDING", "HUMAN_ACTIVE", "BOT_COOLDOWN"}
        if expected_state is not None:
            allowed_states.update(("BOT_ACTIVE", "BOT_DRAFT_ONLY"))
        if state.state not in allowed_states:
            raise HumanWorkflowConflict("conversation_not_human_active")
        if state.state == "HANDOFF_PENDING":
            resolved_work_exists = await session.scalar(
                select(models.HumanWorkItem.id).where(
                    models.HumanWorkItem.conversation_id == conversation_id,
                    models.HumanWorkItem.status == "RESOLVED",
                )
            )
            if resolved_work_exists is None:
                raise HumanWorkflowConflict("conversation_not_human_active")
        state.state = target
        state.state_version += 1
        state.human_agent_id = None
        state.state_changed_reason = "human_work_resumed"
        session.add(
            models.AuditLog(
                tenant_id=conversation.tenant_id,
                category="state_transition",
                actor=effective_actor,
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
    user_id: uuid.UUID | None = None,
    allow_override: bool = False,
    work_item_id: uuid.UUID | None = None,
    expected_version: int | None = None,
    principal: Principal | None = None,
    owner_user_id: uuid.UUID | None = None,
) -> uuid.UUID:
    if principal is None:
        raise HumanWorkflowError("human_principal_required")
    async with get_session_factory()() as session:
        (
            conversation,
            account,
            work,
            state,
            current_principal,
            staff_rows,
        ) = await _lock_conversation_context(
            session,
            conversation_id=conversation_id,
            allowed_tenants=allowed_tenants,
            principal=principal,
            owner_user_id=owner_user_id,
        )
        (
            current_principal,
            _staff,
            effective_user_id,
            effective_actor,
        ) = await _authorize_human_actor(
            principal=principal,
            current_principal=current_principal,
            staff_user=(
                staff_rows.get(current_principal.user_id)
                if current_principal.user_id is not None
                else None
            ),
            account=account,
            tenant_id=conversation.tenant_id,
            actor=actor,
            user_id=user_id,
        )
        if account.status != "active":
            raise HumanWorkflowConflict("human_account_access_denied")
        initiator_session_id = current_principal.session_id
        if work_item_id is not None and (work is None or work.id != work_item_id):
            raise HumanWorkflowConflict("human_work_item_scope_mismatch")
        effective_allow_override = allow_override and current_principal.is_workspace_admin
        if work is None:
            raise HumanWorkflowConflict("human_reply_requires_claim")
        previous_work_version = work.version
        if expected_version is not None and work.version != expected_version:
            raise HumanWorkflowConflict("human_work_item_version_conflict")

        if work.status == "WAITING":
            raise HumanWorkflowConflict("human_reply_requires_claim")
        else:
            _require_assignee(
                work,
                actor=effective_actor,
                user_id=effective_user_id,
                principal=current_principal,
                allow_override=effective_allow_override,
            )
            if effective_allow_override and (
                work.assigned_user_id != effective_user_id
                or work.assigned_actor != effective_actor
                or (
                    effective_user_id is None
                    and work.assigned_session_id != current_principal.session_id
                )
            ):
                await _cancel_superseded_human_replies(
                    session,
                    work=work,
                    principal=current_principal,
                )
                work.assigned_user_id = effective_user_id
                work.assigned_actor = effective_actor
                work.assigned_session_id = (
                    current_principal.session_id if effective_user_id is None else None
                )
                work.claimed_at = work.claimed_at or datetime.now(UTC)
                work.version += 1

        if state.state == "CLOSED":
            raise HumanWorkflowConflict("conversation_not_sendable")
        if state.state != "HUMAN_ACTIVE":
            state.state = "HUMAN_ACTIVE"
            state.state_version += 1
            state.human_agent_id = effective_actor
            state.state_changed_reason = "admin_human_reply"
        else:
            state.human_agent_id = effective_actor
        await advance_handoff_notification_for_work(
            session, work=work, force_refresh=work.version != previous_work_version
        )
        await session.execute(
            update(models.OutboxMessage)
            .where(
                models.OutboxMessage.conversation_id == conversation_id,
                models.OutboxMessage.status.in_(["PENDING", "FAILED"]),
                models.OutboxMessage.actor_kind == OutboxActor.BOT,
            )
            .values(status="CANCELLED", last_error_code="TAKEOVER")
        )

        existing_intent = await find_outbox_intent(
            session,
            tenant_id=conversation.tenant_id,
            conversation_id=conversation.id,
            idempotency_key=idempotency_key,
        )
        if existing_intent is not None:
            _require_assignee(
                work,
                actor=effective_actor,
                user_id=effective_user_id,
                principal=current_principal,
                allow_override=False,
            )
            same_intent = (
                existing_intent.platform_account_id == conversation.platform_account_id
                and existing_intent.reply_to_message_id == reply_to_message_id
                and existing_intent.origin_kind == OutboxOrigin.MANUAL_REPLY
                and existing_intent.actor_kind == OutboxActor.ADMIN_HUMAN
                and existing_intent.actor_id == effective_actor
                and existing_intent.initiator_user_id == effective_user_id
                and existing_intent.initiator_session_id == initiator_session_id
                and existing_intent.human_work_item_version == work.version
                and isinstance(existing_intent.payload, dict)
                and existing_intent.payload.get("text") == text.strip()
            )
            if not same_intent:
                raise OutboxIdempotencyConflict("idempotency_key_reused_with_different_intent")
            outbox_id = existing_intent.id
        else:
            outbox_id = await create_or_get_outbox_intent(
                session,
                conversation_id=conversation.id,
                platform_account_id=conversation.platform_account_id,
                reply_to_message_id=reply_to_message_id,
                text=text,
                origin_kind=OutboxOrigin.MANUAL_REPLY,
                actor_kind=OutboxActor.ADMIN_HUMAN,
                actor_id=effective_actor,
                idempotency_key=idempotency_key,
                initiator_user_id=effective_user_id,
                initiator_session_id=initiator_session_id,
                human_work_item_version=work.version,
            )
        session.add(
            models.AuditLog(
                tenant_id=conversation.tenant_id,
                category="human_work",
                actor=effective_actor,
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
