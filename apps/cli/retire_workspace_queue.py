"""One-off, stopped-process development cutover; never manages Redis or schema."""

import argparse
import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.shared.config import get_settings

EXPECTED_SCHEMA = "c6f2a9d4e810"
AUDIT_ACTION = "WORKSPACE_QUEUE_CUTOVER"
RETIRED = "RETIRED_BY_CUTOVER"
UNKNOWN = "WORKSPACE_QUEUE_CUTOVER_UNKNOWN"
_LOCK_TABLES = (
    "admin_users, audit_logs, automation_states, conversations, decision_jobs, "
    "handoff_notification_intents, human_work_items, messages, outbox_messages, "
    "platform_accounts, provisioning_jobs, raw_events, reply_decisions"
)


def parse_before(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is None:
            raise ValueError("timezone required")
        return parsed.astimezone(UTC)
    except ValueError as error:
        raise argparse.ArgumentTypeError("--before requires an aware ISO timestamp") from error


@dataclass(frozen=True)
class CutoverRequest:
    tenant: str
    cutover_id: uuid.UUID
    before: datetime
    restore_admin_ids: tuple[uuid.UUID, ...] = ()
    apply: bool = False
    confirm_processes_stopped: bool = False
    startup_namespace: str | None = None

    def validate(self) -> None:
        if self.tenant != "default" or self.tenant != get_settings().tenant_id:
            raise ValueError("default_tenant_required")
        if self.before.utcoffset() is None:
            raise ValueError("before_timezone_required")
        if self.before > datetime.now(UTC):
            raise ValueError("before_in_future")
        if (self.apply or self.startup_namespace is not None) and not self.confirm_processes_stopped:
            raise ValueError("processes_stopped_confirmation_required")
        if self.startup_namespace is not None and self.startup_namespace != (
            f"dramatiq-cutover-{self.cutover_id.hex}"
        ):
            raise ValueError("startup_namespace_mismatch")

    def parameters(self) -> dict[str, Any]:
        return {
            "tenant": self.tenant,
            "cutover_id": str(self.cutover_id),
            "before": self.before.astimezone(UTC).isoformat(),
            "restore_admin_ids": sorted({str(value) for value in self.restore_admin_ids}),
            "schema": EXPECTED_SCHEMA,
            **({"startup_contract": "fresh-queue-v1", "namespace": self.startup_namespace}
               if self.startup_namespace is not None else {}),
        }


@dataclass(frozen=True)
class Operation:
    name: str
    model: type[models.Base]
    predicate: Any
    values: dict[str, Any]


def _unfinished_raw() -> Any:
    raw = models.RawEvent
    return and_(
        ~raw.processing_status.startswith("PROCESSED"),
        ~raw.processing_status.startswith("IGNORED"),
        raw.processing_status.not_in(("VERIFIED_REQUEST", "XCHAT_UNSUPPORTED_EVENT", RETIRED)),
    )


def _operations(request: CutoverRequest, now: datetime) -> tuple[Operation, ...]:
    def old(model: Any) -> Any:
        return and_(model.tenant_id == request.tenant, model.created_at < request.before)

    conversation_ids = select(models.Conversation.id).where(
        models.Conversation.tenant_id == request.tenant,
    )
    account_ids = select(models.PlatformAccount.id).where(
        models.PlatformAccount.tenant_id == request.tenant,
    )
    human = models.HumanWorkItem
    human_predicate = and_(old(human), human.status.in_(("WAITING", "CLAIMED")))
    human_conversations = select(human.conversation_id).where(human_predicate)
    state = models.AutomationState
    reset_age = state.updated_at < request.before
    if request.startup_namespace is not None:
        message = models.Message
        # Startup validates the fresh namespace after stopping old processes. Migration may
        # touch legacy human states after before; only widen age for otherwise idle old work.
        reset_age = or_(reset_age, and_(
            state.state.in_(("HUMAN_ACTIVE", "HANDOFF_PENDING")),
            ~select(message.id).where(
                message.conversation_id == state.conversation_id,
                message.direction == "inbound", message.created_at >= request.before,
            ).exists(),
            ~select(human.id).where(
                human.conversation_id == state.conversation_id,
                human.created_at >= request.before,
            ).exists(),
        ))
    outbox = models.OutboxMessage
    decision = models.DecisionJob
    draft = models.ReplyDecision
    notification = models.HandoffNotificationIntent
    provisioning = models.ProvisioningJob
    raw = models.RawEvent
    raw_scope = or_(
        raw.tenant_id == request.tenant,
        and_(raw.tenant_id.is_(None), raw.platform_account_id.in_(account_ids)),
    )
    # Preserve evidence, successful events and ingress-only records. No forged PROCESSED state.
    raw_pending = and_(
        raw_scope, raw.received_at < request.before, _unfinished_raw(),
    )
    notification_expiry = {
        "action_nonce": func.gen_random_uuid(), "valid_until": now,
        "next_attempt_at": None, "claim_token": None,
        "claim_expires_at": None, "sending_revision": None,
    }
    return (
        # This must precede cancelling the work items selected by the subquery.
        Operation("automation_reset", state, and_(
            state.conversation_id.in_(human_conversations), reset_age,
            state.state.in_(("HUMAN_ACTIVE", "HANDOFF_PENDING", "BOT_COOLDOWN")),
        ), {
            "state": "BOT_DRAFT_ONLY", "state_version": state.state_version + 1,
            "human_agent_id": None, "last_human_message_at": None,
            "resume_policy": "MANUAL", "state_changed_reason": RETIRED,
        }),
        Operation("outbox_cancelled", outbox, and_(
            old(outbox), outbox.status.in_(("PENDING", "FAILED")),
        ), {
            "status": "CANCELLED", "last_error_code": RETIRED,
            "next_attempt_at": None, "locked_at": None, "locked_by": None,
        }),
        Operation("outbox_unknown", outbox, and_(
            old(outbox), outbox.status.in_(("SENDING", "NEEDS_REVIEW")),
        ), {
            "status": "NEEDS_REVIEW", "last_error_code": UNKNOWN,
            "next_attempt_at": None, "locked_at": None, "locked_by": None,
        }),
        Operation("decisions_superseded", decision, and_(
            decision.conversation_id.in_(conversation_ids), decision.account_id.in_(account_ids),
            decision.created_at < request.before,
            decision.status.in_(("PENDING", "FAILED", "PROCESSING", "NEEDS_REVIEW")),
        ), {
            "status": "SUPERSEDED", "claim_token": None, "locked_at": None,
            "next_attempt_at": None, "completed_at": now, "last_error": RETIRED,
        }),
        Operation("drafts_rejected", draft, and_(
            old(draft), draft.action == "draft",
            or_(draft.review_action.is_(None), draft.review_action == "PENDING"),
            draft.reviewed_at.is_(None),
        ), {
            "review_action": "REJECTED", "reviewed_at": now,
            "reviewed_by": f"cutover:{request.cutover_id}", "review_reason": RETIRED,
        }),
        Operation("human_cancelled", human, human_predicate, {
            "status": "CANCELLED", "version": human.version + 1,
            "resolved_at": now, "resolved_actor": f"cutover:{request.cutover_id}",
            "due_at": None,
        }),
        Operation("notifications_cancelled", notification, and_(
            old(notification), notification.status.in_(("PENDING", "FAILED", "BLOCKED_CONFIG")),
        ), {
            **notification_expiry, "status": "CANCELLED", "desired_card_state": "CANCELLED",
            "last_error_code": RETIRED,
        }),
        # A stale SENDING card update would otherwise be retried by the notification sweep.
        Operation("notifications_unknown", notification, and_(
            old(notification), notification.status.in_(("SENDING", "NEEDS_REVIEW")),
        ), {**notification_expiry, "status": "NEEDS_REVIEW", "last_error_code": UNKNOWN}),
        Operation("notification_actions_expired", notification, and_(
            old(notification), notification.status == "SYNCED",
        ), {"action_nonce": func.gen_random_uuid(), "valid_until": now}),
        Operation("provisioning_cancelled", provisioning, and_(
            old(provisioning), provisioning.status.not_in(("COMPLETED", "CANCELLED")),
        ), {
            "status": "CANCELLED", "attempt_count": provisioning.attempt_count + 1,
            "locked_at": None, "locked_by": None, "next_attempt_at": None,
            "completed_at": now, "last_error_code": RETIRED,
        }),
        Operation("raw_retired", raw, raw_pending, {
            "processing_status": RETIRED, "processing_error_code": RETIRED,
            "processing_claim_token": None, "processing_claim_expires_at": None,
            "processing_next_attempt_at": None,
            "processing_attempt_count": raw.processing_attempt_count + 1,
        }),
    )


async def _validate_database(session: AsyncSession, request: CutoverRequest) -> None:
    versions = list((await session.scalars(text("SELECT version_num FROM alembic_version"))).all())
    if versions != [EXPECTED_SCHEMA]:
        raise ValueError("exact_schema_head_required")
    database_now = await session.scalar(select(func.clock_timestamp()))
    if request.before > database_now:
        raise ValueError("before_in_future")


async def _read_admins(session: AsyncSession, request: CutoverRequest) -> list[models.AdminUser]:
    statement = select(models.AdminUser).where(
        models.AdminUser.id.in_(request.restore_admin_ids),
    ).order_by(models.AdminUser.id)
    if request.apply:
        statement = statement.with_for_update()
    admins = list((await session.scalars(statement)).all())
    if len(admins) != len(set(request.restore_admin_ids)) or any(
        admin.tenant_id != request.tenant or admin.status != "active"
        or admin.role not in {"AGENT", "USER", "WORKSPACE_ADMIN"} for admin in admins
    ):
        raise ValueError("restore_admin_scope_or_role_invalid")
    return admins


async def _perform_cutover(session: AsyncSession, request: CutoverRequest) -> dict[str, Any]:
    await _validate_database(session, request)
    existing = await session.get(models.AuditLog, request.cutover_id)
    if existing is not None:
        if (
            existing.action != AUDIT_ACTION or existing.tenant_id != request.tenant
            or existing.detail.get("parameters") != request.parameters()
        ):
            raise ValueError("cutover_id_parameter_conflict")
        return {**existing.detail, "status": "already_applied"}
    admins = await _read_admins(session, request)
    raw = models.RawEvent
    unscoped_raw = await session.scalar(select(func.count()).select_from(raw).where(
        raw.tenant_id.is_(None), raw.platform_account_id.is_(None),
        raw.received_at < request.before, _unfinished_raw(),
    ))
    if request.apply and unscoped_raw:
        raise ValueError("unscoped_raw_requires_manual_review")
    now = await session.scalar(select(func.clock_timestamp()))
    operations = _operations(request, now)
    counts = {
        operation.name: await session.scalar(
            select(func.count()).select_from(operation.model).where(operation.predicate)
        ) for operation in operations
    }
    admin_changes = [
        {"id": str(admin.id), "previous_role": admin.role, "role": "WORKSPACE_ADMIN"}
        for admin in admins
    ]
    detail = {
        "parameters": request.parameters(),
        "counts": {**counts, "unscoped_raw_blockers": unscoped_raw, "admins_restored": sum(
            admin.role != "WORKSPACE_ADMIN" for admin in admins
        )},
        "admins": admin_changes,
        "warning": "Old broker messages MUST remain isolated; tokenless direct actors replay raw.",
    }
    if not request.apply:
        return {**detail, "status": "preview"}
    for operation in operations:
        result = await session.execute(
            update(operation.model).where(operation.predicate).values(**operation.values)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != counts[operation.name]:
            raise ValueError("cutover_count_changed")
    for admin in admins:
        if admin.role == "WORKSPACE_ADMIN":
            continue
        await session.execute(update(models.AdminUser).where(
            models.AdminUser.id == admin.id, models.AdminUser.tenant_id == request.tenant,
        ).values(role="WORKSPACE_ADMIN").execution_options(synchronize_session=False))
        session.add(models.AuditLog(
            tenant_id=request.tenant, category="authorization", actor="cli:workspace-cutover",
            action="RESTORE_WORKSPACE_ADMIN", subject_type="admin_user", subject_id=str(admin.id),
            detail={"cutover_id": str(request.cutover_id), "previous_role": admin.role,
                    "role": "WORKSPACE_ADMIN"},
        ))
    session.add(models.AuditLog(
        id=request.cutover_id, tenant_id=request.tenant, category="maintenance",
        actor="cli:workspace-cutover", action=AUDIT_ACTION, subject_type="workspace",
        subject_id=request.tenant, detail=detail,
    ))
    return {**detail, "status": "applied"}


async def run_cutover(session: AsyncSession, request: CutoverRequest) -> dict[str, Any]:
    request.validate()
    async with session.begin():
        if not request.apply:
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        await session.execute(text("SET LOCAL lock_timeout = '3s'"))
        await session.execute(text("SET LOCAL statement_timeout = '30s'"))
        if request.apply:
            acquired = await session.scalar(text(
                "SELECT pg_try_advisory_xact_lock(hashtext('workspace_queue_cutover'))"
            ))
            if not acquired:
                raise ValueError("cutover_already_running")
            # These short locks protect this transaction, NOT external in-flight requests.
            await session.execute(text(f"LOCK TABLE {_LOCK_TABLES} IN SHARE ROW EXCLUSIVE MODE"))
            await session.execute(text("LOCK TABLE alembic_version IN SHARE MODE"))
        report = await _perform_cutover(session, request)
    return report


async def _run(request: CutoverRequest) -> dict[str, Any]:
    async with get_session_factory()() as session:
        return await run_cutover(session, request)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--cutover-id", type=uuid.UUID, required=True)
    parser.add_argument("--before", type=parse_before, required=True)
    parser.add_argument("--restore-admin-id", type=uuid.UUID, action="append", default=[])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-processes-stopped", action="store_true")
    arguments = parser.parse_args()
    request = CutoverRequest(
        tenant=arguments.tenant, cutover_id=arguments.cutover_id, before=arguments.before,
        restore_admin_ids=tuple(arguments.restore_admin_id), apply=arguments.apply,
        confirm_processes_stopped=arguments.confirm_processes_stopped,
    )
    try:
        report = asyncio.run(_run(request))
    except ValidationError:
        parser.exit(2, "cutover refused: invalid configuration (values redacted)\n")
    except ValueError as error:
        parser.exit(2, f"cutover refused: {error}\n")
    except SQLAlchemyError:
        parser.exit(
            2, "cutover database failure; no success confirmed; retry the SAME cutover ID\n",
        )
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
