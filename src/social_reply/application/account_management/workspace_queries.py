"""Read-only, account-scoped queries for the tenant business workspace."""

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import Select, and_, func, or_, select, union

from social_reply.application.account_management.access import account_read_condition
from social_reply.application.account_management.auth import Principal
from social_reply.infrastructure.database import models

CONTACT_PAGE_SIZE = 50
CONTACT_MAX_PAGE = 1000
CONTACT_CONVERSATION_LIMIT = 20
AGENT_CHOICE_LIMIT = 200


def contact_list_statement(
    principal: Principal,
    tenant_id: str,
    *,
    search: str,
    page: int,
    contact_id: UUID | None = None,
) -> Select:
    if not 1 <= page <= CONTACT_MAX_PAGE or len(search) > 100:
        raise ValueError("contact_query_invalid")
    contact = models.Contact
    account = models.PlatformAccount
    statement = (
        select(
            contact.id,
            contact.display_name,
            contact.external_user_id,
            contact.created_at,
            account.platform,
            account.name.label("account_name"),
            account.brand_id,
            account.id.label("account_id"),
        )
        .join(account, contact.platform_account_id == account.id)
        .where(contact.tenant_id == tenant_id, account_read_condition(principal, tenant_id))
    )
    if contact_id is not None:
        return statement.where(contact.id == contact_id).limit(1)
    if search:
        statement = statement.where(
            or_(
                contact.display_name.icontains(search, autoescape=True),
                contact.external_user_id.icontains(search, autoescape=True),
                account.name.icontains(search, autoescape=True),
            )
        )
    return (
        statement.order_by(contact.created_at.desc(), contact.id.desc())
        .offset((page - 1) * CONTACT_PAGE_SIZE)
        .limit(CONTACT_PAGE_SIZE + 1)
    )


def contact_conversations_statement(
    principal: Principal,
    tenant_id: str,
    contact_id: UUID,
) -> Select:
    conversation = models.Conversation
    contact = models.Contact
    account = models.PlatformAccount
    return (
        select(conversation.id, conversation.channel_type, conversation.created_at)
        .join(
            contact,
            and_(
                conversation.contact_id == contact.id,
                conversation.platform_account_id == contact.platform_account_id,
            ),
        )
        .join(account, conversation.platform_account_id == account.id)
        .where(
            contact.id == contact_id,
            contact.tenant_id == tenant_id,
            conversation.tenant_id == tenant_id,
            account_read_condition(principal, tenant_id),
        )
        .order_by(conversation.created_at.desc(), conversation.id.desc())
        .limit(CONTACT_CONVERSATION_LIMIT)
    )


def report_statements(
    principal: Principal,
    tenant_id: str,
    *,
    days: int,
    end: datetime,
) -> dict[str, Select]:
    if days not in (7, 30) or end.utcoffset() is None:
        raise ValueError("report_window_invalid")
    start = end - timedelta(days=days)
    message = models.Message
    conversation = models.Conversation
    account = models.PlatformAccount
    human = models.HumanWorkItem
    outbox = models.OutboxMessage
    scope = (conversation.tenant_id == tenant_id, account_read_condition(principal, tenant_id))
    messages = (
        select(
            account.platform,
            func.count().filter(message.direction == "inbound").label("inbound"),
            func.count().filter(message.direction == "outbound").label("outbound"),
            func.count(func.distinct(conversation.id)).label("active_conversations"),
            func.count()
            .filter(
                and_(
                    message.direction == "outbound",
                    message.sender_type == "agent",
                )
            )
            .label("human_replies"),
        )
        .select_from(message)
        .join(conversation, message.conversation_id == conversation.id)
        .join(account, conversation.platform_account_id == account.id)
        .where(
            *scope,
            message.private.is_(False),
            message.direction.in_(("inbound", "outbound")),
            message.created_at >= start,
            message.created_at < end,
        )
        .group_by(account.platform)
        .order_by(account.platform)
    )
    conversations = (
        select(func.count().label("new_conversations"))
        .select_from(conversation)
        .join(account, conversation.platform_account_id == account.id)
        .where(*scope, conversation.created_at >= start, conversation.created_at < end)
    )
    human_items = (
        select(human.status, func.count().label("count"))
        .select_from(human)
        .join(conversation, human.conversation_id == conversation.id)
        .join(account, conversation.platform_account_id == account.id)
        .where(
            *scope, human.tenant_id == tenant_id, human.created_at >= start, human.created_at < end
        )
        .group_by(human.status)
        .order_by(human.status)
    )
    outbox_items = (
        select(outbox.status, func.count().label("count"))
        .select_from(outbox)
        .join(conversation, outbox.conversation_id == conversation.id)
        .join(account, conversation.platform_account_id == account.id)
        .where(
            *scope,
            outbox.tenant_id == tenant_id,
            outbox.platform_account_id == account.id,
            outbox.created_at >= start,
            outbox.created_at < end,
        )
        .group_by(outbox.status)
        .order_by(outbox.status)
    )
    return {
        "messages": messages,
        "conversations": conversations,
        "human": human_items,
        "outbox": outbox_items,
    }


def agent_choices_statement(principal: Principal, tenant_id: str) -> Select:
    account_brands = select(models.PlatformAccount.brand_id.label("brand_id")).where(
        account_read_condition(principal, tenant_id)
    )
    # Legacy runtime brands are real persisted scopes, not synthetic default agents.
    if principal.is_workspace_admin and tenant_id in principal.allowed_tenants:
        brands = union(
            account_brands,
            select(models.Agent.legacy_brand_id.label("brand_id")).where(
                models.Agent.tenant_id == tenant_id,
                models.Agent.status == "active",
            ),
            select(models.ReplyBusinessPrompt.brand_id.label("brand_id")).where(
                models.ReplyBusinessPrompt.tenant_id == tenant_id,
            ),
            select(models.KnowledgeDocument.brand_id.label("brand_id")).where(
                models.KnowledgeDocument.tenant_id == tenant_id,
            ),
        ).subquery()
    else:
        brands = account_brands.distinct().subquery()
    return (
        select(brands.c.brand_id, func.coalesce(models.Agent.name, brands.c.brand_id).label("name"))
        .outerjoin(
            models.Agent,
            and_(
                models.Agent.tenant_id == tenant_id,
                models.Agent.legacy_brand_id == brands.c.brand_id,
                models.Agent.status == "active",
            ),
        )
        .order_by(brands.c.brand_id)
        .limit(AGENT_CHOICE_LIMIT)
    )
