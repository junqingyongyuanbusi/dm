"""Read-only unified inbox; mutations stay in the existing human workflow routes."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import Select, and_, false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from social_reply.application.account_management import saas_console
from social_reply.application.account_management.access import account_read_condition
from social_reply.application.account_management.admin import _csrf, _ensure_csrf
from social_reply.application.account_management.auth import Principal
from social_reply.application.account_management.human_workflow import has_unfinished_human_send
from social_reply.application.account_management.saas_ui import (
    definition_list,
    escape,
    format_datetime,
    navigation_icon,
    status_badge,
)
from social_reply.application.account_management.templating import render_template, trusted_html
from social_reply.application.account_management.ui_i18n import get_locale
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory

router = APIRouter()
PAGE_SIZE = 100
CONVERSATION_QUEUES = ("all", "mine", "ai", "human", "resolved")
LEGACY_QUEUES = ("drafts", "delivery")
PLATFORMS = ("facebook", "instagram", "telegram", "x", "whatsapp", "email", "feishu")

_COPY = {
    "title": ("收件箱", "Inbox"),
    "description": (
        "在同一工作台查看真实渠道消息与人工接待。",
        "Real channel messages and human reception in one workspace.",
    ),
    "all": ("全部会话", "All conversations"),
    "mine": ("我的", "Mine"),
    "ai": ("AI 接待", "AI handling"),
    "human": ("待人工", "Human attention"),
    "resolved": ("已解决", "Resolved"),
    "human_completed": (
        "人工处理已完成；自动化状态独立显示",
        "Human handling completed; automation state is shown separately",
    ),
    "drafts": ("草稿审核", "Draft review"),
    "delivery": ("投递异常", "Delivery issues"),
    "search": ("搜索会话", "Search conversations"),
    "search_hint": ("搜索会话、客户或账号", "Search conversations, customers or accounts"),
    "platform": ("平台", "Platform"),
    "all_platforms": ("全部平台", "All platforms"),
    "channel_type": ("渠道类型", "Channel type"),
    "all_channels": ("全部类型", "All types"),
    "dm": ("私信", "Direct message"),
    "comment": ("评论", "Comment"),
    "group": ("群聊", "Group chat"),
    "apply": ("应用筛选", "Apply filters"),
    "empty_title": ("没有匹配的会话", "No matching conversations"),
    "empty_description": (
        "尝试调整筛选条件，或等待渠道收到新消息。",
        "Adjust your filters or wait for a new channel message.",
    ),
    "no_message": ("暂无消息", "No messages yet"),
    "anonymous": ("未命名联系人", "Unnamed contact"),
    "inspector": ("客户详情", "Contact details"),
    "conversation_content": ("会话内容", "Conversation content"),
    "attributes": ("会话属性", "Conversation properties"),
    "identity": ("客户标识", "Customer identity"),
    "recent": ("最近更新 ↓", "Recently updated ↓"),
    "scope_reply": ("可回复 · 授权账号范围", "Reply enabled · Authorized accounts"),
    "scope_read": ("仅查看 · 授权账号范围", "Read only · Authorized accounts"),
    "brand": ("wikiglobal 金融客户服务", "wikiglobal financial customer service"),
    "clear": ("清除筛选", "Clear filters"),
    "non_text": ("非文本消息", "Non-text message"),
    "reply_hint": ("核实客户问题后，输入回复内容…", "Verify the customer request, then write a reply…"),
    "transfer_record": ("会话详情与转交", "Conversation details and transfer"),
    "record": ("查看会话详情", "View conversation details"),
    "history_limit": ("最多显示最近 100 条消息 · UTC", "Up to 100 recent messages · UTC"),
    "contact": ("联系人", "Contact"),
    "external_id": ("渠道用户 ID", "Channel user ID"),
    "account": ("渠道账号", "Channel account"),
    "state": ("会话状态", "Conversation state"),
    "assignee": ("负责人", "Assignee"),
    "unassigned": ("未分配", "Unassigned"),
    "created": ("创建时间", "Created"),
    "select": ("选择会话查看详情", "Select a conversation for details"),
    "back": ("返回会话列表", "Back to conversations"),
    "previous": ("上一页", "Previous"),
    "next": ("下一页", "Next"),
    "count": ("共 {total} 个会话 · 第 {page} 页", "{total} conversations · Page {page}"),
    "takeover": ("开始人工接待", "Start human reception"),
    "claim": ("领取会话", "Claim conversation"),
    "resolve": ("结束人工接待", "Resolve reception"),
    "reply": ("回复内容", "Your reply"),
    "send": ("发送回复", "Send reply"),
    "readonly": ("当前权限仅可查看会话。", "Your current permissions allow viewing only."),
    "claim_required": (
        "需先领取会话，才能发送人工回复。",
        "Claim this conversation before sending a human reply.",
    ),
    "pending": (
        "回复正在投递，确认结果后才能继续操作。",
        "A reply is being delivered. Wait for confirmation before continuing.",
    ),
    "safety": (
        "发送前将再次检查权限、接待归属与会话版本。",
        "Permissions, assignment and conversation version are checked again before sending.",
    ),
    "unknown": ("其他状态", "Other state"),
}


def _labels() -> dict[str, str]:
    language_index = 1 if get_locale() == "en" else 0
    return {key: value[language_index] for key, value in _COPY.items()}


@dataclass(frozen=True)
class ConversationEntry:
    conversation: models.Conversation
    contact: models.Contact
    account: models.PlatformAccount
    latest_message: models.Message | None
    state: str
    work_item: models.HumanWorkItem | None


def _scoped_conversations(principal: Principal, tenant_id: str) -> Select:
    latest_message = aliased(models.Message, name="latest_message")
    latest_message_id = (
        select(models.Message.id)
        .where(models.Message.conversation_id == models.Conversation.id)
        .order_by(models.Message.history_seq.desc())
        .limit(1)
        .correlate(models.Conversation)
        .scalar_subquery()
    )
    return (
        select(
            models.Conversation,
            models.Contact,
            models.PlatformAccount,
            latest_message,
            func.coalesce(models.AutomationState.state, models.PlatformAccount.automation_default),
            models.HumanWorkItem,
        )
        .select_from(models.Conversation)
        .join(
            models.PlatformAccount,
            models.PlatformAccount.id == models.Conversation.platform_account_id,
        )
        .join(
            models.Contact,
            and_(
                models.Contact.id == models.Conversation.contact_id,
                models.Contact.tenant_id == tenant_id,
                models.Contact.platform_account_id == models.Conversation.platform_account_id,
            ),
        )
        .outerjoin(
            models.AutomationState, models.AutomationState.conversation_id == models.Conversation.id
        )
        .outerjoin(latest_message, latest_message.id == latest_message_id)
        .outerjoin(
            models.HumanWorkItem,
            and_(
                models.HumanWorkItem.conversation_id == models.Conversation.id,
                models.HumanWorkItem.tenant_id == tenant_id,
                models.HumanWorkItem.status.in_(("WAITING", "CLAIMED")),
            ),
        )
        .where(
            models.Conversation.tenant_id == tenant_id, account_read_condition(principal, tenant_id)
        )
    )


def _assignment_condition(principal: Principal):
    if principal.user_id is not None:
        identity = models.HumanWorkItem.assigned_user_id == principal.user_id
    elif principal.is_superadmin and principal.session_id is not None:
        identity = and_(
            models.HumanWorkItem.assigned_user_id.is_(None),
            models.HumanWorkItem.assigned_session_id == principal.session_id,
        )
    else:
        return false()
    return and_(
        identity,
        models.HumanWorkItem.status == "CLAIMED",
        models.HumanWorkItem.assigned_actor == principal.actor,
    )


def _resolved_condition(tenant_id: str, state):
    resolved_work = aliased(models.HumanWorkItem, name="resolved_work")
    latest_resolution = (
        select(func.max(resolved_work.resolved_at))
        .where(
            resolved_work.conversation_id == models.Conversation.id,
            resolved_work.tenant_id == tenant_id,
            resolved_work.status == "RESOLVED",
        )
        .correlate(models.Conversation)
        .scalar_subquery()
    )
    # Compare local persistence time with local resolution time, not provider clocks.
    # A delayed inbound received after resolution must reopen the queue membership.
    latest_inbound = (
        select(func.max(models.Message.created_at))
        .where(
            models.Message.conversation_id == models.Conversation.id,
            models.Message.direction == "inbound",
        )
        .correlate(models.Conversation)
        .scalar_subquery()
    )
    return or_(
        state == "CLOSED",
        and_(
            models.HumanWorkItem.id.is_(None),
            latest_resolution.is_not(None),
            or_(latest_inbound.is_(None), latest_resolution >= latest_inbound),
        ),
    )


def build_conversation_query(
    principal: Principal,
    tenant_id: str,
    *,
    queue: str = "all",
    search: str = "",
    platform: str = "",
    channel_type: str = "",
    page: int = 1,
    selected_id: uuid.UUID | None = None,
) -> Select:
    """Apply account authorization and all user filters before the bounded SQL page."""
    statement = _scoped_conversations(principal, tenant_id)
    state = func.coalesce(models.AutomationState.state, models.PlatformAccount.automation_default)
    states = {
        "ai": ("BOT_ACTIVE", "BOT_DRAFT_ONLY"),
        "human": ("HUMAN_ACTIVE", "HANDOFF_PENDING"),
    }
    if queue in states:
        statement = statement.where(state.in_(states[queue]))
    elif queue == "resolved":
        statement = statement.where(_resolved_condition(tenant_id, state))
    elif queue == "mine":
        statement = statement.where(_assignment_condition(principal))
    elif queue != "all":
        raise HTTPException(status_code=422, detail="invalid_inbox_queue")
    if platform:
        statement = statement.where(models.Conversation.platform == platform)
    if channel_type:
        statement = statement.where(models.Conversation.channel_type == channel_type)
    latest_message = statement.column_descriptions[3]["entity"]
    if search.strip():
        statement = statement.where(
            or_(
                models.Contact.display_name.icontains(search.strip(), autoescape=True),
                models.Contact.external_user_id.icontains(search.strip(), autoescape=True),
                models.PlatformAccount.name.icontains(search.strip(), autoescape=True),
                latest_message.text.icontains(search.strip(), autoescape=True),
            )
        )
    if selected_id is not None:
        statement = statement.where(
            or_(
                models.Conversation.id == selected_id,
                models.HumanWorkItem.id == selected_id,
            )
        )
    return (
        statement.order_by(
            func.coalesce(latest_message.created_at, models.Conversation.created_at).desc(),
            models.Conversation.id.desc(),
        )
        .limit(PAGE_SIZE)
        .offset((page - 1) * PAGE_SIZE)
    )


async def _load_messages(
    session: AsyncSession,
    principal: Principal,
    tenant_id: str,
    conversation_id: uuid.UUID,
) -> list[models.Message]:
    statement = (
        select(models.Message)
        .join(models.Conversation, models.Conversation.id == models.Message.conversation_id)
        .join(
            models.PlatformAccount,
            models.PlatformAccount.id == models.Conversation.platform_account_id,
        )
        .where(
            models.Conversation.tenant_id == tenant_id,
            models.Conversation.id == conversation_id,
            account_read_condition(principal, tenant_id),
        )
        .order_by(models.Message.history_seq.desc())
        .limit(PAGE_SIZE)
    )
    newest = list((await session.scalars(statement)).all())
    return list(reversed(newest))


async def _load_workspace(
    session: AsyncSession,
    principal: Principal,
    tenant_id: str,
    *,
    selected_id: uuid.UUID | None,
    queue: str,
    search: str,
    platform: str,
    channel_type: str,
    page: int,
) -> tuple[list[ConversationEntry], ConversationEntry | None, list[models.Message], int, bool]:
    filters = {"queue": queue, "search": search, "platform": platform, "channel_type": channel_type}
    statement = build_conversation_query(principal, tenant_id, page=page, **filters)
    # Count the same scoped relation without rewriting ORM entity join origins.
    count_statement = select(func.count()).select_from(
        statement.order_by(None).limit(None).offset(None).subquery()
    )
    total = int(await session.scalar(count_statement) or 0)
    entries = [ConversationEntry(*row) for row in (await session.execute(statement)).all()]
    selected = entries[0] if entries else None
    if selected_id is not None:
        selection_query = build_conversation_query(
            principal, tenant_id, selected_id=selected_id, **filters
        )
        selected_row = (await session.execute(selection_query)).one_or_none()
        if selected_row is None:
            raise HTTPException(status_code=404, detail="conversation_not_found")
        selected = ConversationEntry(*selected_row)
    messages = (
        []
        if selected is None
        else await _load_messages(
            session,
            principal,
            tenant_id,
            selected.conversation.id,
        )
    )
    pending_send = False
    if selected and (principal.has_capability("reply") or principal.has_capability("takeover")):
        pending_send = await has_unfinished_human_send(
            session,
            tenant_id=tenant_id,
            conversation_id=selected.conversation.id,
        )
    return entries, selected, messages, total, pending_send


def _owns_work(principal: Principal, work_item: models.HumanWorkItem | None) -> bool:
    if (
        work_item is None
        or work_item.status != "CLAIMED"
        or work_item.assigned_actor != principal.actor
    ):
        return False
    return (principal.user_id is not None and work_item.assigned_user_id == principal.user_id) or (
        principal.is_superadmin
        and work_item.assigned_user_id is None
        and principal.session_id is not None
        and work_item.assigned_session_id == principal.session_id
    )


def _action_form(action: str, label: str, fields: dict[str, object], *, content: str = "") -> str:
    hidden_fields = "".join(
        f'<input type="hidden" name="{escape(name)}" value="{escape(value)}">'
        for name, value in fields.items()
    )
    form_class = "unified-inbox-composer" if content else "unified-inbox-reception-form"
    button_class = "saas-button primary" if content else "saas-button small"
    return (
        f'<form class="saas-form {form_class}" method="post" action="{escape(action)}">'
        f'{hidden_fields}{content}<button class="{button_class}" type="submit">'
        f"{escape(label)}</button></form>"
    )


def _reception_actions(
    tenant_id: str,
    principal: Principal,
    entry: ConversationEntry,
    *,
    csrf_token: str,
    pending_send: bool,
) -> str:
    if not principal.has_capability("takeover") or pending_send:
        return ""
    labels = _labels()
    root = saas_console._tenant_root(tenant_id)
    work_item = entry.work_item
    fields = {"csrf_token": csrf_token, "return_to": "inbox"}
    if work_item is None and entry.state in {
        "BOT_ACTIVE",
        "BOT_DRAFT_ONLY",
        "HANDOFF_PENDING",
        "HUMAN_ACTIVE",
    }:
        return _action_form(
            f"{root}/conversations/{entry.conversation.id}/start-reception",
            labels["takeover"],
            fields,
        )
    if work_item is None:
        return ""
    fields = {**fields, "expected_version": work_item.version}
    if work_item.status == "WAITING":
        return _action_form(f"{root}/work-items/{work_item.id}/claim", labels["claim"], fields)
    if _owns_work(principal, work_item):
        return _action_form(f"{root}/work-items/{work_item.id}/resolve", labels["resolve"], fields)
    return ""


def render_conversation_actions(
    tenant_id: str,
    principal: Principal,
    entry: ConversationEntry,
    messages: list[models.Message],
    *,
    csrf_token: str,
    pending_send: bool = False,
    include_reception_controls: bool = True,
) -> str:
    labels = _labels()
    if not (principal.has_capability("reply") or principal.has_capability("takeover")):
        return f'<p class="saas-muted">{escape(labels["readonly"])}</p>'
    if pending_send:
        return f'<p class="saas-alert" role="status">{escape(labels["pending"])}</p>'
    root = saas_console._tenant_root(tenant_id)
    work_item = entry.work_item
    fields = {"csrf_token": csrf_token, "return_to": "inbox"}
    if work_item is not None:
        fields = {**fields, "expected_version": work_item.version}
    actions = (
        _reception_actions(
            tenant_id, principal, entry, csrf_token=csrf_token, pending_send=pending_send
        )
        if include_reception_controls
        else ""
    )
    reply_target = next(
        (message for message in reversed(messages) if message.direction == "inbound"), None
    )
    if principal.has_capability("reply") and _owns_work(principal, work_item) and reply_target:
        actions += _action_form(
            f"{root}/conversations/{entry.conversation.id}/reply",
            labels["send"],
            {
                **fields,
                "reply_to_message_id": reply_target.id,
                "work_item_id": work_item.id,
                "idempotency_key": uuid.uuid4(),
            },
            content=(
                f'<label for="unified-reply">{escape(labels["reply"])}</label>'
                '<textarea id="unified-reply" name="text" rows="3" '
                f'placeholder="{escape(labels["reply_hint"])}" '
                'maxlength="10000" required aria-describedby="unified-reply-safety"></textarea>'
            ),
        )
    elif principal.has_capability("reply"):
        actions += f'<p class="saas-muted">{escape(labels["claim_required"])}</p>'
    return actions + (
        '<p id="unified-reply-safety" class="saas-muted unified-inbox-safety">'
        f'{escape(labels["safety"])}</p>'
    )


def _inbox_item(entry: ConversationEntry) -> saas_console.InboxItem:
    return saas_console.InboxItem(
        item_id=entry.conversation.id,
        conversation_id=entry.conversation.id,
        queue="all",
        title=entry.contact.display_name or _labels()["anonymous"],
        platform=entry.conversation.platform,
        channel_type=entry.conversation.channel_type,
        account_name=entry.account.name,
        status=entry.state,
        reason="",
        created_at=entry.conversation.created_at,
        assigned_actor=entry.work_item.assigned_actor if entry.work_item else None,
    )


def _inbox_url(tenant_id: str, **parameters: object) -> str:
    query = urlencode({key: value for key, value in parameters.items() if value not in (None, "")})
    return f"{saas_console._tenant_root(tenant_id)}/inbox?{query}"


def _queue_links(tenant_id: str, principal: Principal, queue: str, filters: dict) -> list[dict]:
    queues = CONVERSATION_QUEUES + (
        LEGACY_QUEUES if principal.is_admin and principal.has_capability("reply") else ()
    )
    return [
        {
            "key": key,
            "label": _labels()[key],
            "active": key == queue,
            "href": _inbox_url(tenant_id, **{**filters, "queue": key, "page": 1}),
        }
        for key in queues
    ]


def _conversation_rows(
    tenant_id: str,
    entries: list[ConversationEntry],
    selected: ConversationEntry | None,
    filters: dict,
) -> list[dict]:
    labels = _labels()
    return [
        {
            "href": _inbox_url(tenant_id, **filters, item_id=entry.conversation.id),
            "active": selected is not None and selected.conversation.id == entry.conversation.id,
            "title": entry.contact.display_name or labels["anonymous"],
            "initial": (entry.contact.display_name or labels["anonymous"])[:1],
            "preview": (entry.latest_message.text or labels["no_message"])[:180]
            if entry.latest_message
            else labels["no_message"],
            "time": format_datetime(
                entry.latest_message.created_at
                if entry.latest_message
                else entry.conversation.created_at
            ),
            "platform": entry.conversation.platform,
            "channel_type": entry.conversation.channel_type,
            "account": entry.account.name,
            "badge": trusted_html(status_badge(entry.state)),
            "assignee": entry.work_item.assigned_actor
            if entry.work_item and entry.work_item.assigned_actor
            else labels["unassigned"],
        }
        for entry in entries
    ]


def _inspector(entry: ConversationEntry | None, *, queue: str = "all") -> str:
    labels = _labels()
    if entry is None:
        return f'<p class="saas-muted">{escape(labels["select"])}</p>'
    completion_notice = (
        f'<p class="saas-muted">{escape(labels["human_completed"])}</p>'
        if queue == "resolved" and entry.state != "CLOSED"
        else ""
    )
    contact_name = entry.contact.display_name or labels["anonymous"]
    channel_name = labels.get(entry.conversation.channel_type, entry.conversation.channel_type)
    return (
        '<div class="unified-inbox-profile">'
        f'<span class="unified-inbox-avatar" aria-hidden="true">{escape(contact_name[:1])}</span>'
        f'<h3>{escape(contact_name)}</h3>'
        f'<p>{escape(entry.conversation.platform.title())} · {escape(channel_name)}</p></div>'
        f'<section class="unified-inbox-detail-section"><h3>{escape(labels["attributes"])}</h3>'
        f'<div class="unified-inbox-property"><span>{escape(labels["state"])}</span>'
        f'{status_badge(entry.state)}</div>{completion_notice}'
        + definition_list(
            (
                (
                    labels["assignee"],
                    (entry.work_item.assigned_actor or labels["unassigned"])
                    if entry.work_item
                    else labels["unassigned"],
                ),
                (labels["channel_type"], channel_name),
                (
                    labels["created"],
                    format_datetime(entry.conversation.created_at, include_year=True),
                ),
            )
        )
        + '</section><section class="unified-inbox-detail-section">'
        + f'<h3>{escape(labels["identity"])}</h3>'
        + definition_list(
            (
                (labels["external_id"], entry.contact.external_user_id),
                ("Conversation ID", entry.conversation.id),
            )
        )
        + '</section><section class="unified-inbox-detail-section">'
        + f'<h3>{escape(labels["account"])}</h3>'
        + f'<strong>{escape(entry.account.name)}</strong>'
        + f'<p class="saas-muted">{escape(entry.conversation.platform.title())}</p></section>'
    )


def render_inbox_body(
    *,
    tenant_id: str,
    principal: Principal,
    entries: list[ConversationEntry],
    selected: ConversationEntry | None,
    messages: list[models.Message],
    queue: str,
    search: str,
    platform: str,
    channel_type: str,
    page: int,
    total: int,
    csrf_token: str,
    has_selection: bool,
    pending_send: bool,
) -> str:
    labels = _labels()
    filters = {
        "queue": queue,
        "q": search,
        "platform": platform,
        "channel_type": channel_type,
        "page": page,
    }
    actions = (
        ""
        if selected is None
        else render_conversation_actions(
            tenant_id,
            principal,
            selected,
            messages,
            csrf_token=csrf_token,
            pending_send=pending_send,
            include_reception_controls=False,
        )
    )
    return render_template(
        "tenant/unified_inbox.html",
        labels=labels,
        selected_item=_inbox_item(selected) if selected else None,
        messages=messages,
        format_datetime=format_datetime,
        selected_badge=trusted_html(status_badge(selected.state)) if selected else "",
        scope_label=(
            labels["scope_reply"] if principal.has_capability("reply") else labels["scope_read"]
        ),
        can_reply=principal.has_capability("reply"),
        queue=queue,
        search=search,
        platform=platform,
        channel_type=channel_type,
        platforms=PLATFORMS,
        channel_types=tuple(
            dict.fromkeys(
                ("dm", "comment", "group", "email", *([channel_type] if channel_type else []))
            )
        ),
        form_action=f"{saas_console._tenant_root(tenant_id)}/inbox",
        queue_links=_queue_links(tenant_id, principal, queue, filters),
        rows=_conversation_rows(tenant_id, entries, selected, filters),
        count_label=labels["count"].format(total=total, page=page),
        previous_href=_inbox_url(tenant_id, **{**filters, "page": page - 1}) if page > 1 else "",
        next_href=_inbox_url(tenant_id, **{**filters, "page": page + 1})
        if page * PAGE_SIZE < total
        else "",
        back_href=_inbox_url(tenant_id, **filters),
        clear_href=_inbox_url(tenant_id, queue="all"),
        record_href=(
            f"{saas_console._tenant_root(tenant_id)}/conversations/{selected.conversation.id}"
            if selected
            else ""
        ),
        record_label=(
            labels["transfer_record"]
            if selected
            and principal.has_capability("takeover")
            and _owns_work(principal, selected.work_item)
            and not pending_send
            else labels["record"]
        ),
        has_selection=has_selection,
        reception_html=trusted_html(
            _reception_actions(
                tenant_id, principal, selected, csrf_token=csrf_token, pending_send=pending_send
            )
            if selected
            else ""
        ),
        actions_html=trusted_html(actions),
        inspector_html=trusted_html(_inspector(selected, queue=queue)),
        inbox_icon=trusted_html(navigation_icon("inbox")),
    )


@router.get("/app/t/{tenant_id}/inbox", response_class=HTMLResponse)
async def tenant_unified_inbox(
    request: Request,
    tenant_id: str,
    queue: str = "all",
    item_id: str = "",
    q: Annotated[str, Query(max_length=200)] = "",
    platform: Annotated[str, Query(max_length=32)] = "",
    channel_type: Annotated[str, Query(max_length=64)] = "",
    page: Annotated[int, Query(ge=1, le=10000)] = 1,
) -> Response:
    principal = await saas_console._require_tenant_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    principal.require_capability("inbox.read")
    if queue in LEGACY_QUEUES:
        if not principal.is_admin or not principal.has_capability("reply"):
            raise HTTPException(status_code=403, detail="inbox_queue_access_denied")
        return await saas_console.tenant_inbox(request, tenant_id, queue=queue, item_id=item_id)
    if queue not in CONVERSATION_QUEUES:
        raise HTTPException(status_code=422, detail="invalid_inbox_queue")
    if platform and platform not in PLATFORMS:
        raise HTTPException(status_code=422, detail="invalid_inbox_platform")
    try:
        selected_id = uuid.UUID(item_id) if item_id else None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid_inbox_item") from exc
    async with get_session_factory()() as session:
        entries, selected, messages, total, pending_send = await _load_workspace(
            session,
            principal,
            tenant_id,
            selected_id=selected_id,
            queue=queue,
            search=q,
            platform=platform,
            channel_type=channel_type,
            page=page,
        )
    csrf_token = _csrf(request)
    body = render_inbox_body(
        tenant_id=tenant_id,
        principal=principal,
        entries=entries,
        selected=selected,
        messages=messages,
        queue=queue,
        search=q,
        platform=platform,
        channel_type=channel_type,
        page=page,
        total=total,
        csrf_token=csrf_token,
        has_selection=selected_id is not None,
        pending_send=pending_send,
    )
    labels = _labels()
    response = saas_console._render_page(
        principal=principal,
        tenant_id=tenant_id,
        title=labels["title"],
        description=labels["description"],
        body=body,
        active_navigation="inbox",
        inbox_count=total,
        workbench=True,
    )
    return _ensure_csrf(response, request, csrf_token)
