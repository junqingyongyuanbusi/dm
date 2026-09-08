"""Bounded homepage facts, without provider probes or private message content."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import Select, and_, case, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from social_reply.application.account_management.access import account_read_condition
from social_reply.application.account_management.auth import Principal
from social_reply.domain.platform_accounts import ACTIVE_ACCOUNT_STATUS
from social_reply.infrastructure.database import models
from social_reply.shared.config import Settings

_ALERT_LIMIT = 3
_ACTIVITY_LIMIT = 5
_META_FAILURE_STATUSES = ("REAUTH_REQUIRED", "ERROR")
_FEISHU_FAILURE_STATUSES = ("CREDENTIAL_INVALID", "BOT_NOT_ACTIVE", "BOT_ID_MISMATCH", "ERROR")
_DELIVERY_FAILURE_STATUSES = ("FAILED", "NEEDS_REVIEW")


@dataclass(frozen=True)
class HomeChannelAlert:
    account_id: UUID
    account_name: str
    platform: str
    health_status: str
    checked_at: datetime | None


@dataclass(frozen=True)
class HomeBusinessActivity:
    event_id: UUID
    conversation_id: UUID
    kind: str
    account_name: str
    occurred_at: datetime


@dataclass(frozen=True)
class HomeOverview:
    alerts: tuple[HomeChannelAlert, ...]
    activities: tuple[HomeBusinessActivity, ...]


def _account_scope(principal: Principal, tenant_id: str) -> ColumnElement[bool]:
    return account_read_condition(principal, tenant_id)


def _parse_checked_at(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        recorded_at = datetime.fromisoformat(value)
        if recorded_at.utcoffset() is None:
            return None
        return recorded_at.astimezone(UTC)
    except (ValueError, OverflowError):
        # A broken historical timestamp must never become a fabricated live check.
        return None


def _channel_alert_query(
    principal: Principal, tenant_id: str, settings: Settings
) -> Select | None:
    account = models.PlatformAccount
    meta_status = account.config["meta_health_status"].astext
    feishu_status = account.config["feishu_health_status"].astext
    platform_conditions = tuple(
        and_(account.platform == platform, health_status.in_(failure_statuses))
        for enabled, platform, health_status, failure_statuses in (
            (settings.facebook_messenger_enabled, "facebook", meta_status, _META_FAILURE_STATUSES),
            (
                settings.instagram_messaging_enabled, "instagram",
                meta_status, _META_FAILURE_STATUSES,
            ),
            (settings.feishu_enabled, "feishu", feishu_status, _FEISHU_FAILURE_STATUSES),
        )
        if enabled
    )
    if not platform_conditions:
        return None
    return (
        select(
            account.id.label("account_id"),
            account.name.label("account_name"),
            account.platform,
            case((account.platform == "feishu", feishu_status), else_=meta_status).label(
                "health_status"
            ),
            case(
                (account.platform == "feishu", account.config["feishu_health_checked_at"].astext),
                else_=account.config["meta_health_checked_at"].astext,
            ).label("checked_at"),
        )
        .where(
            _account_scope(principal, tenant_id),
            account.status == ACTIVE_ACCOUNT_STATUS,
            or_(*platform_conditions),
        )
        .order_by(account.platform, account.name, account.id)
        .limit(_ALERT_LIMIT)
    )


def _handoff_query(principal: Principal, tenant_id: str) -> Select:
    work_item = models.HumanWorkItem
    conversation = models.Conversation
    account = models.PlatformAccount
    return (
        select(
            work_item.id.label("event_id"),
            work_item.conversation_id,
            literal("handoff").label("kind"),
            account.name.label("account_name"),
            work_item.created_at.label("occurred_at"),
        )
        .join(conversation, work_item.conversation_id == conversation.id)
        .join(account, conversation.platform_account_id == account.id)
        .where(
            work_item.tenant_id == tenant_id,
            conversation.tenant_id == tenant_id,
            _account_scope(principal, tenant_id),
        )
        .order_by(work_item.created_at.desc(), work_item.id.desc())
        .limit(_ACTIVITY_LIMIT)
    )


def _sent_query(principal: Principal, tenant_id: str) -> Select:
    outbox = models.OutboxMessage
    conversation = models.Conversation
    account = models.PlatformAccount
    source_kinds = (
        (and_(outbox.origin_kind == "DECISION", outbox.actor_kind == "BOT"), "auto_sent"),
        (outbox.origin_kind == "DRAFT_APPROVAL", "draft_sent"),
        (outbox.origin_kind == "MANUAL_REPLY", "manual_sent"),
    )
    return (
        select(
            outbox.id.label("event_id"),
            outbox.conversation_id,
            case(*source_kinds).label("kind"),
            account.name.label("account_name"),
            outbox.sent_at.label("occurred_at"),
        )
        .join(conversation, outbox.conversation_id == conversation.id)
        .join(account, conversation.platform_account_id == account.id)
        .where(
            outbox.tenant_id == tenant_id,
            conversation.tenant_id == tenant_id,
            outbox.platform_account_id == account.id,
            _account_scope(principal, tenant_id),
            outbox.status == "SENT",
            outbox.sent_at.is_not(None),
            or_(*(condition for condition, _kind in source_kinds)),
        )
        .order_by(outbox.sent_at.desc(), outbox.id.desc())
        .limit(_ACTIVITY_LIMIT)
    )


def _delivery_failure_query(principal: Principal, tenant_id: str) -> Select:
    attempt = models.DeliveryAttempt
    outbox = models.OutboxMessage
    conversation = models.Conversation
    account = models.PlatformAccount
    return (
        select(
            attempt.id.label("event_id"),
            outbox.conversation_id,
            case(
                (attempt.outcome == "FAILED", "delivery_failed"),
                else_="delivery_review",
            ).label("kind"),
            account.name.label("account_name"),
            attempt.created_at.label("occurred_at"),
        )
        .select_from(attempt)
        .join(outbox, attempt.outbox_id == outbox.id)
        .join(conversation, outbox.conversation_id == conversation.id)
        .join(account, conversation.platform_account_id == account.id)
        .where(
            outbox.tenant_id == tenant_id,
            conversation.tenant_id == tenant_id,
            outbox.platform_account_id == account.id,
            _account_scope(principal, tenant_id),
            attempt.outcome.in_(_DELIVERY_FAILURE_STATUSES),
            outbox.status.in_(_DELIVERY_FAILURE_STATUSES),
        )
        .order_by(attempt.created_at.desc(), attempt.id.desc())
        .limit(_ACTIVITY_LIMIT)
    )


async def load_home_overview(
    session: AsyncSession,
    principal: Principal,
    tenant_id: str,
    *,
    settings: Settings,
) -> HomeOverview:
    """Return recorded alerts and historical events, not live health or pending counts."""
    principal.require_tenant(tenant_id)
    if not principal.is_superadmin and principal.user_id is None:
        return HomeOverview(alerts=(), activities=())

    alerts: tuple[HomeChannelAlert, ...] = ()
    alert_query = _channel_alert_query(principal, tenant_id, settings)
    if alert_query is not None:
        alert_rows = (await session.execute(alert_query)).mappings().all()
        alerts = tuple(
            HomeChannelAlert(
                account_id=row["account_id"],
                account_name=row["account_name"],
                platform=row["platform"],
                health_status=row["health_status"],
                checked_at=_parse_checked_at(row["checked_at"]),
            )
            for row in alert_rows
        )

    queries = (_handoff_query(principal, tenant_id),)
    if principal.is_workspace_admin:
        queries += (
            _sent_query(principal, tenant_id),
            _delivery_failure_query(principal, tenant_id),
        )
    activities: tuple[HomeBusinessActivity, ...] = ()
    for query in queries:
        activity_rows = (await session.execute(query)).mappings().all()
        activities += tuple(HomeBusinessActivity(**row) for row in activity_rows)
    return HomeOverview(
        alerts=alerts,
        activities=tuple(
            sorted(activities, key=lambda event: (event.occurred_at, event.event_id), reverse=True)
        )[:_ACTIVITY_LIMIT],
    )
