from __future__ import annotations

import uuid

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.application.account_management.access import (
    lock_session_authorities,
    lock_user_authority,
)
from social_reply.application.handoff_notifications.service import (
    advance_handoff_notification_for_work,
)
from social_reply.application.message_delivery.intents import OutboxActor, OutboxOrigin
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.advisory_locks import (
    acquire_conversation_delivery_xact_lock,
)


async def revoke_staff_authority(
    session: AsyncSession,
    user_id: uuid.UUID,
    reason: str,
) -> dict[str, int]:
    """Revoke one employee's executable authority without touching unrelated work.

    The transaction follows the shared mutation order: read identities without locks, lock
    staff authority and sessions, acquire every affected conversation delivery lock in UUID
    order, then lock conversations/accounts/users/work items.  In particular, NULL
    ``assigned_user_id`` is never treated as belonging to this employee; a real bootstrap
    assignment is identified by its session and is left untouched.
    """
    normalized_reason = reason.strip()
    if not normalized_reason:
        raise ValueError("staff_revoke_reason_required")

    identity = await session.scalar(
        select(models.AdminUser)
        .where(models.AdminUser.id == user_id)
        .execution_options(populate_existing=True)
    )
    if identity is None:
        raise ValueError("staff_user_not_found")
    tenant_id = identity.tenant_id

    # These are deliberately unlocked discovery reads.  Every writer that can change an
    # assignment or grant takes the same staff authority lock before its business rows.
    work_identity = list(
        (
            await session.execute(
                select(
                    models.HumanWorkItem.id,
                    models.HumanWorkItem.conversation_id,
                    models.Conversation.platform_account_id,
                )
                .join(
                    models.Conversation,
                    models.Conversation.id == models.HumanWorkItem.conversation_id,
                )
                .where(
                    models.HumanWorkItem.tenant_id == tenant_id,
                    models.HumanWorkItem.status == "CLAIMED",
                    models.HumanWorkItem.assigned_user_id == user_id,
                    models.Conversation.tenant_id == tenant_id,
                )
            )
        ).all()
    )
    conversation_ids = sorted({conversation_id for _, conversation_id, _ in work_identity}, key=str)
    work_account_ids = {account_id for _, _, account_id in work_identity}
    outbox_identity = list(
        (
            await session.execute(
                select(
                    models.OutboxMessage.conversation_id,
                    models.OutboxMessage.platform_account_id,
                ).where(
                    models.OutboxMessage.tenant_id == tenant_id,
                    models.OutboxMessage.initiator_user_id == user_id,
                    models.OutboxMessage.origin_kind.in_(
                        [OutboxOrigin.MANUAL_REPLY, OutboxOrigin.DRAFT_APPROVAL]
                    ),
                    models.OutboxMessage.actor_kind == OutboxActor.ADMIN_HUMAN,
                    models.OutboxMessage.status.in_(["PENDING", "FAILED"]),
                )
            )
        ).all()
    )
    conversation_ids = sorted(
        {conversation_id for _, conversation_id, _ in work_identity}
        | {conversation_id for conversation_id, _ in outbox_identity},
        key=str,
    )
    outbox_account_ids = {account_id for _, account_id in outbox_identity}
    work_account_ids.update(outbox_account_ids)

    # Every writer that can change an assignment or grant takes the same staff authority lock.
    await lock_user_authority(session, user_id)
    # Re-read after the staff lock so a compliant concurrent assignment is either included or
    # waits behind this transaction; no work row is locked before conversation locks.
    work_identity = list(
        (
            await session.execute(
                select(
                    models.HumanWorkItem.id,
                    models.HumanWorkItem.conversation_id,
                    models.Conversation.platform_account_id,
                )
                .join(
                    models.Conversation,
                    models.Conversation.id == models.HumanWorkItem.conversation_id,
                )
                .where(
                    models.HumanWorkItem.tenant_id == tenant_id,
                    models.HumanWorkItem.status == "CLAIMED",
                    models.HumanWorkItem.assigned_user_id == user_id,
                    models.Conversation.tenant_id == tenant_id,
                )
            )
        ).all()
    )
    conversation_ids = sorted({conversation_id for _, conversation_id, _ in work_identity}, key=str)
    work_account_ids.update(account_id for _, _, account_id in work_identity)
    outbox_identity = list(
        (
            await session.execute(
                select(
                    models.OutboxMessage.conversation_id,
                    models.OutboxMessage.platform_account_id,
                ).where(
                    models.OutboxMessage.tenant_id == tenant_id,
                    models.OutboxMessage.initiator_user_id == user_id,
                    models.OutboxMessage.origin_kind.in_(
                        [OutboxOrigin.MANUAL_REPLY, OutboxOrigin.DRAFT_APPROVAL]
                    ),
                    models.OutboxMessage.actor_kind == OutboxActor.ADMIN_HUMAN,
                    models.OutboxMessage.status.in_(["PENDING", "FAILED"]),
                )
            )
        ).all()
    )
    conversation_ids = sorted(
        {conversation_id for _, conversation_id, _ in work_identity}
        | {conversation_id for conversation_id, _ in outbox_identity},
        key=str,
    )
    work_account_ids.update(account_id for _, account_id in outbox_identity)

    # Lock all target sessions before any conversation or business-row lock.  Session
    # invalidation and authority changes therefore cannot race a sensitive operation.
    target_session_ids = tuple(await session.scalars(
        select(models.AdminSession.id).where(models.AdminSession.user_id == user_id)
    ))
    await lock_session_authorities(session, target_session_ids)
    await session.execute(
        select(models.AdminSession)
        .where(models.AdminSession.user_id == user_id)
        .execution_options(populate_existing=True)
        .order_by(models.AdminSession.id)
        .with_for_update()
    )
    for conversation_id in conversation_ids:
        await acquire_conversation_delivery_xact_lock(session, conversation_id)

    conversations = {
        conversation.id: conversation
        for conversation in (
            await session.scalars(
                select(models.Conversation)
                .where(
                    models.Conversation.id.in_(conversation_ids),
                    models.Conversation.tenant_id == tenant_id,
                )
                .execution_options(populate_existing=True)
                .with_for_update()
                .order_by(models.Conversation.id)
            )
        ).all()
    }
    if len(conversations) != len(conversation_ids):
        raise ValueError("staff_revoke_conversation_scope_mismatch")

    # Re-discover grants after the staff lock, then lock every account in stable order.
    grant_identity = list(
        (
            await session.scalars(
                select(models.AccountReauthorizationGrant.platform_account_id).where(
                    models.AccountReauthorizationGrant.user_id == user_id,
                    models.AccountReauthorizationGrant.active.is_(True),
                )
            )
        ).all()
    )
    access_grant_identity = list(await session.scalars(
        select(models.AccountAccessGrant.platform_account_id).where(
            models.AccountAccessGrant.tenant_id == tenant_id,
            models.AccountAccessGrant.user_id == user_id,
            models.AccountAccessGrant.active.is_(True),
        )
    ))
    account_ids = sorted(
        set(grant_identity) | set(access_grant_identity) | work_account_ids, key=str
    )
    accounts = {
        account.id: account
        for account in (
            await session.scalars(
                select(models.PlatformAccount)
                .where(
                    models.PlatformAccount.id.in_(account_ids),
                    models.PlatformAccount.tenant_id == tenant_id,
                )
                .execution_options(populate_existing=True)
                .with_for_update()
                .order_by(models.PlatformAccount.id)
            )
        ).all()
    }
    if len(accounts) != len(account_ids):
        raise ValueError("staff_revoke_grant_scope_mismatch")

    user = await session.scalar(
        select(models.AdminUser)
        .where(models.AdminUser.id == user_id, models.AdminUser.tenant_id == tenant_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if user is None:
        raise ValueError("staff_user_not_found")

    active_grants = list(
        (
            await session.scalars(
                select(models.AccountReauthorizationGrant)
                .where(
                    models.AccountReauthorizationGrant.user_id == user_id,
                    models.AccountReauthorizationGrant.active.is_(True),
                )
                .execution_options(populate_existing=True)
                .with_for_update()
                .order_by(models.AccountReauthorizationGrant.id)
            )
        ).all()
    )
    active_access_grants = list(await session.scalars(
        select(models.AccountAccessGrant).where(
            models.AccountAccessGrant.tenant_id == tenant_id,
            models.AccountAccessGrant.user_id == user_id,
            models.AccountAccessGrant.active.is_(True),
        ).execution_options(populate_existing=True).order_by(models.AccountAccessGrant.id)
        .with_for_update()
    ))
    work_items = list(
        (
            await session.scalars(
                select(models.HumanWorkItem)
                .where(
                    models.HumanWorkItem.tenant_id == tenant_id,
                    models.HumanWorkItem.status == "CLAIMED",
                    models.HumanWorkItem.assigned_user_id == user_id,
                )
                .execution_options(populate_existing=True)
                .with_for_update()
                .order_by(models.HumanWorkItem.id)
            )
        ).all()
    )
    states = {
        state.conversation_id: state
        for state in (
            await session.scalars(
                select(models.AutomationState)
                .where(models.AutomationState.conversation_id.in_(conversation_ids))
                .execution_options(populate_existing=True)
                .with_for_update()
                .order_by(models.AutomationState.conversation_id)
            )
        ).all()
    }

    released_work = 0
    for work in work_items:
        conversation = conversations.get(work.conversation_id)
        if conversation is None or conversation.tenant_id != work.tenant_id:
            raise ValueError("staff_revoke_conversation_scope_mismatch")
        state = states.get(work.conversation_id)
        if state is None:
            raise ValueError("staff_revoke_automation_state_missing")

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
            state.state_changed_reason = "staff_authority_revoked"
        else:
            session.add(
                models.AuditLog(
                    tenant_id=work.tenant_id,
                    category="staff_lifecycle",
                    actor=f"user:{user.username}",
                    action="HUMAN_WORK_STATE_DRIFT",
                    subject_type="human_work_item",
                    subject_id=str(work.id),
                    detail={"state": state.state, "reason": normalized_reason},
                )
            )
        await advance_handoff_notification_for_work(session, work=work)
        released_work += 1

    bumped_accounts: set[uuid.UUID] = set()
    for grant in [*active_grants, *active_access_grants]:
        grant.active = False
        account = accounts.get(grant.platform_account_id)
        if account is None:
            raise ValueError("staff_revoke_grant_scope_mismatch")
        if account.id not in bumped_accounts:
            account.config_version += 1
            bumped_accounts.add(account.id)

    provisioning_jobs = list(
        (
            await session.scalars(
                select(models.ProvisioningJob)
                .where(
                    or_(
                        models.ProvisioningJob.initiator_user_id == user_id,
                        models.ProvisioningJob.owner_user_id == user_id,
                    ),
                    models.ProvisioningJob.status.in_(
                        ["PENDING", "FAILED", "NEEDS_ACTION", "PROCESSING"]
                    ),
                )
                .with_for_update()
            )
        ).all()
    )
    cancelled_jobs = 0
    for provisioning_job in provisioning_jobs:
        result = dict(provisioning_job.result or {})
        checkpoint = result.get("checkpoint")
        applied = (
            isinstance(checkpoint, dict)
            and checkpoint.get("job_id") == str(provisioning_job.id)
            and checkpoint.get("account_id")
            and checkpoint.get("phase")
            in {"ACCOUNT_PERSISTED", "CREDENTIALS_APPLIED", "SUBSCRIPTIONS_APPLIED"}
        )
        provisioning_job.status = "CANCELLED"
        provisioning_job.next_attempt_at = None
        provisioning_job.locked_at = None
        provisioning_job.locked_by = None
        if applied:
            phase = str(checkpoint["phase"])
            provisioning_job.current_step = f"{phase}_CANCELLED"
            provisioning_job.last_error_code = "STAFF_AUTHORITY_REVOKED_AFTER_APPLY"
            provisioning_job.last_error_message = (
                "Provisioning was cancelled after the durable application fact was committed: "
                f"{phase}; {normalized_reason[:350]}"
            )
            provisioning_job.result = {
                **result,
                "cancellation": {
                    "after_applied": True,
                    "phase": phase,
                    "reason": normalized_reason,
                },
            }
        else:
            provisioning_job.current_step = "REVOKED"
            provisioning_job.last_error_code = "STAFF_AUTHORITY_REVOKED"
            provisioning_job.last_error_message = normalized_reason[:500]
        cancelled_jobs += 1

    manual_outboxes = list(
        (
            await session.scalars(
                select(models.OutboxMessage)
                .where(
                    models.OutboxMessage.tenant_id == tenant_id,
                    models.OutboxMessage.conversation_id.in_(conversation_ids),
                    models.OutboxMessage.initiator_user_id == user_id,
                    models.OutboxMessage.origin_kind.in_(
                        [OutboxOrigin.MANUAL_REPLY, OutboxOrigin.DRAFT_APPROVAL]
                    ),
                    models.OutboxMessage.actor_kind == OutboxActor.ADMIN_HUMAN,
                    models.OutboxMessage.status.in_(["PENDING", "FAILED"]),
                )
                .execution_options(populate_existing=True)
                .order_by(
                    models.OutboxMessage.conversation_id,
                    models.OutboxMessage.id,
                )
                .with_for_update()
            )
        ).all()
    )
    for manual_outbox in manual_outboxes:
        manual_outbox.status = "CANCELLED"
        manual_outbox.last_error_code = "STAFF_AUTHORITY_REVOKED"
        manual_outbox.last_error_message = normalized_reason[:500]
        manual_outbox.next_attempt_at = None
    cancelled_outbox = len(manual_outboxes)
    operators = list(
        (
            await session.scalars(
                select(models.FeishuHandoffOperator)
                .where(
                    models.FeishuHandoffOperator.admin_user_id == user_id,
                    models.FeishuHandoffOperator.status == "ACTIVE",
                )
                .execution_options(populate_existing=True)
                .order_by(models.FeishuHandoffOperator.id)
                .with_for_update()
            )
        ).all()
    )
    for operator in operators:
        operator.status = "DISABLED"
    disabled_operators = len(operators)

    session.add(
        models.AuditLog(
            tenant_id=tenant_id,
            category="staff_lifecycle",
            actor=f"user:{user.username}",
            action="REVOKE_STAFF_AUTHORITY",
            subject_type="admin_user",
            subject_id=str(user.id),
            detail={
                "reason": normalized_reason,
                "released_work_items": released_work,
                "legacy_work_items_released": 0,
                "revoked_grants": len(active_grants),
                "revoked_access_grants": len(active_access_grants),
                "bumped_accounts": len(bumped_accounts),
                "cancelled_jobs": cancelled_jobs,
                "cancelled_outbox": cancelled_outbox,
                "disabled_feishu_operators": disabled_operators,
            },
        )
    )
    return {
        "released_work_items": released_work,
        "legacy_work_items_released": 0,
        "revoked_grants": len(active_grants),
        "revoked_access_grants": len(active_access_grants),
        "bumped_accounts": len(bumped_accounts),
        "cancelled_jobs": cancelled_jobs,
        "cancelled_outbox": cancelled_outbox,
        "disabled_feishu_operators": disabled_operators,
    }
