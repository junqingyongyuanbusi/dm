"""Read-only dashboard facts, scoped before aggregation and never provider-probed."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from urllib.parse import quote, urlencode

from sqlalchemy import Date, Select, and_, case, cast, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.application.account_management.access import account_read_condition
from social_reply.application.account_management.auth import Principal
from social_reply.application.account_management.ui_i18n import get_locale
from social_reply.application.reply_review.queries import reviewable_draft_condition
from social_reply.infrastructure.database import models

_TREND_DAYS = 7
_ATTENTION_PER_KIND = 3
_ACTIVE_WORK_STATUSES = ("WAITING", "CLAIMED")
_AI_STATES = ("BOT_ACTIVE", "BOT_DRAFT_ONLY")
_META_FAILURES = ("REAUTH_REQUIRED", "ERROR")
_FEISHU_FAILURES = ("CREDENTIAL_INVALID", "BOT_NOT_ACTIVE", "BOT_ID_MISMATCH", "ERROR")


@dataclass(frozen=True)
class HomeTrendDay:
    day: date
    received: int
    ai: int


@dataclass(frozen=True)
class HomeChannelStat:
    platform: str
    accounts: int
    received: int


@dataclass(frozen=True)
class HomeAttentionItem:
    title: str
    description: str
    href: str
    kind: str


@dataclass(frozen=True)
class HomeDashboardData:
    pending_count: int
    ai_count: int
    account_count: int
    enabled_account_count: int
    resolved_count: int
    trend: tuple[HomeTrendDay, ...]
    channels: tuple[HomeChannelStat, ...]
    attention: tuple[HomeAttentionItem, ...]
    published_knowledge_count: int = 0
    draft_knowledge_count: int = 0
    pending_draft_count: int = 0


def _scoped_conversations(principal: Principal, tenant_id: str) -> Select:
    return (
        select(models.Conversation.id)
        .select_from(models.Conversation)
        .join(
            models.PlatformAccount,
            models.PlatformAccount.id == models.Conversation.platform_account_id,
        )
        .where(
            models.Conversation.tenant_id == tenant_id, account_read_condition(principal, tenant_id)
        )
    )


def _conversation_counts(principal: Principal, tenant_id: str) -> Select:
    # Import lazily: unified_inbox also imports the console rendering module.
    from social_reply.application.account_management.unified_inbox import _resolved_condition

    state = func.coalesce(models.AutomationState.state, models.PlatformAccount.automation_default)
    conversation_count = func.count(func.distinct(models.Conversation.id))
    return (
        _scoped_conversations(principal, tenant_id)
        .outerjoin(
            models.AutomationState, models.AutomationState.conversation_id == models.Conversation.id
        )
        .outerjoin(
            models.HumanWorkItem,
            and_(
                models.HumanWorkItem.conversation_id == models.Conversation.id,
                models.HumanWorkItem.tenant_id == tenant_id,
                models.HumanWorkItem.status.in_(_ACTIVE_WORK_STATUSES),
            ),
        )
        .with_only_columns(
            conversation_count.filter(models.HumanWorkItem.id.is_not(None)).label("pending_count"),
            conversation_count.filter(
                and_(
                    state.in_(_AI_STATES),
                    models.HumanWorkItem.id.is_(None),
                )
            ).label("ai_count"),
            conversation_count.filter(_resolved_condition(tenant_id, state)).label(
                "resolved_count"
            ),
        )
    )


def _public_message_days(
    principal: Principal,
    tenant_id: str,
    start: datetime,
    now: datetime,
    *,
    bot: bool = False,
) -> Select:
    message_day = cast(func.timezone("UTC", models.Message.created_at), Date)
    statement = (
        _scoped_conversations(principal, tenant_id)
        .join(models.Message, models.Message.conversation_id == models.Conversation.id)
        .where(
            models.Message.private.is_(False),
            models.Message.direction == ("outbound" if bot else "inbound"),
            models.Message.created_at >= start,
            models.Message.created_at <= now,
        )
        .with_only_columns(
            models.Conversation.id.label("conversation_id"),
            models.PlatformAccount.id.label("account_id"),
            message_day.label("day"),
        )
    )
    if bot:
        statement = statement.where(models.Message.sender_type == "bot")
    return statement.distinct()


async def _load_trend(
    session: AsyncSession,
    principal: Principal,
    tenant_id: str,
    start: datetime,
    now: datetime,
) -> tuple[HomeTrendDay, ...]:
    received = _public_message_days(principal, tenant_id, start, now).cte("received_days")
    replied = _public_message_days(principal, tenant_id, start, now, bot=True).cte("bot_days")
    statement = (
        select(
            received.c.day,
            func.count().label("received"),
            func.count(replied.c.conversation_id).label("ai"),
        )
        .select_from(received)
        .outerjoin(
            replied,
            and_(
                replied.c.conversation_id == received.c.conversation_id,
                replied.c.day == received.c.day,
            ),
        )
        .group_by(received.c.day)
        .order_by(received.c.day)
    )
    rows = (await session.execute(statement)).mappings().all()
    by_day = {row["day"]: HomeTrendDay(**row) for row in rows}
    days = tuple(start.date() + timedelta(days=offset) for offset in range(_TREND_DAYS))
    return tuple(by_day.get(day, HomeTrendDay(day=day, received=0, ai=0)) for day in days)


async def _load_channels(
    session: AsyncSession,
    principal: Principal,
    tenant_id: str,
    start: datetime,
    now: datetime,
) -> tuple[HomeChannelStat, ...]:
    received = _public_message_days(principal, tenant_id, start, now).subquery()
    statement = (
        select(
            models.PlatformAccount.platform,
            func.count(func.distinct(models.PlatformAccount.id)).label("accounts"),
            func.count(func.distinct(received.c.conversation_id)).label("received"),
        )
        .select_from(models.PlatformAccount)
        .outerjoin(received, received.c.account_id == models.PlatformAccount.id)
        .where(account_read_condition(principal, tenant_id))
        .group_by(models.PlatformAccount.platform)
        .order_by(models.PlatformAccount.platform)
    )
    return tuple(HomeChannelStat(**row) for row in (await session.execute(statement)).mappings())


def _human_attention_query(principal: Principal, tenant_id: str) -> Select:
    return (
        _scoped_conversations(principal, tenant_id)
        .join(
            models.HumanWorkItem,
            and_(
                models.HumanWorkItem.conversation_id == models.Conversation.id,
                models.HumanWorkItem.tenant_id == tenant_id,
                models.HumanWorkItem.status.in_(_ACTIVE_WORK_STATUSES),
            ),
        )
        .join(
            models.Contact,
            and_(
                models.Contact.id == models.Conversation.contact_id,
                models.Contact.tenant_id == tenant_id,
                models.Contact.platform_account_id == models.PlatformAccount.id,
            ),
        )
        .with_only_columns(
            models.Conversation.id.label("conversation_id"),
            models.Contact.display_name,
            models.HumanWorkItem.status,
        )
        .order_by(
            models.HumanWorkItem.priority.desc(),
            models.HumanWorkItem.created_at,
            models.HumanWorkItem.id,
        )
        .limit(_ATTENTION_PER_KIND)
    )


def _channel_attention_query(principal: Principal, tenant_id: str) -> Select:
    account = models.PlatformAccount
    meta_status = account.config["meta_health_status"].astext
    feishu_status = account.config["feishu_health_status"].astext
    return (
        select(
            account.id,
            account.name,
            case(
                (account.status == "DISABLED", "DISABLED"),
                (account.platform == "feishu", feishu_status),
                else_=meta_status,
            ).label("health_status"),
        )
        .where(
            account_read_condition(principal, tenant_id),
            or_(
                account.status == "DISABLED",
                and_(
                    account.platform.in_(("facebook", "instagram")), meta_status.in_(_META_FAILURES)
                ),
                and_(account.platform == "feishu", feishu_status.in_(_FEISHU_FAILURES)),
            ),
        )
        .order_by(account.name, account.id)
        .limit(_ATTENTION_PER_KIND)
    )


def _description(status: str) -> str:
    descriptions = {
        "WAITING": ("等待人工处理", "Awaiting human handling"),
        "CLAIMED": ("人工接待进行中", "Human handling in progress"),
        "DISABLED": ("账号已停用", "Account disabled"),
        "REAUTH_REQUIRED": ("已记录：账号需要重新授权", "Recorded: reauthorization required"),
        "CREDENTIAL_INVALID": ("已记录：凭据无效", "Recorded: invalid credentials"),
        "BOT_NOT_ACTIVE": ("已记录：机器人未激活", "Recorded: bot inactive"),
        "BOT_ID_MISMATCH": ("已记录：机器人身份不匹配", "Recorded: bot identity mismatch"),
        "ERROR": ("已记录：渠道配置检查异常", "Recorded: channel configuration check failed"),
    }
    return descriptions[status][1 if get_locale() == "en" else 0]


async def _load_attention(
    session: AsyncSession,
    principal: Principal,
    tenant_id: str,
) -> tuple[HomeAttentionItem, ...]:
    root = f"/app/t/{quote(tenant_id, safe='')}"
    work_rows = (await session.execute(_human_attention_query(principal, tenant_id))).mappings()
    human_items = tuple(
        HomeAttentionItem(
            title=row["display_name"]
            or ("Unnamed contact" if get_locale() == "en" else "未命名联系人"),
            description=_description(row["status"]),
            href=f"{root}/inbox?{urlencode({'item_id': row['conversation_id']})}",
            kind="human",
        )
        for row in work_rows
    )
    account_rows = (
        await session.execute(_channel_attention_query(principal, tenant_id))
    ).mappings()
    channel_items = tuple(
        HomeAttentionItem(
            title=row["name"],
            description=_description(row["health_status"]),
            href=f"{root}/channels/accounts/{row['id']}",
            kind="channel",
        )
        for row in account_rows
    )
    return human_items + channel_items


def _knowledge_counts(principal: Principal, tenant_id: str) -> Select:
    visible_brands = select(models.PlatformAccount.brand_id).where(
        account_read_condition(principal, tenant_id),
    )
    return select(
        func.count()
        .filter(models.KnowledgeDocument.status == "published")
        .label("published_knowledge_count"),
        func.count()
        .filter(models.KnowledgeDocument.status == "draft")
        .label("draft_knowledge_count"),
    ).where(
        models.KnowledgeDocument.tenant_id == tenant_id,
        models.KnowledgeDocument.brand_id.in_(visible_brands),
    )


async def _load_pending_drafts(session: AsyncSession, principal: Principal, tenant_id: str) -> int:
    if not principal.is_workspace_admin:
        return 0
    statement = (
        _scoped_conversations(principal, tenant_id)
        .join(models.ReplyDecision, models.ReplyDecision.conversation_id == models.Conversation.id)
        .where(models.ReplyDecision.tenant_id == tenant_id, reviewable_draft_condition())
        .with_only_columns(func.count(models.ReplyDecision.id))
    )
    return int(await session.scalar(statement) or 0)


async def load_home_dashboard(
    session: AsyncSession,
    principal: Principal,
    tenant_id: str,
    now: datetime,
) -> HomeDashboardData:
    """Load a current snapshot plus UTC calendar-day history, in the caller's transaction.

    Trend uses local Message.created_at, public inbound distinct conversations per day,
    and the subset with a public bot outbound on that same day, bounded inclusively by
    now. Channel received counts are distinct across the entire seven-day window.
    Snapshot cards are current memberships, not mutually exclusive historical totals.
    Account enabled means status=active only, not connected or healthy.
    """
    principal.require_tenant(tenant_id)
    principal.require_capability("home.read")
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ValueError("home dashboard now must be timezone-aware UTC")
    start = datetime.combine(now.date() - timedelta(days=_TREND_DAYS - 1), datetime.min.time(), UTC)
    await session.execute(text("SET LOCAL statement_timeout = '5000ms'"))
    account_counts = (
        (
            await session.execute(
                select(
                    func.count().label("account_count"),
                    func.count()
                    .filter(models.PlatformAccount.status == "active")
                    .label("enabled_account_count"),
                ).where(account_read_condition(principal, tenant_id))
            )
        )
        .mappings()
        .one()
    )
    conversation_counts = (
        (await session.execute(_conversation_counts(principal, tenant_id))).mappings().one()
    )
    knowledge_counts = (
        (await session.execute(_knowledge_counts(principal, tenant_id))).mappings().one()
    )
    return HomeDashboardData(
        **account_counts,
        **conversation_counts,
        **knowledge_counts,
        trend=await _load_trend(session, principal, tenant_id, start, now),
        channels=await _load_channels(session, principal, tenant_id, start, now),
        attention=await _load_attention(session, principal, tenant_id),
        pending_draft_count=await _load_pending_drafts(session, principal, tenant_id),
    )
