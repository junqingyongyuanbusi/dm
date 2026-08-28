import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote, urlencode, urlsplit

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import aliased

from social_reply.application.account_management.admin import (
    _csrf,
    _form,
    _require_csrf,
    _secure_cookie,
    _submit_form,
)
from social_reply.application.account_management.auth import Principal, current_principal
from social_reply.application.account_management.human_workflow import (
    HumanWorkflowError,
    claim_human_work_item,
    resolve_human_work_item,
    send_human_reply,
)
from social_reply.application.account_management.jobs import public_job
from social_reply.application.account_management.meta_credentials import (
    facebook_app_credentials,
    instagram_app_credentials,
)
from social_reply.application.account_management.saas_ui import (
    definition_list,
    empty_state,
    escape,
    format_age,
    format_datetime,
    metric_card,
    primary_action,
    render_saas_page,
    safe_json_details,
    secondary_action,
    status_badge,
    tabs,
)
from social_reply.application.account_management.x_app import x_app_credentials
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.shared.config import get_settings

router = APIRouter(tags=["saas-console"])


@dataclass(frozen=True)
class InboxSummary:
    human_count: int
    draft_count: int
    delivery_count: int
    oldest_human_at: datetime | None
    oldest_draft_at: datetime | None
    oldest_delivery_at: datetime | None

    @property
    def total(self) -> int:
        return self.human_count + self.draft_count + self.delivery_count


@dataclass(frozen=True)
class InboxItem:
    item_id: uuid.UUID
    conversation_id: uuid.UUID
    queue: str
    title: str
    platform: str
    channel_type: str
    account_name: str
    status: str
    reason: str
    created_at: datetime
    work_item_version: int | None = None


@dataclass(frozen=True)
class JourneyRow:
    job: models.DecisionJob
    conversation: models.Conversation
    account: models.PlatformAccount
    contact: models.Contact
    decision: models.ReplyDecision | None


async def _require_web_principal(request: Request) -> Principal | Response:
    principal = await current_principal(request)
    return_target = request.url.path
    if request.url.query:
        return_target = f"{return_target}?{request.url.query}"
    encoded_next = urlencode({"next": return_target})
    if principal is None:
        return RedirectResponse(
            f"/auth/login?{encoded_next}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    if principal.must_change_password:
        return RedirectResponse(
            f"/auth/change-password?{encoded_next}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    return principal


async def _require_tenant_principal(
    request: Request,
    tenant_id: str,
) -> Principal | Response:
    principal = await _require_web_principal(request)
    if isinstance(principal, Response):
        return principal
    principal.require_tenant(tenant_id)
    return principal


async def _require_tenant_admin_principal(
    request: Request,
    tenant_id: str,
) -> Principal | Response:
    principal = await _require_tenant_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    principal.require_admin()
    return principal


def _tenant_root(tenant_id: str) -> str:
    return f"/app/t/{quote(tenant_id, safe='')}"


def _agent_root(tenant_id: str, agent_id: str) -> str:
    return f"{_tenant_root(tenant_id)}/agents/{quote(agent_id, safe='')}"


def _account_scope_condition(principal: Principal, tenant_id: str):
    tenant_condition = models.PlatformAccount.tenant_id == tenant_id
    if principal.is_admin:
        return tenant_condition
    return and_(
        tenant_condition,
        models.PlatformAccount.owner_user_id == principal.user_id,
    )


async def _load_inbox_summary(
    session,
    principal: Principal,
    tenant_id: str,
) -> InboxSummary:
    human_condition = and_(
        models.HumanWorkItem.tenant_id == tenant_id,
        models.HumanWorkItem.status.in_(("WAITING", "CLAIMED")),
    )
    draft_condition = and_(
        models.ReplyDecision.tenant_id == tenant_id,
        models.ReplyDecision.action == "draft",
        or_(
            models.ReplyDecision.review_action.is_(None),
            models.ReplyDecision.review_action == "PENDING",
        ),
        models.ReplyDecision.review_outbox_id.is_(None),
    )
    delivery_condition = and_(
        models.OutboxMessage.tenant_id == tenant_id,
        models.OutboxMessage.status.in_(("FAILED", "NEEDS_REVIEW")),
    )
    human_count, oldest_human_at = (
        await session.execute(
            select(func.count(), func.min(models.HumanWorkItem.created_at))
            .join(
                models.Conversation,
                models.HumanWorkItem.conversation_id == models.Conversation.id,
            )
            .join(
                models.PlatformAccount,
                models.Conversation.platform_account_id == models.PlatformAccount.id,
            )
            .where(human_condition, _account_scope_condition(principal, tenant_id))
        )
    ).one()
    if not principal.is_admin:
        return InboxSummary(
            human_count=int(human_count),
            draft_count=0,
            delivery_count=0,
            oldest_human_at=oldest_human_at,
            oldest_draft_at=None,
            oldest_delivery_at=None,
        )
    draft_count, oldest_draft_at = (
        await session.execute(
            select(func.count(), func.min(models.ReplyDecision.created_at))
            .join(
                models.Conversation,
                models.ReplyDecision.conversation_id == models.Conversation.id,
            )
            .join(
                models.PlatformAccount,
                models.Conversation.platform_account_id == models.PlatformAccount.id,
            )
            .where(draft_condition, _account_scope_condition(principal, tenant_id))
        )
    ).one()
    delivery_count, oldest_delivery_at = (
        await session.execute(
            select(func.count(), func.min(models.OutboxMessage.created_at))
            .join(
                models.PlatformAccount,
                models.OutboxMessage.platform_account_id == models.PlatformAccount.id,
            )
            .where(delivery_condition, _account_scope_condition(principal, tenant_id))
        )
    ).one()
    return InboxSummary(
        human_count=int(human_count),
        draft_count=int(draft_count),
        delivery_count=int(delivery_count),
        oldest_human_at=oldest_human_at,
        oldest_draft_at=oldest_draft_at,
        oldest_delivery_at=oldest_delivery_at,
    )


async def _load_agent_ids(
    session,
    principal: Principal,
    tenant_id: str,
) -> list[str]:
    account_brands = set(
        (
            await session.execute(
                select(models.PlatformAccount.brand_id).where(
                    _account_scope_condition(principal, tenant_id)
                )
            )
        ).scalars()
    )
    prompt_brands = set(
        (
            await session.execute(
                select(models.ReplyBusinessPrompt.brand_id).where(
                    models.ReplyBusinessPrompt.tenant_id == tenant_id
                )
            )
        ).scalars()
    )
    knowledge_brands = set(
        (
            await session.execute(
                select(models.KnowledgeDocument.brand_id).where(
                    models.KnowledgeDocument.tenant_id == tenant_id
                )
            )
        ).scalars()
    )
    return sorted(account_brands | prompt_brands | knowledge_brands | {"default"})


def _display_agent_name(agent_id: str) -> str:
    if agent_id == "default":
        return "默认客户支持 Agent"
    normalized = agent_id.replace("_", " ").replace("-", " ").strip()
    return f"{normalized.title()} Agent"


def _render_page(
    *,
    principal: Principal,
    tenant_id: str,
    title: str,
    description: str,
    body: str,
    active_navigation: str,
    inbox_count: int,
    primary_action_html: str = "",
    breadcrumbs: tuple[tuple[str, str | None], ...] = (),
) -> HTMLResponse:
    return HTMLResponse(
        render_saas_page(
            principal=principal,
            title=title,
            description=description,
            body=body,
            active_navigation=active_navigation,
            tenant_id=tenant_id,
            primary_action_html=primary_action_html,
            breadcrumbs=breadcrumbs,
            inbox_count=inbox_count,
        )
    )


@router.get("/app", response_class=HTMLResponse)
async def tenant_selector(request: Request) -> Response:
    principal = await _require_web_principal(request)
    if isinstance(principal, Response):
        return principal
    tenants = sorted(principal.allowed_tenants)
    if len(tenants) == 1:
        return RedirectResponse(
            _tenant_root(tenants[0]),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    tenant_cards = "".join(
        '<a class="saas-card saas-agent-card" '
        f'href="{escape(_tenant_root(tenant_id))}">'
        f"<div><h2>{escape(tenant_id)}</h2>"
        '<p class="saas-muted">进入租户工作区，查看 Agent、知识、收件箱和运行链路。</p>'
        '</div><span class="saas-button">打开工作区 →</span></a>'
        for tenant_id in tenants
    )
    body = (
        f'<div class="saas-grid two">{tenant_cards}</div>'
        if tenant_cards
        else empty_state("没有可访问的 Tenant", "请联系系统管理员分配工作区权限。")
    )
    return HTMLResponse(
        render_saas_page(
            principal=principal,
            title="选择 Tenant",
            description="选择本次要进入的租户工作区。跨租户系统操作仍在系统后台完成。",
            body=body,
            active_navigation="home",
            tenant_id=None,
        )
    )


@router.get("/app/t/{tenant_id}", response_class=HTMLResponse)
async def tenant_home(request: Request, tenant_id: str) -> Response:
    principal = await _require_tenant_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    now = datetime.now(UTC)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    async with get_session_factory()() as session:
        inbox_summary = await _load_inbox_summary(session, principal, tenant_id)
        message_count = await session.scalar(
            select(func.count())
            .select_from(models.Message)
            .join(
                models.Conversation,
                models.Message.conversation_id == models.Conversation.id,
            )
            .join(
                models.PlatformAccount,
                models.Conversation.platform_account_id == models.PlatformAccount.id,
            )
            .where(
                models.Conversation.tenant_id == tenant_id,
                _account_scope_condition(principal, tenant_id),
                models.Message.created_at >= today_start,
            )
        )
        account_count = await session.scalar(
            select(func.count()).where(_account_scope_condition(principal, tenant_id))
        )
        active_account_count = await session.scalar(
            select(func.count()).where(
                _account_scope_condition(principal, tenant_id),
                models.PlatformAccount.status == "active",
            )
        )
        published_knowledge_count = await session.scalar(
            select(func.count()).where(
                models.KnowledgeDocument.tenant_id == tenant_id,
                models.KnowledgeDocument.status == "published",
            )
        )
        recent_audits = (
            (
                await session.execute(
                    select(models.AuditLog)
                    .where(
                        models.AuditLog.tenant_id == tenant_id,
                        *(
                            ()
                            if principal.is_admin
                            else (models.AuditLog.actor == principal.actor,)
                        ),
                    )
                    .order_by(models.AuditLog.created_at.desc())
                    .limit(5)
                )
            )
            .scalars()
            .all()
        )
        agent_ids = await _load_agent_ids(session, principal, tenant_id)

    next_action = _home_next_action(
        principal,
        tenant_id,
        inbox_summary,
        int(account_count or 0),
    )
    if principal.is_admin:
        attention_cards = "".join(
            (
                metric_card(
                    inbox_summary.human_count,
                    "待人工",
                    detail=f"最老等待 {format_age(inbox_summary.oldest_human_at)}",
                ),
                metric_card(
                    inbox_summary.draft_count,
                    "待审核草稿",
                    detail=f"最老等待 {format_age(inbox_summary.oldest_draft_at)}",
                ),
                metric_card(
                    inbox_summary.delivery_count,
                    "投递异常",
                    detail=f"最老等待 {format_age(inbox_summary.oldest_delivery_at)}",
                ),
            )
        )
        attention_description = "按人工、草稿和投递风险汇总当前工作。"
    else:
        attention_cards = "".join(
            (
                metric_card(
                    int(account_count or 0),
                    "我的授权账号",
                    detail=f"{int(active_account_count or 0)} 个当前可用",
                ),
                metric_card(
                    inbox_summary.human_count,
                    "待人工",
                    detail=f"最老等待 {format_age(inbox_summary.oldest_human_at)}",
                ),
                metric_card(
                    int(message_count or 0),
                    "今日消息",
                    detail="仅统计我的账号",
                ),
            )
        )
        attention_description = "只汇总归属于你的账号、消息和人工工作。"
    readiness_rows = "".join(
        (
            _progress_row(True, f"已识别 {len(agent_ids)} 个 Agent 作用域"),
            _progress_row(
                bool(published_knowledge_count),
                f"{int(published_knowledge_count or 0)} 条知识已发布",
            ),
            _progress_row(
                int(active_account_count or 0) == int(account_count or 0)
                and bool(account_count),
                f"{int(active_account_count or 0)}/{int(account_count or 0)} 个渠道账号可用",
                warning=bool(account_count),
            ),
            _progress_row(True, "发送仍经过 kill switch、Final Guard 与 Outbox"),
        )
    )
    audit_rows = "".join(
        f"<tr><td>{format_datetime(audit.created_at)}</td>"
        f"<td>{escape(audit.actor)}</td><td>{escape(audit.action)}</td>"
        f"<td>{escape(audit.subject_type)}</td></tr>"
        for audit in recent_audits
    )
    recent_activity = (
        '<div class="saas-table-wrap"><table class="saas-table">'
        '<thead><tr><th>时间</th><th>Actor</th><th>动作</th><th>资源</th></tr></thead>'
        f"<tbody>{audit_rows}</tbody></table></div>"
        if audit_rows
        else empty_state("暂无最近活动", "配置、审核和人工操作将在这里形成可追溯记录。")
    )
    body = f"""{next_action}
<div class="saas-section-title"><div><h2>需要关注</h2>
<p>{escape(attention_description)}</p></div></div>
<div class="saas-grid three">{attention_cards}</div>
<div class="saas-grid two">
  <section class="saas-card"><div class="saas-card-header"><div><h2>工作区就绪度</h2>
  <p>Agent 能力仍受现有安全链路约束。</p></div>
  {secondary_action(f'{_tenant_root(tenant_id)}/agents', '查看 Agents', small=True)}</div>
  <div class="saas-card-body"><ul class="saas-progress-list">{readiness_rows}</ul></div></section>
  <section class="saas-card"><div class="saas-card-header"><div><h2>今日概览</h2>
  <p>只展示帮助判断下一步的基础数据。</p></div></div>
  <div class="saas-card-body"><div class="saas-grid two" style="margin-top:0">
  {metric_card(int(message_count or 0), '今日消息')}
  {metric_card(int(published_knowledge_count or 0), '已发布知识')}
  </div></div></section>
</div>
<div class="saas-section-title"><div><h2>最近活动</h2>
<p>配置、审核与状态变更的审计摘要。</p></div>
{secondary_action(f'{_tenant_root(tenant_id)}/audit' if principal.is_admin else f'{_tenant_root(tenant_id)}/activity', '查看全部', small=True)}</div>
{recent_activity}"""
    return _render_page(
        principal=principal,
        tenant_id=tenant_id,
        title="首页",
        description=f"{tenant_id} 工作区当前有 {inbox_summary.total} 项需要处理。",
        body=body,
        active_navigation="home",
        inbox_count=inbox_summary.total,
    )


def _home_next_action(
    principal: Principal,
    tenant_id: str,
    summary: InboxSummary,
    account_count: int,
) -> str:
    inbox_href = f"{_tenant_root(tenant_id)}/inbox"
    if summary.delivery_count:
        title = "先核实最老的投递异常"
        description = (
            f"共有 {summary.delivery_count} 条异常，最老已等待 "
            f"{format_age(summary.oldest_delivery_at)}。结果不确定的发送不会直接重试。"
        )
        href = f"{inbox_href}?queue=delivery"
        action_label = "查看投递异常"
    elif summary.human_count:
        title = "处理最老的人工工作项"
        description = (
            f"共有 {summary.human_count} 项等待人工，最老已等待 "
            f"{format_age(summary.oldest_human_at)}。"
        )
        href = f"{inbox_href}?queue=human"
        action_label = "打开并处理"
    elif summary.draft_count:
        title = "审核最老的 AI 草稿"
        description = (
            f"共有 {summary.draft_count} 条草稿待审核，最老已等待 "
            f"{format_age(summary.oldest_draft_at)}。"
        )
        href = f"{inbox_href}?queue=drafts"
        action_label = "开始审核"
    elif not account_count:
        if principal.is_admin:
            title = "完成第一个 Agent 的渠道配置"
            description = "当前没有平台账号。连接渠道后，Agent 才能开始接收消息。"
            href = f"{_tenant_root(tenant_id)}/agents/default/channels"
            action_label = "继续设置"
        else:
            title = "授权你的第一个平台账号"
            description = (
                "连接 Telegram Bot 或 Email 后，你就能在工作区查看并处理该账号的对话。"
            )
            href = f"{_tenant_root(tenant_id)}/channels"
            action_label = "授权新账号"
    else:
        title = "当前没有紧急工作"
        description = "建议检查 Agent 运行状态和最近配置变更。"
        href = f"{_tenant_root(tenant_id)}/agents"
        action_label = "查看 Agents"
    return (
        '<section class="saas-next-action"><div><div class="saas-eyebrow">下一步</div>'
        f"<h2>{escape(title)}</h2><p>{escape(description)}</p></div>"
        f"{primary_action(href, action_label)}</section>"
    )


def _progress_row(done: bool, label: str, *, warning: bool = False) -> str:
    if done:
        icon_class = "done"
        icon = "✓"
    elif warning:
        icon_class = "warning"
        icon = "!"
    else:
        icon_class = ""
        icon = "·"
    return (
        '<li class="saas-progress-item">'
        f'<span class="saas-progress-icon {icon_class}">{icon}</span>'
        f"<span>{escape(label)}</span></li>"
    )


@router.get("/app/t/{tenant_id}/agents", response_class=HTMLResponse)
async def agent_list(request: Request, tenant_id: str) -> Response:
    principal = await _require_tenant_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    async with get_session_factory()() as session:
        inbox_summary = await _load_inbox_summary(session, principal, tenant_id)
        agent_ids = await _load_agent_ids(session, principal, tenant_id)
        cards: list[str] = []
        for agent_id in agent_ids:
            accounts = (
                (
                    await session.execute(
                        select(models.PlatformAccount).where(
                            _account_scope_condition(principal, tenant_id),
                            models.PlatformAccount.brand_id == agent_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            published_count = await session.scalar(
                select(func.count()).where(
                    models.KnowledgeDocument.tenant_id == tenant_id,
                    models.KnowledgeDocument.brand_id == agent_id,
                    models.KnowledgeDocument.status == "published",
                )
            )
            prompt = await session.scalar(
                select(models.ReplyBusinessPrompt).where(
                    models.ReplyBusinessPrompt.tenant_id == tenant_id,
                    models.ReplyBusinessPrompt.brand_id == agent_id,
                )
            )
            cards.append(
                _render_agent_card(
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    accounts=accounts,
                    published_count=int(published_count or 0),
                    prompt=prompt,
                )
            )
    body = (
        '<div class="saas-filter-bar"><span class="saas-status neutral">'
        f"全部 {len(agent_ids)}</span><span class=\"saas-muted\">"
        "Agent 当前按 Tenant + Brand 作用域聚合。</span></div>"
        f'<div class="saas-grid">{"".join(cards)}</div>'
    )
    return _render_page(
        principal=principal,
        tenant_id=tenant_id,
        title="Agents",
        description="集中查看每个业务 Agent 的指令、模型、渠道、知识和运行状态。",
        body=body,
        active_navigation="agents",
        inbox_count=inbox_summary.total,
        primary_action_html=(
            primary_action("/admin/content/reply-prompt", "配置新作用域")
            if principal.is_admin
            else ""
        ),
    )


def _render_agent_card(
    *,
    tenant_id: str,
    agent_id: str,
    accounts: list[models.PlatformAccount],
    published_count: int,
    prompt: models.ReplyBusinessPrompt | None,
) -> str:
    active_accounts = [account for account in accounts if account.status == "active"]
    modes = {account.automation_default for account in accounts}
    if not accounts:
        automation_mode = "unconfigured"
        status = "unconfigured"
        next_step = "连接第一个渠道账号"
    elif len(modes) > 1:
        automation_mode = "mixed"
        status = "degraded"
        next_step = "统一或确认账号自动化模式"
    else:
        automation_mode = next(iter(modes))
        status = "healthy" if len(active_accounts) == len(accounts) else "degraded"
        next_step = (
            "检查不可用的渠道账号"
            if status == "degraded"
            else "检查最近运行活动"
        )
    prompt_text = f"业务指令 v{prompt.revision}" if prompt else "使用代码默认业务指令"
    return f"""<section class="saas-card saas-agent-card">
<div><div style="display:flex;align-items:center;gap:10px"><h2>{escape(_display_agent_name(agent_id))}</h2>
{status_badge(status)}</div>
<p class="saas-muted">作用域 <span class="saas-mono">{escape(agent_id)}</span> · {escape(prompt_text)}</p>
<div class="saas-agent-meta"><span>模式：{status_badge(automation_mode)}</span>
<span>渠道：{len(active_accounts)}/{len(accounts)} 可用</span>
<span>知识：{published_count} 条已发布</span></div>
<p><strong>下一步：</strong>{escape(next_step)}</p></div>
<div>{secondary_action(f'{_agent_root(tenant_id, agent_id)}/overview', '打开 Agent →')}</div>
</section>"""


_AGENT_SECTIONS = {
    "overview",
    "instructions",
    "model",
    "channels",
    "knowledge",
    "flow",
    "activity",
}


@router.get(
    "/app/t/{tenant_id}/agents/{agent_id}",
    response_class=HTMLResponse,
)
async def agent_detail_redirect(tenant_id: str, agent_id: str) -> Response:
    return RedirectResponse(
        f"{_agent_root(tenant_id, agent_id)}/overview",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get(
    "/app/t/{tenant_id}/agents/{agent_id}/{section}",
    response_class=HTMLResponse,
)
async def agent_detail(
    request: Request,
    tenant_id: str,
    agent_id: str,
    section: str,
) -> Response:
    principal = await _require_tenant_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    if section not in _AGENT_SECTIONS:
        raise HTTPException(status_code=404, detail="agent_section_not_found")
    async with get_session_factory()() as session:
        agent_ids = await _load_agent_ids(session, principal, tenant_id)
        if agent_id not in agent_ids:
            raise HTTPException(status_code=404, detail="agent_not_found")
        inbox_summary = await _load_inbox_summary(session, principal, tenant_id)
        accounts = (
            (
                await session.execute(
                    select(models.PlatformAccount)
                    .where(
                        _account_scope_condition(principal, tenant_id),
                        models.PlatformAccount.brand_id == agent_id,
                    )
                    .order_by(models.PlatformAccount.platform, models.PlatformAccount.name)
                )
            )
            .scalars()
            .all()
        )
        prompt_pointer = await session.scalar(
            select(models.ReplyBusinessPrompt).where(
                models.ReplyBusinessPrompt.tenant_id == tenant_id,
                models.ReplyBusinessPrompt.brand_id == agent_id,
            )
        )
        prompt_version = None
        if prompt_pointer:
            prompt_version = await session.scalar(
                select(models.ReplyBusinessPromptVersion).where(
                    models.ReplyBusinessPromptVersion.id == prompt_pointer.active_version_id,
                    models.ReplyBusinessPromptVersion.tenant_id == tenant_id,
                    models.ReplyBusinessPromptVersion.brand_id == agent_id,
                )
            )
        knowledge_counts = dict(
            (
                await session.execute(
                    select(models.KnowledgeDocument.status, func.count())
                    .where(
                        models.KnowledgeDocument.tenant_id == tenant_id,
                        models.KnowledgeDocument.brand_id == agent_id,
                    )
                    .group_by(models.KnowledgeDocument.status)
                )
            ).all()
        )
        recent_audits = (
            (
                await session.execute(
                    select(models.AuditLog)
                    .where(
                        models.AuditLog.tenant_id == tenant_id,
                        *(
                            ()
                            if principal.is_admin
                            else (models.AuditLog.actor == principal.actor,)
                        ),
                    )
                    .order_by(models.AuditLog.created_at.desc())
                    .limit(12)
                )
            )
            .scalars()
            .all()
        )

    agent_base = _agent_root(tenant_id, agent_id)
    section_tabs = tabs(
        (
            ("overview", f"{agent_base}/overview", "Overview"),
            ("instructions", f"{agent_base}/instructions", "Instructions"),
            ("model", f"{agent_base}/model", "Model"),
            ("channels", f"{agent_base}/channels", "Channels"),
            ("knowledge", f"{agent_base}/knowledge", "Knowledge"),
            ("flow", f"{agent_base}/flow", "Flow"),
            ("activity", f"{agent_base}/activity", "Activity"),
        ),
        section,
    )
    section_body = _render_agent_section(
        section=section,
        tenant_id=tenant_id,
        agent_id=agent_id,
        accounts=accounts,
        prompt_pointer=prompt_pointer,
        prompt_version=prompt_version,
        knowledge_counts=knowledge_counts,
        recent_audits=recent_audits,
        is_admin=principal.is_admin,
    )
    modes = {account.automation_default for account in accounts}
    agent_mode = next(iter(modes)) if len(modes) == 1 else ("mixed" if modes else "unconfigured")
    return _render_page(
        principal=principal,
        tenant_id=tenant_id,
        title=_display_agent_name(agent_id),
        description=(
            f"作用域 {agent_id} · 当前模式 "
            f"{_plain_status_label(agent_mode)} · {len(accounts)} 个渠道账号。"
        ),
        body=f"{section_tabs}{section_body}",
        active_navigation="agents",
        inbox_count=inbox_summary.total,
        primary_action_html=(
            primary_action(
                "/admin/prompt?tenant_id="
                f"{quote(tenant_id)}&brand_id={quote(agent_id)}",
                "运行试验",
            )
            if principal.is_admin
            else ""
        ),
        breadcrumbs=(
            ("Agents", f"{_tenant_root(tenant_id)}/agents"),
            (_display_agent_name(agent_id), None),
        ),
    )


def _plain_status_label(status: str) -> str:
    labels = {
        "BOT_ACTIVE": "自动回复",
        "BOT_DRAFT_ONLY": "仅生成草稿",
        "mixed": "混合模式",
        "unconfigured": "尚未配置",
    }
    return labels.get(status, status)


def _render_agent_section(
    *,
    section: str,
    tenant_id: str,
    agent_id: str,
    accounts: list[models.PlatformAccount],
    prompt_pointer: models.ReplyBusinessPrompt | None,
    prompt_version: models.ReplyBusinessPromptVersion | None,
    knowledge_counts: dict[str, int],
    recent_audits: list[models.AuditLog],
    is_admin: bool,
) -> str:
    if section == "overview":
        return _render_agent_overview(
            tenant_id=tenant_id,
            agent_id=agent_id,
            accounts=accounts,
            prompt_pointer=prompt_pointer,
            knowledge_counts=knowledge_counts,
            is_admin=is_admin,
        )
    if section == "instructions":
        return _render_agent_instructions(
            tenant_id=tenant_id,
            agent_id=agent_id,
            prompt_pointer=prompt_pointer,
            prompt_version=prompt_version,
            is_admin=is_admin,
        )
    if section == "model":
        return _render_agent_model()
    if section == "channels":
        return _render_agent_channels(
            accounts,
            tenant_id=tenant_id,
            is_admin=is_admin,
        )
    if section == "knowledge":
        return _render_agent_knowledge(
            tenant_id,
            agent_id,
            knowledge_counts,
            is_admin=is_admin,
        )
    if section == "flow":
        return _render_agent_flow()
    return _render_agent_activity(recent_audits)


def _render_agent_overview(
    *,
    tenant_id: str,
    agent_id: str,
    accounts: list[models.PlatformAccount],
    prompt_pointer: models.ReplyBusinessPrompt | None,
    knowledge_counts: dict[str, int],
    is_admin: bool,
) -> str:
    active_accounts = [account for account in accounts if account.status == "active"]
    if not accounts:
        next_title = "连接第一个渠道账号"
        next_description = "Agent 已有安全运行框架，但还不能接收或发送平台消息。"
        next_href = (
            "/admin/integrations/accounts"
            if is_admin
            else f"{_tenant_root(tenant_id)}/channels"
        )
        action_label = "连接渠道" if is_admin else "授权我的账号"
    elif len(active_accounts) != len(accounts):
        next_title = "修复不可用的渠道账号"
        next_description = f"{len(accounts) - len(active_accounts)} 个账号当前不可用。"
        next_href = "/admin/integrations/accounts" if is_admin else f"{_tenant_root(tenant_id)}/profile"
        action_label = "查看账号"
    elif not prompt_pointer:
        next_title = "补充业务指令"
        next_description = "当前仍使用代码默认业务说明；可保存 Tenant + Brand 版本化指令。"
        next_href = "/admin/content/reply-prompt" if is_admin else f"{_agent_root(tenant_id, agent_id)}/instructions"
        action_label = "编辑指令" if is_admin else "查看指令"
    elif not knowledge_counts.get("published", 0):
        next_title = "发布第一条批准知识"
        next_description = (
            "没有已发布知识时，事实类问题将更容易转人工或只生成草稿。"
            if is_admin
            else "当前没有可查询的已发布知识，请联系管理员补充并发布内容。"
        )
        next_href = (
            f"{_tenant_root(tenant_id)}/knowledge"
            if is_admin
            else f"{_tenant_root(tenant_id)}/knowledge-query"
        )
        action_label = "打开知识中心" if is_admin else "查询知识"
    else:
        next_title = "检查最近运行活动"
        next_description = "当前配置完整，建议查看最近决策和配置变更。"
        next_href = f"{_agent_root(tenant_id, agent_id)}/activity"
        action_label = "查看活动"
    readiness = "".join(
        (
            _progress_row(bool(prompt_pointer), "业务指令已版本化"),
            _progress_row(bool(knowledge_counts.get("published")), "已有已发布知识"),
            _progress_row(
                bool(accounts) and len(active_accounts) == len(accounts),
                f"{len(active_accounts)}/{len(accounts)} 个渠道可用",
                warning=bool(accounts),
            ),
            _progress_row(True, "Draft-only、Final Guard 与 Outbox 安全边界有效"),
        )
    )
    return f"""<div class="saas-grid two" style="margin-top:0">
<section class="saas-next-action"><div><div class="saas-eyebrow">下一步</div>
<h2>{escape(next_title)}</h2><p>{escape(next_description)}</p></div>
{primary_action(next_href, action_label)}</section>
<section class="saas-card"><div class="saas-card-header"><div><h2>运行摘要</h2>
<p>聚合当前 Agent 作用域的真实配置。</p></div></div><div class="saas-card-body">
{definition_list((('渠道账号', len(accounts)), ('已发布知识', knowledge_counts.get('published', 0)), ('业务指令', f'v{prompt_pointer.revision}' if prompt_pointer else '代码默认')))}
</div></section></div>
<div class="saas-section-title"><div><h2>就绪度</h2><p>只展示用户可以继续完成的配置步骤。</p></div></div>
<section class="saas-card"><div class="saas-card-body"><ul class="saas-progress-list">{readiness}</ul></div></section>"""


def _render_agent_instructions(
    *,
    tenant_id: str,
    agent_id: str,
    prompt_pointer: models.ReplyBusinessPrompt | None,
    prompt_version: models.ReplyBusinessPromptVersion | None,
    is_admin: bool,
) -> str:
    content = prompt_version.content if prompt_version else "当前使用代码默认业务指令。"
    metadata = definition_list(
        (
            ("Tenant", tenant_id),
            ("Brand / Agent", agent_id),
            ("活动版本", f"v{prompt_pointer.revision}" if prompt_pointer else "代码默认"),
            ("内容 Hash", prompt_pointer.content_hash[:12] if prompt_pointer else "—"),
            ("最后更新", format_datetime(prompt_pointer.updated_at) if prompt_pointer else "—"),
            ("更新人", prompt_pointer.updated_by if prompt_pointer else "system"),
        )
    )
    edit_action = (
        secondary_action(
            "/admin/content/reply-prompt",
            "进入编辑器",
            small=True,
        )
        if is_admin
        else ""
    )
    return f"""<div class="saas-grid" style="grid-template-columns:220px minmax(0,1fr) 280px;margin-top:0">
<aside class="saas-card"><div class="saas-card-header"><div><h2>版本</h2>
<p>不可变历史由现有 Prompt 域维护。</p></div></div><div class="saas-card-body">
{status_badge('active' if prompt_pointer else 'unconfigured', label=f'活动 v{prompt_pointer.revision}' if prompt_pointer else '代码默认')}
</div></aside>
<section class="saas-card"><div class="saas-card-header"><div><h2>业务指令</h2>
<p>低权限业务说明，不能覆盖系统安全契约。</p></div>
{edit_action}</div>
<div class="saas-card-body"><div class="saas-alert">{escape(content)}</div></div></section>
<aside class="saas-card"><div class="saas-card-header"><div><h2>安全与元数据</h2></div></div>
<div class="saas-card-body">{metadata}<div class="saas-alert warning" style="margin-top:16px">
系统身份、输出 Schema、知识权威和发送授权保持代码拥有，租户不能编辑。</div></div></aside>
</div>"""


def _render_agent_model() -> str:
    settings = get_settings()
    provider_label = settings.llm_provider
    model_label = settings.openai_model if settings.llm_provider == "openai" else "stub"
    grounding_model = settings.openai_grounding_model or model_label
    return f"""<div class="saas-grid two" style="margin-top:0">
<section class="saas-card"><div class="saas-card-header"><div><h2>主回复模型</h2>
<p>当前是部署级受控连接，Tenant 尚不能填写任意 Base URL。</p></div>{status_badge('active')}</div>
<div class="saas-card-body">{definition_list((('Provider', provider_label), ('Model', model_label), ('用途', '主决策与回复生成'), ('配置来源', 'Railway / Settings')))}</div></section>
<section class="saas-card"><div class="saas-card-header"><div><h2>Grounding 模型</h2>
<p>独立验证知识回复与证据是否一致。</p></div>{status_badge('active')}</div>
<div class="saas-card-body">{definition_list((('Model', grounding_model), ('用途', '语义忠实度验证'), ('Fallback', model_label), ('密钥状态', '已配置' if settings.openai_api_key.get_secret_value() else '未配置')))}</div></section>
</div>
<section class="saas-alert warning" style="margin-top:18px">Model Profile 管理仍处于规划阶段。当前页面只展示安全裁剪后的有效配置，不回显 API Key 或完整上游地址。</section>"""


def _render_agent_channels(
    accounts: list[models.PlatformAccount],
    *,
    tenant_id: str,
    is_admin: bool,
) -> str:
    account_href = (
        "/admin/integrations/accounts"
        if is_admin
        else f"{_tenant_root(tenant_id)}/profile"
    )
    account_action_label = "管理账号" if is_admin else "查看我的账号"
    rows = "".join(
        f"<tr><td><strong>{escape(account.name)}</strong><br>"
        f'<span class="saas-muted">{escape(account.platform)}</span></td>'
        f"<td>{status_badge(account.status)}</td>"
        f"<td>{status_badge(account.automation_default)}</td>"
        f"<td>v{account.config_version}</td>"
        f'<td><a href="{account_href}">{account_action_label} →</a></td></tr>'
        for account in accounts
    )
    if not rows:
        return empty_state(
            "尚未连接渠道",
            "连接渠道后，Agent 才能接收消息；新账号仍默认仅生成草稿。",
            action_html=primary_action(
                account_href,
                "连接第一个渠道" if is_admin else "授权我的账号",
            ),
        )
    return (
        '<div class="saas-table-wrap"><table class="saas-table"><thead><tr>'
        "<th>账号</th><th>连接状态</th><th>自动化模式</th><th>配置版本</th><th></th>"
        f"</tr></thead><tbody>{rows}</tbody></table></div>"
    )


def _render_agent_knowledge(
    tenant_id: str,
    agent_id: str,
    knowledge_counts: dict[str, int],
    *,
    is_admin: bool,
) -> str:
    published_count = int(knowledge_counts.get("published", 0))
    draft_count = int(knowledge_counts.get("draft", 0))
    knowledge_href = (
        f"{_tenant_root(tenant_id)}/knowledge?brand_id={quote(agent_id)}"
        if is_admin
        else f"{_tenant_root(tenant_id)}/knowledge-query"
    )
    knowledge_action_label = "打开知识中心" if is_admin else "查询知识"
    body = f"""<div class="saas-grid three" style="margin-top:0">
{metric_card(published_count, '已发布知识')}
{metric_card(draft_count, '草稿与待审核')}
{metric_card(published_count + draft_count, '作用域内总数')}
</div>
<section class="saas-card" style="margin-top:18px"><div class="saas-card-header"><div>
<h2>知识绑定</h2><p>当前 Agent 使用 Tenant + Brand + Platform 作用域检索。</p></div>
{secondary_action(knowledge_href, knowledge_action_label, small=True)}</div>
<div class="saas-card-body"><p>来源文档、检索 Chunk 和批准答案保持分层；只有已发布并通过语言与安全检查的知识进入自动回复路径。</p></div></section>"""
    return body


def _render_agent_flow() -> str:
    stages = (
        ("1", "确定性规则与 kill switch", "系统强制，Flow 不可覆盖"),
        ("2", "语言解析与知识检索", "按 Tenant、Brand、Platform 作用域"),
        ("3", "LLM 决策", "严格结构化 ReplyDecision"),
        ("4", "Final Guard 与 Grounding", "失败时转 DRAFT 或 HANDOFF"),
        ("5", "Outbox 与发送前复检", "所有外发统一经过耐久边界"),
    )
    rows = "".join(
        '<li class="saas-timeline-item"><span class="saas-timeline-marker success">'
        f"{escape(marker)}</span><div><div class=\"saas-timeline-title\">{escape(title)}</div>"
        f'<div class="saas-timeline-detail">{escape(detail)}</div></div></li>'
        for marker, title, detail in stages
    )
    return f"""<section class="saas-card"><div class="saas-card-header"><div><h2>当前 Reply Policy Flow</h2>
<p>先展示代码拥有的实际安全流程；可编辑 Flow 将在版本、评测和审计基础稳定后开放。</p></div>{status_badge('active', label='系统受控')}</div>
<div class="saas-card-body"><ol class="saas-timeline">{rows}</ol></div></section>
<section class="saas-alert warning" style="margin-top:18px">不会开放任意 HTTP、Python、SQL 或直接发送节点。未来 Flow 只能编排允许的只读和决策节点。</section>"""


def _render_agent_activity(audits: list[models.AuditLog]) -> str:
    rows = "".join(
        f"<tr><td>{format_datetime(audit.created_at)}</td>"
        f"<td>{escape(audit.actor)}</td><td>{escape(audit.action)}</td>"
        f"<td>{escape(audit.subject_type)}</td></tr>"
        for audit in audits
    )
    if not rows:
        return empty_state("暂无 Agent 活动", "Prompt、知识、渠道和审核变更将在这里显示。")
    return (
        '<div class="saas-table-wrap"><table class="saas-table"><thead><tr>'
        "<th>时间</th><th>Actor</th><th>动作</th><th>资源</th>"
        f"</tr></thead><tbody>{rows}</tbody></table></div>"
    )


@router.get("/app/t/{tenant_id}/inbox", response_class=HTMLResponse)
async def tenant_inbox(
    request: Request,
    tenant_id: str,
    queue: str = "human",
    item_id: str = "",
) -> Response:
    principal = await _require_tenant_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    if queue not in {"human", "drafts", "delivery"}:
        raise HTTPException(status_code=422, detail="invalid_inbox_queue")
    if not principal.is_admin and queue != "human":
        raise HTTPException(status_code=403, detail="inbox_queue_access_denied")
    selected_item_uuid: uuid.UUID | None = None
    if item_id:
        try:
            selected_item_uuid = uuid.UUID(item_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid_inbox_item") from exc
    async with get_session_factory()() as session:
        inbox_summary = await _load_inbox_summary(session, principal, tenant_id)
        items = await _load_inbox_items(session, principal, tenant_id, queue)
        selected_item = next(
            (item for item in items if item.item_id == selected_item_uuid),
            items[0] if items else None,
        )
        messages: list[models.Message] = []
        if selected_item:
            newest_messages = list(
                (
                    await session.execute(
                        select(models.Message)
                        .join(
                            models.Conversation,
                            models.Message.conversation_id == models.Conversation.id,
                        )
                        .where(
                            models.Message.conversation_id
                            == selected_item.conversation_id,
                            models.Conversation.tenant_id == tenant_id,
                        )
                        .order_by(models.Message.history_seq.desc())
                        .limit(60)
                    )
                )
                .scalars()
                .all()
            )
            messages = list(reversed(newest_messages))
    queue_tabs = _render_queue_tabs(
        tenant_id,
        queue,
        inbox_summary,
        include_admin_queues=principal.is_admin,
    )
    item_list = _render_inbox_item_list(tenant_id, queue, items, selected_item)
    thread = _render_conversation_thread(
        tenant_id,
        messages,
        selected_item,
    )
    action_panel = _render_inbox_action_panel(tenant_id, selected_item)
    body = f"""<section class="saas-alert" style="margin-bottom:14px">
列表不会在阅读或编辑时自动重排。完成当前项目后，再进入下一项。</section>
<div class="saas-inbox-layout">
  <section class="saas-inbox-column"><div class="saas-inbox-column-header">{queue_tabs}</div>
  <div>{item_list}</div></section>
  <section class="saas-inbox-column">{thread}</section>
  <aside class="saas-inbox-column">{action_panel}</aside>
</div>"""
    return _render_page(
        principal=principal,
        tenant_id=tenant_id,
        title="收件箱",
        description=(
            "按最老等待优先处理人工工作、草稿审核和投递异常。"
            if principal.is_admin
            else "按最老等待优先处理归属于你账号的人工工作。"
        ),
        body=body,
        active_navigation="inbox",
        inbox_count=inbox_summary.total,
    )


async def _load_inbox_items(
    session,
    principal: Principal,
    tenant_id: str,
    queue: str,
) -> list[InboxItem]:
    if queue == "human":
        rows = (
            await session.execute(
                select(
                    models.HumanWorkItem,
                    models.Conversation,
                    models.Contact,
                    models.PlatformAccount,
                )
                .join(
                    models.Conversation,
                    models.HumanWorkItem.conversation_id == models.Conversation.id,
                )
                .join(models.Contact, models.Conversation.contact_id == models.Contact.id)
                .join(
                    models.PlatformAccount,
                    models.Conversation.platform_account_id == models.PlatformAccount.id,
                )
                .where(
                    models.HumanWorkItem.tenant_id == tenant_id,
                    models.Conversation.tenant_id == tenant_id,
                    models.PlatformAccount.tenant_id == tenant_id,
                    _account_scope_condition(principal, tenant_id),
                    models.HumanWorkItem.status.in_(("WAITING", "CLAIMED")),
                )
                .order_by(models.HumanWorkItem.created_at)
                .limit(100)
            )
        ).all()
        return [
            InboxItem(
                item_id=work_item.id,
                conversation_id=conversation.id,
                queue="human",
                title=contact.display_name or "匿名联系人",
                platform=conversation.platform,
                channel_type=conversation.channel_type,
                account_name=account.name,
                status=work_item.status,
                reason=work_item.reason_code,
                created_at=work_item.created_at,
                work_item_version=work_item.version,
            )
            for work_item, conversation, contact, account in rows
        ]
    if queue == "drafts":
        rows = (
            await session.execute(
                select(
                    models.ReplyDecision,
                    models.Conversation,
                    models.Contact,
                    models.PlatformAccount,
                )
                .join(
                    models.Conversation,
                    models.ReplyDecision.conversation_id == models.Conversation.id,
                )
                .join(models.Contact, models.Conversation.contact_id == models.Contact.id)
                .join(
                    models.PlatformAccount,
                    models.Conversation.platform_account_id == models.PlatformAccount.id,
                )
                .where(
                    models.ReplyDecision.tenant_id == tenant_id,
                    models.Conversation.tenant_id == tenant_id,
                    models.PlatformAccount.tenant_id == tenant_id,
                    _account_scope_condition(principal, tenant_id),
                    models.ReplyDecision.action == "draft",
                    or_(
                        models.ReplyDecision.review_action.is_(None),
                        models.ReplyDecision.review_action == "PENDING",
                    ),
                    models.ReplyDecision.review_outbox_id.is_(None),
                    or_(
                        models.ReplyDecision.decision_generation.is_(None),
                        models.ReplyDecision.decision_generation
                        == models.Conversation.decision_generation,
                    ),
                )
                .order_by(models.ReplyDecision.created_at)
                .limit(100)
            )
        ).all()
        return [
            InboxItem(
                item_id=decision.id,
                conversation_id=conversation.id,
                queue="drafts",
                title=contact.display_name or "匿名联系人",
                platform=conversation.platform,
                channel_type=conversation.channel_type,
                account_name=account.name,
                status=decision.review_action or "PENDING",
                reason=", ".join(decision.reason_codes or []) or "等待人工审核",
                created_at=decision.created_at,
            )
            for decision, conversation, contact, account in rows
        ]
    rows = (
        await session.execute(
            select(
                models.OutboxMessage,
                models.Conversation,
                models.Contact,
                models.PlatformAccount,
            )
            .join(
                models.Conversation,
                models.OutboxMessage.conversation_id == models.Conversation.id,
            )
            .join(models.Contact, models.Conversation.contact_id == models.Contact.id)
            .join(
                models.PlatformAccount,
                models.OutboxMessage.platform_account_id == models.PlatformAccount.id,
            )
            .where(
                models.OutboxMessage.tenant_id == tenant_id,
                models.Conversation.tenant_id == tenant_id,
                models.PlatformAccount.tenant_id == tenant_id,
                _account_scope_condition(principal, tenant_id),
                models.OutboxMessage.status.in_(("FAILED", "NEEDS_REVIEW")),
            )
            .order_by(models.OutboxMessage.created_at)
            .limit(100)
        )
    ).all()
    return [
        InboxItem(
            item_id=outbox.id,
            conversation_id=conversation.id,
            queue="delivery",
            title=contact.display_name or "匿名联系人",
            platform=conversation.platform,
            channel_type=conversation.channel_type,
            account_name=account.name,
            status=outbox.status,
            reason=outbox.last_error_code or "投递结果需要核实",
            created_at=outbox.created_at,
        )
        for outbox, conversation, contact, account in rows
    ]


def _render_queue_tabs(
    tenant_id: str,
    active_queue: str,
    summary: InboxSummary,
    *,
    include_admin_queues: bool,
) -> str:
    root = f"{_tenant_root(tenant_id)}/inbox"
    queue_data = (
        (
            ("human", "人工", summary.human_count),
            ("drafts", "草稿", summary.draft_count),
            ("delivery", "投递", summary.delivery_count),
        )
        if include_admin_queues
        else (("human", "人工", summary.human_count),)
    )
    links = "".join(
        f'<a class="saas-queue-tab{" active" if key == active_queue else ""}" '
        f'href="{root}?queue={key}">{escape(label)} {count}</a>'
        for key, label, count in queue_data
    )
    return f'<nav class="saas-queue-tabs" aria-label="工作队列">{links}</nav>'


def _render_inbox_item_list(
    tenant_id: str,
    queue: str,
    items: list[InboxItem],
    selected_item: InboxItem | None,
) -> str:
    if not items:
        return (
            '<div class="saas-empty"><h2>当前队列已清空</h2>'
            "<p>完成得很好。切换到其他队列查看下一项工作。</p></div>"
        )
    root = f"{_tenant_root(tenant_id)}/inbox?queue={queue}"
    return "".join(
        f'<a class="saas-work-item{" active" if selected_item and item.item_id == selected_item.item_id else ""}" '
        f'href="{root}&amp;item_id={item.item_id}">'
        f'<div class="saas-work-item-title"><span>{escape(item.title)}</span>'
        f"<span>{format_age(item.created_at)}</span></div>"
        f"<p>{escape(item.platform)} · {escape(item.channel_type)} · "
        f"{escape(item.account_name)}</p>"
        f'<div class="saas-work-item-meta"><span>{status_badge(item.status)}</span>'
        f"<span>{escape(item.reason)}</span></div></a>"
        for item in items
    )


def _render_conversation_thread(
    tenant_id: str,
    messages: list[models.Message],
    selected_item: InboxItem | None,
) -> str:
    if selected_item is None:
        return empty_state("选择一个工作项", "左侧列表按最老等待排序。打开第一项开始处理。")
    message_rows = "".join(
        '<article class="saas-message '
        f'{"outbound" if message.direction == "outbound" else "inbound"}">'
        f'<div class="saas-message-bubble">{escape(message.text or "[非文本消息]")}</div>'
        f'<div class="saas-message-meta">{escape(message.sender_type)} · '
        f"{format_datetime(message.occurred_at or message.created_at)}</div></article>"
        for message in messages
    )
    if not message_rows:
        message_rows = '<div class="saas-empty"><p>这条会话尚无可展示的文本消息。</p></div>'
    return f"""<div class="saas-thread">
<div class="saas-inbox-column-header"><strong>{escape(selected_item.title)}</strong>
<div class="saas-muted">{escape(selected_item.platform)} · {escape(selected_item.channel_type)}</div></div>
<div class="saas-thread-messages">{message_rows}</div>
<div class="saas-composer"><textarea disabled placeholder="选择右侧操作进入受保护的处理流程"></textarea>
<div style="display:flex;justify-content:space-between;align-items:center;margin-top:8px">
<span class="saas-muted">人工回复仍沿用现有 CSRF、目标消息和 Outbox 保护。</span>
{secondary_action(f'{_tenant_root(tenant_id)}/conversations/{selected_item.conversation_id}', '打开完整处理页', small=True)}</div></div>
</div>"""


def _render_inbox_action_panel(
    tenant_id: str,
    selected_item: InboxItem | None,
) -> str:
    if selected_item is None:
        return '<div class="saas-action-panel"><p class="saas-muted">尚未选择工作项。</p></div>'
    if selected_item.queue == "human":
        action_label = "认领并处理"
        warning = "人工工作项使用版本保护；若已被他人认领，系统不会覆盖对方状态。"
    elif selected_item.queue == "drafts":
        action_label = "审核草稿"
        warning = "批准前会再次检查 decision generation、Prompt 和发送资格。"
    elif selected_item.status == "NEEDS_REVIEW":
        action_label = "核实平台结果"
        warning = "平台可能已经接收消息。核实前不要直接重试，以免重复发送。"
    else:
        action_label = "检查并重试"
        warning = "只有确定失败的投递才允许进入自动重试路径。"
    return f"""<div class="saas-action-panel"><h3>当前操作</h3>
{status_badge(selected_item.status)}
<p><strong>{escape(selected_item.reason)}</strong></p>
{definition_list((('账号', selected_item.account_name), ('平台', selected_item.platform), ('等待', format_age(selected_item.created_at)), ('Item ID', str(selected_item.item_id))))}
<div class="saas-alert warning" style="margin:16px 0">{escape(warning)}</div>
{primary_action(f'{_tenant_root(tenant_id)}/conversations/{selected_item.conversation_id}', action_label)}
<p class="saas-muted">完成后返回此页，继续下一项。</p></div>"""


@router.get("/app/t/{tenant_id}/conversations", response_class=HTMLResponse)
async def tenant_conversations(request: Request, tenant_id: str) -> Response:
    principal = await _require_tenant_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    latest_message_at = (
        select(func.max(models.Message.created_at))
        .where(models.Message.conversation_id == models.Conversation.id)
        .correlate(models.Conversation)
        .scalar_subquery()
    )
    async with get_session_factory()() as session:
        inbox_summary = await _load_inbox_summary(session, principal, tenant_id)
        rows = (
            await session.execute(
                select(
                    models.Conversation,
                    models.Contact,
                    models.PlatformAccount,
                    latest_message_at.label("latest_message_at"),
                )
                .join(models.Contact, models.Conversation.contact_id == models.Contact.id)
                .join(
                    models.PlatformAccount,
                    models.Conversation.platform_account_id == models.PlatformAccount.id,
                )
                .where(
                    models.Conversation.tenant_id == tenant_id,
                    _account_scope_condition(principal, tenant_id),
                )
                .order_by(latest_message_at.desc().nullslast())
                .limit(100)
            )
        ).all()
    table_rows = "".join(
        f"<tr><td><a href='{_tenant_root(tenant_id)}/conversations/{conversation.id}'>"
        f"<strong>{escape(contact.display_name or '匿名联系人')}</strong></a></td>"
        f"<td>{escape(account.name)}</td><td>{escape(conversation.platform)}</td>"
        f"<td>{escape(conversation.channel_type)}</td>"
        f"<td>{format_datetime(latest_at)}</td>"
        f"<td><a href='{_tenant_root(tenant_id)}/conversations/{conversation.id}'>打开 →</a></td></tr>"
        for conversation, contact, account, latest_at in rows
    )
    body = (
        '<div class="saas-table-wrap"><table class="saas-table"><thead><tr>'
        '<th>联系人</th><th>授权账号</th><th>平台</th><th>渠道</th><th>最近消息</th><th></th>'
        f"</tr></thead><tbody>{table_rows}</tbody></table></div>"
        if table_rows
        else empty_state("暂无对话", "连接自己的授权账号后，新对话会显示在这里。")
    )
    return _render_page(
        principal=principal,
        tenant_id=tenant_id,
        title="对话",
        description="仅显示你有权访问的平台账号产生的客户对话。",
        body=body,
        active_navigation="conversations",
        inbox_count=inbox_summary.total,
    )


@router.get(
    "/app/t/{tenant_id}/conversations/{conversation_id}",
    response_class=HTMLResponse,
)
async def tenant_conversation_detail(
    request: Request,
    tenant_id: str,
    conversation_id: uuid.UUID,
) -> Response:
    principal = await _require_tenant_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    async with get_session_factory()() as session:
        inbox_summary = await _load_inbox_summary(session, principal, tenant_id)
        row = (
            await session.execute(
                select(models.Conversation, models.Contact, models.PlatformAccount)
                .join(models.Contact, models.Conversation.contact_id == models.Contact.id)
                .join(
                    models.PlatformAccount,
                    models.Conversation.platform_account_id == models.PlatformAccount.id,
                )
                .where(
                    models.Conversation.id == conversation_id,
                    models.Conversation.tenant_id == tenant_id,
                    _account_scope_condition(principal, tenant_id),
                )
            )
        ).one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="conversation_not_found")
        conversation, contact, account = row
        newest_messages = list(
            (
                await session.execute(
                    select(models.Message)
                    .where(models.Message.conversation_id == conversation_id)
                    .order_by(models.Message.history_seq.desc())
                    .limit(100)
                )
            )
            .scalars()
            .all()
        )
        work_item = await session.scalar(
            select(models.HumanWorkItem)
            .where(
                models.HumanWorkItem.conversation_id == conversation_id,
                models.HumanWorkItem.status.in_(("WAITING", "CLAIMED")),
            )
            .order_by(models.HumanWorkItem.created_at.desc())
            .limit(1)
        )
    csrf = _csrf(request)
    reply_target = next(
        (
            message
            for message in newest_messages
            if message.direction == "inbound"
        ),
        None,
    )
    message_rows = "".join(
        '<article class="saas-card saas-card-body">'
        f"<div class='saas-muted'>{escape(message.sender_type)} · "
        f"{format_datetime(message.created_at, include_year=True)}</div>"
        f"<p>{escape(message.text or '（非文本消息）')}</p></article>"
        for message in reversed(newest_messages)
    )
    work_actions = ""
    if work_item is not None and work_item.status == "WAITING":
        work_actions = f"""<form class="saas-form" method="post" action="{_tenant_root(tenant_id)}/work-items/{work_item.id}/claim">
<input type="hidden" name="csrf_token" value="{csrf}"><input type="hidden" name="expected_version" value="{work_item.version}">
<button class="saas-button primary" type="submit">认领此对话</button></form>"""
    elif work_item is not None and work_item.assigned_actor == principal.actor:
        work_actions = f"""<form class="saas-form" method="post" action="{_tenant_root(tenant_id)}/work-items/{work_item.id}/resolve">
<input type="hidden" name="csrf_token" value="{csrf}"><input type="hidden" name="expected_version" value="{work_item.version}">
<button class="saas-button" type="submit">标记已解决</button></form>"""
    reply_form = ""
    if reply_target is not None:
        work_fields = ""
        if work_item is not None:
            work_fields = (
                f'<input type="hidden" name="work_item_id" value="{work_item.id}">'
                f'<input type="hidden" name="expected_version" value="{work_item.version}">'
            )
        reply_form = f"""<section class="saas-card"><div class="saas-card-header"><div><h2>人工回复</h2>
<p>提交后仍进入统一 Outbox 和发送前安全复检。</p></div></div><div class="saas-card-body">
<form class="saas-form" method="post" action="{_tenant_root(tenant_id)}/conversations/{conversation_id}/reply">
<input type="hidden" name="csrf_token" value="{csrf}"><input type="hidden" name="reply_to_message_id" value="{reply_target.id}">
<input type="hidden" name="idempotency_key" value="{uuid.uuid4()}">{work_fields}
<label for="manual-reply">回复内容</label><textarea id="manual-reply" name="text" rows="5" maxlength="10000" required></textarea>
<button class="saas-button primary" type="submit">发送人工回复</button></form></div></section>"""
    body = (
        '<section class="saas-card"><div class="saas-card-body">'
        f"{definition_list((('联系人', contact.display_name or '匿名联系人'), ('账号', account.name), ('平台', conversation.platform), ('渠道', conversation.channel_type), ('Conversation ID', conversation.id)))}"
        f"{work_actions}</div></section>"
        f'<div class="saas-stack">{message_rows}</div>{reply_form}'
    )
    response = _render_page(
        principal=principal,
        tenant_id=tenant_id,
        title=contact.display_name or "对话详情",
        description="查看该授权账号范围内的完整对话记录。",
        body=body,
        active_navigation="conversations",
        inbox_count=inbox_summary.total,
        breadcrumbs=(("对话", f"{_tenant_root(tenant_id)}/conversations"), ("详情", None)),
    )
    if not request.cookies.get("reply_admin_csrf"):
        response.set_cookie(
            "reply_admin_csrf",
            csrf,
            httponly=False,
            samesite="lax",
            secure=_secure_cookie(request),
        )
    return response


@router.post("/app/t/{tenant_id}/work-items/{work_item_id}/claim")
async def claim_tenant_work_item(
    request: Request,
    tenant_id: str,
    work_item_id: uuid.UUID,
) -> Response:
    principal = await _require_tenant_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    try:
        expected_version = int(form.get("expected_version", ""))
        await claim_human_work_item(
            work_item_id=work_item_id,
            allowed_tenants=principal.allowed_tenants,
            actor=principal.actor,
            user_id=principal.user_id,
            expected_version=expected_version,
            owner_user_id=None if principal.is_admin else principal.user_id,
        )
    except (TypeError, ValueError, HumanWorkflowError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse(
        f"{_tenant_root(tenant_id)}/inbox?queue=human",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/app/t/{tenant_id}/work-items/{work_item_id}/resolve")
async def resolve_tenant_work_item(
    request: Request,
    tenant_id: str,
    work_item_id: uuid.UUID,
) -> Response:
    principal = await _require_tenant_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    try:
        expected_version = int(form.get("expected_version", ""))
        await resolve_human_work_item(
            work_item_id=work_item_id,
            allowed_tenants=principal.allowed_tenants,
            actor=principal.actor,
            expected_version=expected_version,
            allow_override=principal.is_admin,
            owner_user_id=None if principal.is_admin else principal.user_id,
        )
    except (TypeError, ValueError, HumanWorkflowError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse(
        f"{_tenant_root(tenant_id)}/inbox?queue=human",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/app/t/{tenant_id}/conversations/{conversation_id}/reply")
async def reply_to_tenant_conversation(
    request: Request,
    tenant_id: str,
    conversation_id: uuid.UUID,
) -> Response:
    principal = await _require_tenant_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    text = (form.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="reply_text_required")
    try:
        work_item_id = (
            uuid.UUID(form["work_item_id"])
            if form.get("work_item_id")
            else None
        )
        expected_version = (
            int(form["expected_version"])
            if form.get("expected_version")
            else None
        )
        await send_human_reply(
            conversation_id=conversation_id,
            reply_to_message_id=uuid.UUID(form.get("reply_to_message_id", "")),
            text=text,
            idempotency_key=str(uuid.UUID(form.get("idempotency_key", ""))),
            allowed_tenants=principal.allowed_tenants,
            actor=principal.actor,
            user_id=principal.user_id,
            allow_override=principal.is_admin,
            work_item_id=work_item_id,
            expected_version=expected_version,
            owner_user_id=None if principal.is_admin else principal.user_id,
        )
    except (TypeError, ValueError, HumanWorkflowError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse(
        f"{_tenant_root(tenant_id)}/conversations/{conversation_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/app/t/{tenant_id}/knowledge-query", response_class=HTMLResponse)
@router.post("/app/t/{tenant_id}/knowledge-query", response_class=HTMLResponse)
async def tenant_knowledge_query(request: Request, tenant_id: str) -> Response:
    principal = await _require_tenant_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    query = (request.query_params.get("q") or "").strip()
    if request.method == "POST":
        form = await _form(request)
        _require_csrf(request, form)
        query = (form.get("q") or "").strip()
    if len(query) > 500:
        raise HTTPException(status_code=422, detail="knowledge_query_too_long")
    async with get_session_factory()() as session:
        inbox_summary = await _load_inbox_summary(session, principal, tenant_id)
        documents: list[models.KnowledgeDocument] = []
        if query:
            search_pattern = f"%{query}%"
            documents = list(
                (
                    await session.execute(
                        select(models.KnowledgeDocument)
                        .where(
                            models.KnowledgeDocument.tenant_id == tenant_id,
                            models.KnowledgeDocument.status == "published",
                            or_(
                                models.KnowledgeDocument.question.ilike(search_pattern),
                                models.KnowledgeDocument.reply.ilike(search_pattern),
                            ),
                        )
                        .order_by(models.KnowledgeDocument.updated_at.desc())
                        .limit(20)
                    )
                )
                .scalars()
                .all()
            )
    csrf = _csrf(request)
    results = "".join(
        '<article class="saas-card"><div class="saas-card-header"><div>'
        f"<h2>{escape(document.question)}</h2>"
        f"<p>{escape(document.brand_id)} · {escape(document.platform)}</p></div>"
        f"{status_badge(document.status)}</div>"
        f'<div class="saas-card-body"><p>{escape(document.reply)}</p></div></article>'
        for document in documents
    )
    if query and not results:
        results = empty_state("没有找到已发布知识", "换一个更具体的关键词再试。")
    body = f"""<section class="saas-card"><div class="saas-card-body">
<form class="saas-form" method="post"><input type="hidden" name="csrf_token" value="{csrf}">
<label for="knowledge-query">输入客户问题或关键词</label>
<div class="saas-form-row"><input id="knowledge-query" name="q" value="{escape(query)}" maxlength="500" required>
<button class="saas-button primary" type="submit">查询已发布知识</button></div>
<p class="saas-muted">只查询已发布内容，不会生成回复或触发外发。</p></form></div></section>
<div class="saas-stack">{results}</div>"""
    response = _render_page(
        principal=principal,
        tenant_id=tenant_id,
        title="知识查询",
        description="搜索管理员已经审核并发布的标准答案。",
        body=body,
        active_navigation="knowledge-query",
        inbox_count=inbox_summary.total,
    )
    if not request.cookies.get("reply_admin_csrf"):
        response.set_cookie(
            "reply_admin_csrf",
            csrf,
            httponly=False,
            samesite="lax",
            secure=_secure_cookie(request),
        )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/app/t/{tenant_id}/activity", response_class=HTMLResponse)
async def tenant_my_activity(request: Request, tenant_id: str) -> Response:
    principal = await _require_tenant_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    async with get_session_factory()() as session:
        inbox_summary = await _load_inbox_summary(session, principal, tenant_id)
        audits = list(
            (
                await session.execute(
                    select(models.AuditLog)
                    .where(
                        models.AuditLog.tenant_id == tenant_id,
                        models.AuditLog.actor == principal.actor,
                    )
                    .order_by(models.AuditLog.created_at.desc())
                    .limit(200)
                )
            )
            .scalars()
            .all()
        )
    rows = "".join(
        f"<tr><td>{format_datetime(audit.created_at, include_year=True)}</td>"
        f"<td>{escape(audit.action)}</td><td>{escape(audit.subject_type)}</td>"
        f"<td><code>{escape(audit.subject_id)}</code></td></tr>"
        for audit in audits
    )
    body = (
        '<div class="saas-table-wrap"><table class="saas-table"><thead><tr>'
        f"<th>时间</th><th>动作</th><th>资源</th><th>ID</th></tr></thead><tbody>{rows}</tbody></table></div>"
        if rows
        else empty_state("暂无个人活动", "你处理会话、审核草稿或绑定账号后会显示在这里。")
    )
    return _render_page(
        principal=principal,
        tenant_id=tenant_id,
        title="我的活动",
        description="仅显示由当前账号执行的操作，不暴露其他用户或系统内部审计。",
        body=body,
        active_navigation="activity",
        inbox_count=inbox_summary.total,
    )


_CHANNEL_AVATAR_HOST_SUFFIXES = (
    ".fbcdn.net",
    ".cdninstagram.com",
)
_CHANNEL_AVATAR_HOSTS = {
    "abs.twimg.com",
    "pbs.twimg.com",
    "platform-lookaside.fbsbx.com",
}


def _safe_channel_avatar_url(value: object) -> str | None:
    candidate = str(value or "").strip()
    if not candidate or len(candidate) > 2048:
        return None
    parsed = urlsplit(candidate)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return None
    if hostname in _CHANNEL_AVATAR_HOSTS:
        return candidate
    if any(hostname.endswith(suffix) for suffix in _CHANNEL_AVATAR_HOST_SUFFIXES):
        return candidate
    return None


def _channel_icon_path(platform: str) -> str:
    known_platform = platform if platform in {
        "email",
        "facebook",
        "feishu",
        "instagram",
        "telegram",
        "whatsapp",
        "x",
    } else "email"
    return f"/static/channel-icons/{known_platform}.svg"


def _channel_avatar(account: models.PlatformAccount) -> str:
    avatar_url = _safe_channel_avatar_url(account.avatar_url)
    if avatar_url:
        return (
            '<img class="saas-account-avatar" '
            f'src="{escape(avatar_url)}" alt="" loading="lazy" '
            'referrerpolicy="no-referrer">'
        )
    initial = (account.name or account.platform or "?").strip()[:1].upper()
    return (
        '<span class="saas-account-avatar fallback" aria-hidden="true">'
        f"{escape(initial)}</span>"
    )


def _channel_account_health(account: models.PlatformAccount) -> tuple[str, str]:
    if account.status != "active":
        return "danger", "已禁用"
    config = dict(account.config or {})
    health_values = {
        str(config.get("meta_health_status") or ""),
        str(config.get("email_health_status") or ""),
        str(config.get("feishu_health_status") or ""),
    }
    if "ERROR" in health_values:
        return "warning", "需要重新授权"
    if "PROVISIONING" in health_values:
        return "info", "正在配置"
    return "success", "已连接"


def _channel_job_error_message(job: models.ProvisioningJob) -> str:
    if job.status == "FAILED":
        return "平台暂时不可用，系统会按安全退避策略自动重试。"
    error_code = str(job.last_error_code or "")
    if error_code == "ACCOUNT_OWNER_CONFLICT":
        return "该平台账号已被其他归属范围绑定，不能重复认领。"
    if error_code.startswith(("EMAIL_", "imap_", "smtp_")):
        return "邮箱验证未完成，请检查地址、App Password 和服务器设置后重新提交。"
    if error_code.startswith("X_"):
        return "X 授权或权限验证未完成，请检查 App 权限后重新授权。"
    if error_code.startswith(("META_", "PLATFORM_HTTP_")):
        return "平台授权未完成，请确认应用权限和账号类型后重新授权。"
    return "账号验证未完成，请重新授权；如问题持续存在，请联系管理员。"


def _channel_job_action(job: models.ProvisioningJob) -> str:
    if job.status not in {"NEEDS_ACTION", "FAILED"}:
        return ""
    if job.status == "FAILED":
        return '<span class="saas-muted">等待自动重试</span>'
    if job.platform in {"telegram", "email"}:
        return (
            '<button class="saas-button small" type="button" '
            f'data-open-channel-dialog="{escape(job.platform)}-dialog">重新填写凭证</button>'
        )
    return '<a class="saas-button small" href="#add-channels">重新授权</a>'


def _render_connected_channel(account: models.PlatformAccount) -> str:
    health_tone, health_label = _channel_account_health(account)
    username = (
        f"@{account.provider_username.lstrip('@')}"
        if account.provider_username
        and account.platform not in {"email", "telegram"}
        else account.provider_username
    )
    profile_line = username or account.external_account_id or "平台账号"
    connected_at = account.profile_updated_at or account.created_at
    return f"""<article class="saas-connected-channel">
<div class="saas-connected-identity">{_channel_avatar(account)}<div>
<h3>{escape(account.name)}</h3><p>{escape(profile_line)}</p></div></div>
<div class="saas-connected-meta"><span class="saas-platform-label">
<img src="{_channel_icon_path(account.platform)}" alt="">{escape(account.platform.title())}</span>
<span class="saas-status {health_tone}">{escape(health_label)}</span>
<span class="saas-muted">最近连接 {format_datetime(connected_at, include_year=True)}</span></div>
</article>"""


def _channel_oauth_form(
    *,
    action: str,
    csrf: str,
    tenant_id: str,
    label: str,
    platform: str | None = None,
    available: bool,
) -> str:
    disabled = "" if available else " disabled aria-disabled=\"true\""
    platform_input = (
        f'<input type="hidden" name="platform" value="{escape(platform)}">'
        if platform
        else ""
    )
    return f"""<form method="post" action="{escape(action)}" data-channel-oauth-form>
<input type="hidden" name="csrf_token" value="{escape(csrf)}">
<input type="hidden" name="tenant_id" value="{escape(tenant_id)}">
<input type="hidden" name="brand_id" value="default">{platform_input}
<button class="saas-button primary small" type="submit"{disabled}>{escape(label)}</button>
</form>"""


def _channel_provider_card(
    *,
    platform: str,
    title: str,
    description: str,
    actions: str,
    available: bool,
    availability_label: str | None = None,
) -> str:
    state_label = availability_label or (
        "可连接" if available else "联系管理员配置应用"
    )
    state_class = "ready" if available else "managed"
    return f"""<article class="saas-provider-card{' disabled' if not available else ''}">
<div class="saas-provider-heading"><span class="saas-provider-icon">
<img src="{_channel_icon_path(platform)}" alt=""></span>
<span class="saas-provider-state {state_class}">{escape(state_label)}</span></div>
<div><h3>{escape(title)}</h3><p>{escape(description)}</p></div>
<div class="saas-provider-actions">{actions}</div></article>"""


@router.get("/app/t/{tenant_id}/channels", response_class=HTMLResponse)
async def tenant_channels(
    request: Request,
    tenant_id: str,
) -> Response:
    principal = await _require_tenant_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    if principal.is_admin:
        return RedirectResponse(
            "/admin/integrations/accounts",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    async with get_session_factory()() as session:
        inbox_summary = await _load_inbox_summary(session, principal, tenant_id)
        accounts = list(
            (
                await session.execute(
                    select(models.PlatformAccount)
                    .where(_account_scope_condition(principal, tenant_id))
                    .order_by(models.PlatformAccount.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        jobs = list(
            (
                await session.execute(
                    select(models.ProvisioningJob)
                    .where(
                        models.ProvisioningJob.tenant_id == tenant_id,
                        models.ProvisioningJob.owner_user_id == principal.user_id,
                    )
                    .order_by(models.ProvisioningJob.created_at.desc())
                    .limit(12)
                )
            )
            .scalars()
            .all()
        )
    settings = get_settings()
    facebook_app = await facebook_app_credentials(tenant_id)
    instagram_app = await instagram_app_credentials(tenant_id)
    x_available = settings.x_integration_enabled and x_app_credentials() is not None
    facebook_available = settings.facebook_messenger_enabled and facebook_app is not None
    instagram_direct_available = (
        settings.instagram_messaging_enabled and instagram_app is not None
    )
    instagram_meta_available = (
        settings.instagram_messaging_enabled and facebook_app is not None
    )
    csrf = _csrf(request)
    connected_channels = "".join(_render_connected_channel(account) for account in accounts)
    if not connected_channels:
        connected_channels = empty_state(
            "还没有已连接账号",
            "从下方选择一个渠道。连接成功后，账号会以名称、头像和健康状态显示在这里。",
        )
    job_cards = "".join(
        f"""<article class="saas-channel-job" data-channel-job
data-job-url="{_tenant_root(tenant_id)}/channels/jobs/{job.id}"
data-job-status="{escape(job.status)}">
<div><img src="{_channel_icon_path(job.platform)}" alt=""><strong>{escape(job.platform.title())}</strong>
<span>{format_datetime(job.created_at, include_year=True)}</span></div>
<div>{status_badge(job.status)}<span data-job-step>{escape(job.current_step)}</span>{_channel_job_action(job)}</div>
{f'<p class="saas-job-error">{escape(_channel_job_error_message(job))}</p>' if job.last_error_code else ''}
</article>"""
        for job in jobs
    ) or '<p class="saas-muted">暂无授权任务。</p>'
    oauth_status = request.query_params.get("status", "")
    job_id = request.query_params.get("job_id", "")
    error_code = request.query_params.get("code", "")
    banner = ""
    if oauth_status in {"processing", "connected"}:
        banner = (
            '<div class="saas-alert success" role="status">授权已提交，正在安全验证账号。'
            f"{f' 任务 {escape(job_id)}' if job_id else ''}</div>"
        )
    elif oauth_status == "error":
        banner = (
            '<div class="saas-alert danger" role="alert">授权未完成。'
            f"错误代码：{escape(error_code or 'oauth_failed')}。请检查应用配置后重试。</div>"
        )
    x_actions = _channel_oauth_form(
        action=f"{_tenant_root(tenant_id)}/channels/oauth/x/start",
        csrf=csrf,
        tenant_id=tenant_id,
        label="使用 X 授权",
        available=x_available,
    )
    facebook_actions = _channel_oauth_form(
        action=f"{_tenant_root(tenant_id)}/channels/oauth/meta/start",
        csrf=csrf,
        tenant_id=tenant_id,
        label="连接 Facebook Page",
        platform="facebook",
        available=facebook_available,
    )
    instagram_actions = (
        _channel_oauth_form(
            action=f"{_tenant_root(tenant_id)}/channels/oauth/instagram/start",
            csrf=csrf,
            tenant_id=tenant_id,
            label="Instagram Login",
            available=instagram_direct_available,
        )
        + _channel_oauth_form(
            action=f"{_tenant_root(tenant_id)}/channels/oauth/meta/start",
            csrf=csrf,
            tenant_id=tenant_id,
            label="通过 Facebook 连接",
            platform="instagram",
            available=instagram_meta_available,
        )
    )
    email_button_attributes = (
        "" if settings.email_enabled else ' disabled aria-disabled="true"'
    )
    provider_cards = "".join(
        (
            _channel_provider_card(
                platform="x",
                title="X",
                description="授权个人或品牌 X 账号，用于私信与已启用的互动能力。",
                actions=x_actions,
                available=x_available,
            ),
            _channel_provider_card(
                platform="facebook",
                title="Facebook",
                description="授权后选择一个或多个可管理的 Facebook Page。",
                actions=facebook_actions,
                available=facebook_available,
            ),
            _channel_provider_card(
                platform="instagram",
                title="Instagram",
                description="支持 Instagram Login，或选择 Facebook 关联的专业账号。",
                actions=instagram_actions,
                available=instagram_direct_available or instagram_meta_available,
            ),
            _channel_provider_card(
                platform="telegram",
                title="Telegram Bot",
                description="使用 BotFather 生成的 Bot Token 连接。",
                actions=(
                    '<button class="saas-button primary small" type="button" '
                    'data-open-channel-dialog="telegram-dialog">填写 Bot Token</button>'
                ),
                available=True,
            ),
            _channel_provider_card(
                platform="email",
                title="Email",
                description="使用 App Password 验证 IMAP 收件与 SMTP 回复。",
                actions=(
                    '<button class="saas-button primary small" type="button" '
                    'data-open-channel-dialog="email-dialog"'
                    f"{email_button_attributes}>"
                    "填写邮箱凭证</button>"
                ),
                available=settings.email_enabled,
            ),
            _channel_provider_card(
                platform="whatsapp",
                title="WhatsApp",
                description="首期由管理员配置 Meta App、WABA 与手机号。",
                actions="",
                available=False,
                availability_label="由管理员配置",
            ),
            _channel_provider_card(
                platform="feishu",
                title="Feishu",
                description="首期由管理员配置自建应用与事件回调。",
                actions="",
                available=False,
                availability_label="由管理员配置",
            ),
        )
    )
    dialogs = f"""<dialog class="saas-channel-dialog" id="telegram-dialog" aria-labelledby="telegram-dialog-title">
<form class="saas-form" method="post" action="{_tenant_root(tenant_id)}/channels/accounts/telegram" data-channel-credential-form>
<div class="saas-dialog-header"><div><div class="saas-eyebrow">Telegram</div><h2 id="telegram-dialog-title">连接 Telegram Bot</h2></div>
<button class="saas-dialog-close" type="button" data-close-channel-dialog aria-label="关闭">×</button></div>
<div class="saas-alert">Token 仅发送到服务端并加密暂存，不会在页面或任务结果中回显。</div>
<input type="hidden" name="csrf_token" value="{csrf}"><input type="hidden" name="tenant_id" value="{escape(tenant_id)}">
<input type="hidden" name="brand_id" value="default"><input type="hidden" name="automation_default" value="BOT_DRAFT_ONLY">
<label for="telegram-name">账号显示名称 <span class="saas-optional">选填</span></label><input id="telegram-name" name="name" placeholder="例如：售后 Telegram">
<label for="telegram-token">Bot Token</label><input id="telegram-token" name="token" type="password" autocomplete="new-password" required>
<div class="saas-dialog-actions"><button class="saas-button" type="button" data-close-channel-dialog>取消</button>
<button class="saas-button primary" type="submit">验证并连接</button></div></form></dialog>
<dialog class="saas-channel-dialog wide" id="email-dialog" aria-labelledby="email-dialog-title">
<form class="saas-form" method="post" action="{_tenant_root(tenant_id)}/channels/accounts/email" data-channel-credential-form>
<div class="saas-dialog-header"><div><div class="saas-eyebrow">Email</div><h2 id="email-dialog-title">连接邮箱账号</h2></div>
<button class="saas-dialog-close" type="button" data-close-channel-dialog aria-label="关闭">×</button></div>
<div class="saas-alert">推荐使用邮箱提供商生成的 App Password。系统以只读 IMAP 收件。</div>
<input type="hidden" name="csrf_token" value="{csrf}"><input type="hidden" name="tenant_id" value="{escape(tenant_id)}">
<input type="hidden" name="brand_id" value="default"><input type="hidden" name="automation_default" value="BOT_DRAFT_ONLY">
<div class="saas-form-grid"><div><label for="email-address">邮箱地址</label><input id="email-address" name="email_address" type="email" autocomplete="email" required></div>
<div><label for="email-username">登录用户名</label><input id="email-username" name="username" autocomplete="username" required></div></div>
<label for="email-password">密码或 App Password</label><input id="email-password" name="password" type="password" autocomplete="new-password" required>
<div class="saas-form-grid"><div><label for="imap-host">IMAP Host</label><input id="imap-host" name="imap_host" required></div>
<div><label for="smtp-host">SMTP Host</label><input id="smtp-host" name="smtp_host" required></div></div>
<input type="hidden" name="imap_port" value="993"><input type="hidden" name="smtp_port" value="465">
<input type="hidden" name="smtp_security" value="ssl"><input type="hidden" name="mailbox" value="INBOX">
<input type="hidden" name="internal_domain_policy" value="ignore">
<div class="saas-dialog-actions"><button class="saas-button" type="button" data-close-channel-dialog>取消</button>
<button class="saas-button primary" type="submit">验证并连接</button></div></form></dialog>"""
    body = f"""{banner}<section><div class="saas-section-title"><div><h2>已连接账号</h2>
<p>这里只显示属于你的账号。管理员可在系统后台查看 Tenant 全部账号。</p></div></div>
<div class="saas-connected-grid">{connected_channels}</div></section>
<section id="add-channels"><div class="saas-section-title"><div><h2>添加渠道</h2>
<p>OAuth 渠道可直接授权；Telegram 与 Email 会打开安全凭证表单。</p></div></div>
<div class="saas-provider-grid">{provider_cards}</div></section>
<section><div class="saas-section-title"><div><h2>授权进度</h2>
<p>页面会刷新仍在处理中的任务，不会展示 Token、密码或其他用户信息。</p></div></div>
<div class="saas-channel-jobs">{job_cards}</div></section>{dialogs}
<script src="/static/channels.js" defer></script>"""
    response = _render_page(
        principal=principal,
        tenant_id=tenant_id,
        title="Channels",
        description="连接并管理属于你的消息渠道。所有新账号默认仅生成草稿。",
        body=body,
        active_navigation="channels",
        inbox_count=inbox_summary.total,
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    if not request.cookies.get("reply_admin_csrf"):
        response.set_cookie(
            "reply_admin_csrf",
            csrf,
            httponly=False,
            samesite="lax",
            secure=_secure_cookie(request),
        )
    return response


@router.get("/app/t/{tenant_id}/profile", response_class=HTMLResponse)
async def tenant_profile(request: Request, tenant_id: str) -> Response:
    principal = await _require_tenant_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    async with get_session_factory()() as session:
        inbox_summary = await _load_inbox_summary(session, principal, tenant_id)
    body = f"""<div class="saas-grid two">
<section class="saas-card"><div class="saas-card-header"><div><h2>账号信息</h2></div></div><div class="saas-card-body">
{definition_list((('用户名', principal.username), ('角色', principal.role), ('Tenant', tenant_id), ('User ID', principal.user_id or 'System')))}
<p><a class="saas-button primary" href="/auth/change-password">修改密码</a></p></div></section>
<section class="saas-card"><div class="saas-card-header"><div><h2>权限说明</h2></div></div><div class="saas-card-body">
<p>普通用户只能查看和处理自己授权账号产生的对话、收件箱和活动。平台账号请在独立 Channels 页面管理。</p>
{secondary_action(f'{_tenant_root(tenant_id)}/channels', '打开 Channels')}</div></section></div>"""
    return _render_page(
        principal=principal,
        tenant_id=tenant_id,
        title="个人中心",
        description="管理个人资料、登录密码和权限说明。",
        body=body,
        active_navigation="profile",
        inbox_count=inbox_summary.total,
    )


@router.post("/app/t/{tenant_id}/channels/accounts/{platform}")
async def connect_personal_account(
    request: Request,
    tenant_id: str,
    platform: str,
) -> Response:
    principal = await _require_tenant_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    if principal.is_admin:
        raise HTTPException(status_code=403, detail="use_admin_account_management")
    if platform not in {"telegram", "email"}:
        raise HTTPException(status_code=422, detail="unsupported_personal_account_platform")
    form = await _form(request)
    _require_csrf(request, form)
    form["tenant_id"] = tenant_id
    return await _submit_form(
        request,
        platform,
        form=form,
        require_admin=False,
        redirect_path=f"{_tenant_root(tenant_id)}/channels?status=processing",
    )


@router.post("/app/t/{tenant_id}/profile/accounts/{platform}")
async def connect_personal_account_compatibility(
    request: Request,
    tenant_id: str,
    platform: str,
) -> Response:
    return await connect_personal_account(request, tenant_id, platform)


@router.get("/app/t/{tenant_id}/channels/jobs/{job_id}")
async def channel_job_status(
    request: Request,
    response: Response,
    tenant_id: str,
    job_id: uuid.UUID,
) -> dict:
    principal = await _require_tenant_principal(request, tenant_id)
    if isinstance(principal, Response):
        raise HTTPException(status_code=401, detail="authentication_required")
    if principal.is_admin:
        raise HTTPException(status_code=403, detail="use_admin_account_management")
    async with get_session_factory()() as session:
        job = (
            await session.execute(
                select(models.ProvisioningJob).where(
                    models.ProvisioningJob.id == job_id,
                    models.ProvisioningJob.tenant_id == tenant_id,
                    models.ProvisioningJob.owner_user_id == principal.user_id,
                )
            )
        ).scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="provisioning_job_not_found")
    payload = public_job(job)
    payload["last_error_message"] = (
        _channel_job_error_message(job)
        if job.last_error_code
        else None
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return payload


@router.get("/app/t/{tenant_id}/knowledge", response_class=HTMLResponse)
async def tenant_knowledge(
    request: Request,
    tenant_id: str,
    status_filter: str = "all",
    brand_id: str = "",
) -> Response:
    principal = await _require_tenant_admin_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    if status_filter not in {"all", "draft", "published", "review"}:
        raise HTTPException(status_code=422, detail="invalid_knowledge_status")
    async with get_session_factory()() as session:
        inbox_summary = await _load_inbox_summary(session, principal, tenant_id)
        statement = select(models.KnowledgeDocument).where(
            models.KnowledgeDocument.tenant_id == tenant_id
        )
        if status_filter in {"draft", "published"}:
            statement = statement.where(models.KnowledgeDocument.status == status_filter)
        elif status_filter == "review":
            statement = statement.where(
                or_(
                    models.KnowledgeDocument.language_verified.is_(False),
                    models.KnowledgeDocument.language_detection_status != "confirmed",
                )
            )
        if brand_id:
            statement = statement.where(models.KnowledgeDocument.brand_id == brand_id)
        documents = (
            (
                await session.execute(
                    statement.order_by(models.KnowledgeDocument.updated_at.desc()).limit(200)
                )
            )
            .scalars()
            .all()
        )
        counts = dict(
            (
                await session.execute(
                    select(models.KnowledgeDocument.status, func.count())
                    .where(models.KnowledgeDocument.tenant_id == tenant_id)
                    .group_by(models.KnowledgeDocument.status)
                )
            ).all()
        )
        review_count = await session.scalar(
            select(func.count()).where(
                models.KnowledgeDocument.tenant_id == tenant_id,
                or_(
                    models.KnowledgeDocument.language_verified.is_(False),
                    models.KnowledgeDocument.language_detection_status != "confirmed",
                ),
            )
        )
    next_review = next(
        (
            document
            for document in documents
            if not document.language_verified
            or document.language_detection_status != "confirmed"
        ),
        None,
    )
    filter_tabs = tabs(
        (
            ("all", f"{_tenant_root(tenant_id)}/knowledge", f"全部 {sum(counts.values())}"),
            (
                "review",
                f"{_tenant_root(tenant_id)}/knowledge?status_filter=review",
                f"待审核 {int(review_count or 0)}",
            ),
            (
                "published",
                f"{_tenant_root(tenant_id)}/knowledge?status_filter=published",
                f"已发布 {int(counts.get('published', 0))}",
            ),
            (
                "draft",
                f"{_tenant_root(tenant_id)}/knowledge?status_filter=draft",
                f"草稿 {int(counts.get('draft', 0))}",
            ),
        ),
        status_filter,
    )
    next_review_html = ""
    if next_review:
        review_action = primary_action(
            f"{_tenant_root(tenant_id)}/knowledge/documents/{next_review.id}",
            "审核文档",
        )
        next_review_html = (
            '<section class="saas-next-action"><div><div class="saas-eyebrow">下一条审核</div>'
            f"<h2>{escape(next_review.question)}</h2>"
            f"<p>{escape(next_review.detected_language)} · "
            f"来源 {escape(next_review.source_file or '人工录入')}</p></div>"
            f"{review_action}</section>"
        )
    rows = "".join(
        f"<tr><td><strong>{escape(document.question[:100])}</strong><br>"
        f'<span class="saas-muted">{escape(document.category or "未分类")}</span></td>'
        f"<td>{escape(document.detected_language or document.source_language)}</td>"
        f"<td>{status_badge(document.status)}</td>"
        f"<td>{'已确认' if document.language_verified else '待确认'}</td>"
        f"<td>{format_datetime(document.updated_at)}</td>"
        f'<td><a href="{_tenant_root(tenant_id)}/knowledge/documents/{document.id}">审核 →</a></td></tr>'
        for document in documents
    )
    table = (
        '<div class="saas-table-wrap"><table class="saas-table"><thead><tr>'
        "<th>问题与分类</th><th>语言</th><th>发布状态</th><th>审核</th><th>更新</th><th></th>"
        f"</tr></thead><tbody>{rows}</tbody></table></div>"
        if rows
        else empty_state(
            "没有匹配的知识文档",
            "调整筛选，或进入兼容知识页新增和导入草稿。",
            action_html=primary_action("/admin/content/knowledge", "添加文档"),
        )
    )
    body = f'{filter_tabs}{next_review_html}<div style="margin-top:18px">{table}</div>'
    return _render_page(
        principal=principal,
        tenant_id=tenant_id,
        title="文档与知识",
        description="将来源、语言审核、发布状态和批准答案保持在清晰的治理流程中。",
        body=body,
        active_navigation="knowledge",
        inbox_count=inbox_summary.total,
        primary_action_html=primary_action("/admin/content/knowledge", "添加文档"),
    )


@router.get(
    "/app/t/{tenant_id}/knowledge/documents/{document_id}",
    response_class=HTMLResponse,
)
async def knowledge_document_detail(
    request: Request,
    tenant_id: str,
    document_id: uuid.UUID,
) -> Response:
    principal = await _require_tenant_admin_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    async with get_session_factory()() as session:
        inbox_summary = await _load_inbox_summary(session, principal, tenant_id)
        document = await session.scalar(
            select(models.KnowledgeDocument).where(
                models.KnowledgeDocument.id == document_id,
                models.KnowledgeDocument.tenant_id == tenant_id,
            )
        )
        if document is None:
            raise HTTPException(status_code=404, detail="knowledge_document_not_found")
        chunk_count = await session.scalar(
            select(func.count()).where(
                models.KnowledgeChunk.tenant_id == tenant_id,
                models.KnowledgeChunk.document_id == document_id,
            )
        )
        localization_count = await session.scalar(
            select(func.count()).where(
                models.KnowledgeLocalization.tenant_id == tenant_id,
                models.KnowledgeLocalization.document_id == document_id,
                models.KnowledgeLocalization.status == "published",
            )
        )
        audits = (
            (
                await session.execute(
                    select(models.AuditLog)
                    .where(
                        models.AuditLog.tenant_id == tenant_id,
                        models.AuditLog.subject_type == "knowledge_document",
                        models.AuditLog.subject_id == str(document_id),
                    )
                    .order_by(models.AuditLog.created_at.desc())
                    .limit(20)
                )
            )
            .scalars()
            .all()
        )
    checklist = "".join(
        (
            _progress_row(bool(document.question.strip()), "问题或触发语句存在"),
            _progress_row(bool(document.reply.strip()), "批准回复候选存在"),
            _progress_row(document.language_verified, "语言已经人工确认"),
            _progress_row(bool(chunk_count), f"{int(chunk_count or 0)} 个检索 Chunk 可用"),
            _progress_row(
                not document.is_official_contact or document.status == "published",
                "官方联系方式经过单独发布审查",
                warning=document.is_official_contact,
            ),
        )
    )
    audit_rows = "".join(
        f"<tr><td>{format_datetime(audit.created_at)}</td><td>{escape(audit.actor)}</td>"
        f"<td>{escape(audit.action)}</td></tr>"
        for audit in audits
    )
    body = f"""<div class="saas-grid two" style="margin-top:0">
<section class="saas-card"><div class="saas-card-header"><div><h2>内容</h2>
<p>来源文档和批准回复候选。</p></div>{status_badge(document.status)}</div>
<div class="saas-card-body"><h3>问题</h3><p>{escape(document.question)}</p>
<h3>批准回复候选</h3><p>{escape(document.reply)}</p></div></section>
<section class="saas-card"><div class="saas-card-header"><div><h2>审核清单</h2>
<p>发布不会绕过语言、联系方式和检索治理。</p></div></div>
<div class="saas-card-body"><ul class="saas-progress-list">{checklist}</ul></div></section>
</div>
<div class="saas-grid two">
<section class="saas-card"><div class="saas-card-header"><div><h2>来源与范围</h2></div></div>
<div class="saas-card-body">{definition_list((('Brand', document.brand_id), ('Platform', document.platform or '全平台'), ('分类', document.category or '未分类'), ('来源文件', document.source_file or '人工录入'), ('已发布本地化', int(localization_count or 0)), ('更新时间', format_datetime(document.updated_at, include_year=True))))}</div></section>
<section class="saas-card"><div class="saas-card-header"><div><h2>审计历史</h2></div></div>
<div class="saas-card-body">{f'<table class="saas-table"><tbody>{audit_rows}</tbody></table>' if audit_rows else '<p class="saas-muted">暂无专属审计记录。</p>'}</div></section>
</div>"""
    return _render_page(
        principal=principal,
        tenant_id=tenant_id,
        title="知识文档审核",
        description="确认内容、语言、来源和检索状态，再进入现有发布流程。",
        body=body,
        active_navigation="knowledge",
        inbox_count=inbox_summary.total,
        primary_action_html=primary_action("/admin/content/knowledge", "进入发布操作"),
        breadcrumbs=(("文档与知识", f"{_tenant_root(tenant_id)}/knowledge"), ("审核", None)),
    )


@router.get("/app/t/{tenant_id}/audit", response_class=HTMLResponse)
async def tenant_audit(
    request: Request,
    tenant_id: str,
    category: str = "",
) -> Response:
    principal = await _require_tenant_admin_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    async with get_session_factory()() as session:
        inbox_summary = await _load_inbox_summary(session, principal, tenant_id)
        statement = select(models.AuditLog).where(models.AuditLog.tenant_id == tenant_id)
        if category:
            statement = statement.where(models.AuditLog.category == category)
        audits = (
            (
                await session.execute(
                    statement.order_by(models.AuditLog.created_at.desc()).limit(250)
                )
            )
            .scalars()
            .all()
        )
        categories = list(
            (
                await session.execute(
                    select(models.AuditLog.category)
                    .where(models.AuditLog.tenant_id == tenant_id)
                    .distinct()
                    .order_by(models.AuditLog.category)
                )
            ).scalars()
        )
    category_options = '<option value="">全部类别</option>' + "".join(
        f'<option value="{escape(value)}"'
        f'{" selected" if value == category else ""}>{escape(value)}</option>'
        for value in categories
    )
    filters = f"""<form class="saas-filter-bar" method="get">
<label class="saas-field"><span>类别</span><select name="category">{category_options}</select></label>
<button class="saas-button" type="submit">应用筛选</button></form>"""
    rows = "".join(
        f"<tr><td>{format_datetime(audit.created_at, include_year=True)}</td>"
        f"<td>{escape(audit.actor)}</td><td>{escape(audit.category)}</td>"
        f"<td><strong>{escape(audit.action)}</strong><br>"
        f'<span class="saas-muted">{escape(audit.subject_type)} · '
        f"{escape(audit.subject_id)}</span></td>"
        f'<td><a href="{_tenant_root(tenant_id)}/audit/{audit.id}">查看 →</a></td></tr>'
        for audit in audits
    )
    table = (
        '<div class="saas-table-wrap"><table class="saas-table"><thead><tr>'
        "<th>时间</th><th>Actor</th><th>类别</th><th>动作与资源</th><th></th>"
        f"</tr></thead><tbody>{rows}</tbody></table></div>"
        if rows
        else empty_state("暂无审计记录", "当前筛选范围内没有可展示的不可变审计事实。")
    )
    return _render_page(
        principal=principal,
        tenant_id=tenant_id,
        title="审计中心",
        description="按 Tenant 查看配置、审核、状态转换和系统操作记录。",
        body=f"{filters}{table}",
        active_navigation="audit",
        inbox_count=inbox_summary.total,
    )


@router.get("/app/t/{tenant_id}/audit/{audit_id}", response_class=HTMLResponse)
async def tenant_audit_detail(
    request: Request,
    tenant_id: str,
    audit_id: uuid.UUID,
) -> Response:
    principal = await _require_tenant_admin_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    async with get_session_factory()() as session:
        inbox_summary = await _load_inbox_summary(session, principal, tenant_id)
        audit = await session.scalar(
            select(models.AuditLog).where(
                models.AuditLog.id == audit_id,
                models.AuditLog.tenant_id == tenant_id,
            )
        )
    if audit is None:
        raise HTTPException(status_code=404, detail="audit_not_found")
    body = f"""<div class="saas-grid two" style="margin-top:0">
<section class="saas-card"><div class="saas-card-header"><div><h2>审计事实</h2>
<p>标识、Actor 和资源范围。</p></div></div><div class="saas-card-body">
{definition_list((('Audit ID', audit.id), ('时间', format_datetime(audit.created_at, include_year=True)), ('Actor', audit.actor), ('类别', audit.category), ('动作', audit.action), ('资源类型', audit.subject_type), ('资源 ID', audit.subject_id)))}</div></section>
<section class="saas-card"><div class="saas-card-header"><div><h2>结构化详情</h2>
<p>敏感信息应在写入审计前完成裁剪。</p></div></div><div class="saas-card-body">
{safe_json_details(audit.detail, summary='展开审计详情')}</div></section></div>"""
    return _render_page(
        principal=principal,
        tenant_id=tenant_id,
        title="审计详情",
        description=f"{audit.action} · {audit.subject_type}",
        body=body,
        active_navigation="audit",
        inbox_count=inbox_summary.total,
        breadcrumbs=(("审计中心", f"{_tenant_root(tenant_id)}/audit"), (str(audit.id), None)),
    )


def _journey_decision_join_condition():
    return or_(
        models.ReplyDecision.decision_job_id == models.DecisionJob.id,
        and_(
            models.ReplyDecision.decision_job_id.is_(None),
            models.ReplyDecision.message_id == models.DecisionJob.message_id,
        ),
    )


async def _load_journey_rows(session, tenant_id: str, limit: int = 200) -> list[JourneyRow]:
    rows = (
        await session.execute(
            select(
                models.DecisionJob,
                models.Conversation,
                models.PlatformAccount,
                models.Contact,
                models.ReplyDecision,
            )
            .join(
                models.Conversation,
                models.DecisionJob.conversation_id == models.Conversation.id,
            )
            .join(
                models.PlatformAccount,
                models.DecisionJob.account_id == models.PlatformAccount.id,
            )
            .join(models.Contact, models.Conversation.contact_id == models.Contact.id)
            .outerjoin(models.ReplyDecision, _journey_decision_join_condition())
            .where(
                models.Conversation.tenant_id == tenant_id,
                models.PlatformAccount.tenant_id == tenant_id,
                or_(
                    models.ReplyDecision.id.is_(None),
                    models.ReplyDecision.tenant_id == tenant_id,
                ),
            )
            .order_by(models.DecisionJob.created_at.desc())
            .limit(limit)
        )
    ).all()
    return [
        JourneyRow(
            job=job,
            conversation=conversation,
            account=account,
            contact=contact,
            decision=decision,
        )
        for job, conversation, account, contact, decision in rows
    ]


@router.get("/app/t/{tenant_id}/journeys", response_class=HTMLResponse)
async def tenant_journeys(request: Request, tenant_id: str) -> Response:
    principal = await _require_tenant_admin_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    async with get_session_factory()() as session:
        inbox_summary = await _load_inbox_summary(session, principal, tenant_id)
        journey_rows = await _load_journey_rows(session, tenant_id)
    rows = "".join(
        f"<tr><td>{format_datetime(row.job.created_at, include_year=True)}</td>"
        f"<td><strong>{escape(row.contact.display_name or '匿名联系人')}</strong><br>"
        f'<span class="saas-muted">{escape(row.account.name)} · '
        f"{escape(row.conversation.channel_type)}</span></td>"
        f"<td>{status_badge(row.job.status)}</td>"
        f"<td>{status_badge(row.decision.action) if row.decision else '—'}</td>"
        f"<td>g{escape(row.job.decision_generation if row.job.decision_generation is not None else '—')}</td>"
        f'<td><a href="{_tenant_root(tenant_id)}/journeys/{row.job.id}">查看链路 →</a></td></tr>'
        for row in journey_rows
    )
    table = (
        '<div class="saas-table-wrap"><table class="saas-table"><thead><tr>'
        "<th>开始时间</th><th>会话</th><th>Job</th><th>决策</th><th>Generation</th><th></th>"
        f"</tr></thead><tbody>{rows}</tbody></table></div>"
        if rows
        else empty_state("暂无处理链路", "入站消息形成 DecisionJob 后，会在这里显示完整 Journey。")
    )
    return _render_page(
        principal=principal,
        tenant_id=tenant_id,
        title="Processing Journey",
        description="从入站证据到决策、Outbox 和投递尝试的只读事实链。",
        body=table,
        active_navigation="journeys",
        inbox_count=inbox_summary.total,
    )


@router.get("/app/t/{tenant_id}/journeys/{journey_id}", response_class=HTMLResponse)
async def tenant_journey_detail(
    request: Request,
    tenant_id: str,
    journey_id: uuid.UUID,
) -> Response:
    principal = await _require_tenant_admin_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    decision_alias = aliased(models.ReplyDecision)
    async with get_session_factory()() as session:
        inbox_summary = await _load_inbox_summary(session, principal, tenant_id)
        row = (
            await session.execute(
                select(
                    models.DecisionJob,
                    models.Conversation,
                    models.PlatformAccount,
                    models.Contact,
                    decision_alias,
                    models.RawEvent,
                    models.Message,
                )
                .join(
                    models.Conversation,
                    models.DecisionJob.conversation_id == models.Conversation.id,
                )
                .join(
                    models.PlatformAccount,
                    models.DecisionJob.account_id == models.PlatformAccount.id,
                )
                .join(models.Contact, models.Conversation.contact_id == models.Contact.id)
                .join(models.Message, models.DecisionJob.message_id == models.Message.id)
                .outerjoin(
                    decision_alias,
                    or_(
                        decision_alias.decision_job_id == models.DecisionJob.id,
                        and_(
                            decision_alias.decision_job_id.is_(None),
                            decision_alias.message_id == models.DecisionJob.message_id,
                        ),
                    ),
                )
                .outerjoin(models.RawEvent, models.DecisionJob.raw_event_id == models.RawEvent.id)
                .where(
                    models.DecisionJob.id == journey_id,
                    models.Conversation.tenant_id == tenant_id,
                    models.PlatformAccount.tenant_id == tenant_id,
                    or_(decision_alias.id.is_(None), decision_alias.tenant_id == tenant_id),
                    or_(models.RawEvent.id.is_(None), models.RawEvent.tenant_id == tenant_id),
                )
            )
        ).one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="journey_not_found")
        job, conversation, account, contact, decision, raw_event, message = row
        outbox_ids = [
            outbox_id
            for outbox_id in (
                decision.outbox_id if decision else None,
                decision.review_outbox_id if decision else None,
            )
            if outbox_id is not None
        ]
        outboxes = []
        attempts: list[models.DeliveryAttempt] = []
        if outbox_ids:
            outboxes = list(
                (
                    await session.execute(
                        select(models.OutboxMessage)
                        .where(
                            models.OutboxMessage.id.in_(outbox_ids),
                            models.OutboxMessage.tenant_id == tenant_id,
                        )
                        .order_by(models.OutboxMessage.created_at)
                    )
                ).scalars()
            )
            attempts = list(
                (
                    await session.execute(
                        select(models.DeliveryAttempt)
                        .where(models.DeliveryAttempt.outbox_id.in_(outbox_ids))
                        .order_by(
                            models.DeliveryAttempt.outbox_id,
                            models.DeliveryAttempt.attempt_no,
                        )
                    )
                ).scalars()
            )
    timeline_items = [
        _journey_timeline_item(
            "1",
            "RawEvent 已接收" if raw_event else "无 RawEvent 引用",
            raw_event.processing_status if raw_event else "历史或内部触发",
            raw_event.received_at if raw_event else job.created_at,
            "success" if raw_event else "neutral",
        ),
        _journey_timeline_item(
            "2",
            "Message 已归一化",
            f"{message.direction} · {message.sender_type}",
            message.occurred_at or message.created_at,
            "success",
        ),
        _journey_timeline_item(
            "3",
            "DecisionJob",
            f"{job.status} · attempt {job.attempt_count}",
            job.completed_at or job.created_at,
            "success" if job.status == "COMPLETED" else "warning",
        ),
    ]
    if decision:
        timeline_items.append(
            _journey_timeline_item(
                "4",
                f"ReplyDecision · {decision.action}",
                ", ".join(decision.reason_codes or []) or decision.source,
                decision.created_at,
                "success" if decision.action == "auto_reply" else "warning",
            )
        )
    for outbox in outboxes:
        role_label = "审核 Outbox" if decision and outbox.id == decision.review_outbox_id else "发送 Outbox"
        timeline_items.append(
            _journey_timeline_item(
                "5",
                role_label,
                f"{outbox.status} · {outbox.origin_kind} · attempt {outbox.attempt_count}",
                outbox.sent_at or outbox.created_at,
                "success" if outbox.status == "SENT" else "warning",
            )
        )
    for attempt in attempts:
        timeline_items.append(
            _journey_timeline_item(
                str(attempt.attempt_no),
                "投递尝试",
                f"{attempt.outcome} · {attempt.error_code or '无错误码'}",
                attempt.created_at,
                "success" if attempt.outcome == "SENT" else "warning",
            )
        )
    decision_details = (
        definition_list(
            (
                ("Decision ID", decision.id),
                ("Action", decision.action),
                ("Intent", decision.intent),
                ("Confidence", f"{decision.confidence:.3f}"),
                ("Risk", decision.risk_level),
                ("Prompt", decision.prompt_version),
                ("Business Prompt Version", decision.reply_business_prompt_version_id),
                ("Decision Release", decision.decision_release_sha),
            )
        )
        if decision
        else '<p class="saas-muted">当前 Job 尚未形成 ReplyDecision。</p>'
    )
    evidence_details = safe_json_details(
        {
            "job_snapshot": job.snapshot,
            "raw_event_context": raw_event.context if raw_event else None,
            "decision_rag_evidence": decision.rag_evidence if decision else None,
        },
        summary="展开证据与快照",
    )
    body = f"""<div class="saas-grid" style="grid-template-columns:minmax(0,1fr) 340px;margin-top:0">
<section class="saas-card"><div class="saas-card-header"><div><h2>处理时间线</h2>
<p>阶段来自 PostgreSQL 事实，不根据日志推测。</p></div></div>
<div class="saas-card-body"><ol class="saas-timeline">{''.join(timeline_items)}</ol></div></section>
<aside class="saas-card"><div class="saas-card-header"><div><h2>上下文</h2></div></div>
<div class="saas-card-body">{definition_list((('联系人', contact.display_name or '匿名联系人'), ('账号', account.name), ('平台', conversation.platform), ('会话', conversation.id), ('Generation', job.decision_generation), ('Job ID', job.id)))}</div></aside></div>
<div class="saas-grid two"><section class="saas-card"><div class="saas-card-header"><div><h2>决策来源</h2></div></div>
<div class="saas-card-body">{decision_details}</div></section>
<section class="saas-card"><div class="saas-card-header"><div><h2>结构化证据</h2></div></div>
<div class="saas-card-body">{evidence_details}</div></section></div>"""
    return _render_page(
        principal=principal,
        tenant_id=tenant_id,
        title="Journey 详情",
        description=f"DecisionJob {job.id} 的端到端处理事实。",
        body=body,
        active_navigation="journeys",
        inbox_count=inbox_summary.total,
        breadcrumbs=(("Processing Journey", f"{_tenant_root(tenant_id)}/journeys"), (str(job.id), None)),
    )


def _journey_timeline_item(
    marker: str,
    title: str,
    detail: str,
    occurred_at: datetime | None,
    tone: str,
) -> str:
    return (
        '<li class="saas-timeline-item">'
        f'<span class="saas-timeline-marker {escape(tone)}">{escape(marker)}</span>'
        f'<div><div class="saas-timeline-title">{escape(title)}</div>'
        f'<div class="saas-timeline-detail">{escape(detail)} · '
        f"{format_datetime(occurred_at, include_year=True)}</div></div></li>"
    )


@router.get("/app/t/{tenant_id}/settings", response_class=HTMLResponse)
async def tenant_settings(request: Request, tenant_id: str) -> Response:
    principal = await _require_tenant_admin_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    async with get_session_factory()() as session:
        inbox_summary = await _load_inbox_summary(session, principal, tenant_id)
        user = await session.scalar(
            select(models.AdminUser).where(models.AdminUser.tenant_id == tenant_id)
        )
        account_count = await session.scalar(
            select(func.count()).where(models.PlatformAccount.tenant_id == tenant_id)
        )
    body = f"""<div class="saas-grid two" style="margin-top:0">
<section class="saas-card"><div class="saas-card-header"><div><h2>工作区身份</h2>
<p>Tenant 是所有业务数据查询和授权的边界。</p></div></div><div class="saas-card-body">
{definition_list((('Tenant ID', tenant_id), ('工作区用户', user.username if user else 'Bootstrap 管理员'), ('账号数量', int(account_count or 0)), ('默认安全模式', 'BOT_DRAFT_ONLY')))}</div></section>
<section class="saas-card"><div class="saas-card-header"><div><h2>配置入口</h2>
<p>写操作继续复用现有经过 CSRF 保护的后台。</p></div></div><div class="saas-card-body">
<div style="display:flex;gap:10px;flex-wrap:wrap">{secondary_action('/admin/integrations/accounts', '渠道账号')}{secondary_action('/admin/content/reply-prompt', '业务指令')}{secondary_action('/admin/content/knowledge', '知识治理')}</div>
<div class="saas-alert warning" style="margin-top:18px">生产配置事实源仍是 Railway 服务变量。工作区设置不会覆盖跨角色安全配置或功能开关。</div></div></section></div>"""
    return _render_page(
        principal=principal,
        tenant_id=tenant_id,
        title="工作区设置",
        description="查看租户身份、访问边界和受控配置入口。",
        body=body,
        active_navigation="settings",
        inbox_count=inbox_summary.total,
    )


async def _require_system_principal(request: Request) -> Principal | Response:
    principal = await _require_web_principal(request)
    if isinstance(principal, Response):
        return principal
    if not principal.is_superadmin:
        raise HTTPException(status_code=403, detail="system_admin_required")
    return principal


def _render_system_page(
    *,
    principal: Principal,
    title: str,
    description: str,
    body: str,
    active_navigation: str,
) -> HTMLResponse:
    return HTMLResponse(
        render_saas_page(
            principal=principal,
            title=title,
            description=description,
            body=body,
            active_navigation=active_navigation,
            tenant_id=None,
            system_admin=True,
        )
    )


@router.get("/admin/system/overview", response_class=HTMLResponse)
async def system_overview(request: Request) -> Response:
    principal = await _require_system_principal(request)
    if isinstance(principal, Response):
        return principal
    async with get_session_factory()() as session:
        tenant_count = await session.scalar(select(func.count()).select_from(models.AdminUser))
        account_count = await session.scalar(
            select(func.count()).select_from(models.PlatformAccount)
        )
        disabled_account_count = await session.scalar(
            select(func.count()).where(models.PlatformAccount.status == "DISABLED")
        )
        failed_outbox_count = await session.scalar(
            select(func.count()).where(
                models.OutboxMessage.status.in_(("FAILED", "NEEDS_REVIEW"))
            )
        )
        recent_audits = list(
            (
                await session.execute(
                    select(models.AuditLog)
                    .order_by(models.AuditLog.created_at.desc())
                    .limit(12)
                )
            ).scalars()
        )
    audit_rows = "".join(
        f"<tr><td>{format_datetime(audit.created_at)}</td><td>{escape(audit.tenant_id)}</td>"
        f"<td>{escape(audit.actor)}</td><td>{escape(audit.action)}</td></tr>"
        for audit in recent_audits
    )
    body = f"""<section class="saas-alert warning" style="margin-bottom:18px">
这是跨租户特权区域。租户业务操作应返回对应工作区完成。</section>
<div class="saas-grid four">
{metric_card(int(tenant_count or 0), 'Tenant 用户')}
{metric_card(int(account_count or 0), '平台账号')}
{metric_card(int(disabled_account_count or 0), '禁用账号')}
{metric_card(int(failed_outbox_count or 0), '投递风险')}</div>
<div class="saas-grid two"><section class="saas-card"><div class="saas-card-header"><div><h2>系统操作</h2>
<p>沿用现有健康、安全和访问管理能力。</p></div></div><div class="saas-card-body">
<div style="display:flex;gap:10px;flex-wrap:wrap">{secondary_action('/admin/system/health', '系统健康')}{secondary_action('/admin/system/safety', '安全控制')}{secondary_action('/admin/system/users', '用户与访问')}</div></div></section>
<section class="saas-card"><div class="saas-card-header"><div><h2>最近跨租户活动</h2></div>
{secondary_action('/admin/system/audit', '查看全部', small=True)}</div><div class="saas-card-body">
{f'<table class="saas-table"><tbody>{audit_rows}</tbody></table>' if audit_rows else '<p class="saas-muted">暂无审计记录。</p>'}</div></section></div>"""
    return _render_system_page(
        principal=principal,
        title="系统总览",
        description="查看跨租户运行风险并进入受控系统操作。",
        body=body,
        active_navigation="system-overview",
    )


@router.get("/admin/system/audit", response_class=HTMLResponse)
async def system_audit(request: Request, tenant_id: str = "") -> Response:
    principal = await _require_system_principal(request)
    if isinstance(principal, Response):
        return principal
    async with get_session_factory()() as session:
        statement = select(models.AuditLog)
        if tenant_id:
            statement = statement.where(models.AuditLog.tenant_id == tenant_id)
        audits = list(
            (
                await session.execute(
                    statement.order_by(models.AuditLog.created_at.desc()).limit(500)
                )
            ).scalars()
        )
        tenant_ids = list(
            (
                await session.execute(
                    select(models.AuditLog.tenant_id)
                    .distinct()
                    .order_by(models.AuditLog.tenant_id)
                )
            ).scalars()
        )
    tenant_options = '<option value="">全部 Tenant</option>' + "".join(
        f'<option value="{escape(value)}"'
        f'{" selected" if value == tenant_id else ""}>{escape(value)}</option>'
        for value in tenant_ids
    )
    rows = "".join(
        f"<tr><td>{format_datetime(audit.created_at, include_year=True)}</td>"
        f"<td>{escape(audit.tenant_id)}</td><td>{escape(audit.actor)}</td>"
        f"<td>{escape(audit.category)}</td><td>{escape(audit.action)}</td>"
        f"<td>{escape(audit.subject_type)} · {escape(audit.subject_id)}</td></tr>"
        for audit in audits
    )
    body = f"""<form class="saas-filter-bar" method="get"><label class="saas-field">
<span>Tenant</span><select name="tenant_id">{tenant_options}</select></label>
<button class="saas-button" type="submit">应用筛选</button></form>
<div class="saas-table-wrap"><table class="saas-table"><thead><tr>
<th>时间</th><th>Tenant</th><th>Actor</th><th>类别</th><th>动作</th><th>资源</th>
</tr></thead><tbody>{rows}</tbody></table></div>"""
    return _render_system_page(
        principal=principal,
        title="跨租户审计",
        description="系统管理员只读查看所有 Tenant 的审计事实。",
        body=body,
        active_navigation="system-audit",
    )


@router.get("/help", response_class=HTMLResponse)
async def product_help(request: Request) -> Response:
    principal = await _require_web_principal(request)
    if isinstance(principal, Response):
        return principal
    body = """<div class="saas-grid two" style="margin-top:0">
<section class="saas-card"><div class="saas-card-header"><div><h2>从哪里开始</h2></div></div>
<div class="saas-card-body"><ol class="saas-progress-list">
<li class="saas-progress-item"><span class="saas-progress-icon done">1</span><span>选择 Tenant 并查看首页下一步。</span></li>
<li class="saas-progress-item"><span class="saas-progress-icon done">2</span><span>在 Agents 中检查指令、模型、渠道和知识。</span></li>
<li class="saas-progress-item"><span class="saas-progress-icon done">3</span><span>在收件箱按最老等待处理人工、草稿和投递风险。</span></li>
</ol></div></section>
<section class="saas-card"><div class="saas-card-header"><div><h2>安全边界</h2></div></div>
<div class="saas-card-body"><p>新账号默认仅生成草稿。外发始终经过 Tenant 检查、kill switch、Generation、Final Guard、幂等和 Outbox 约束。</p>
<p>Processing Journey 与审计中心是只读事实视图，不会改变业务状态。</p></div></section></div>"""
    return HTMLResponse(
        render_saas_page(
            principal=principal,
            title="帮助中心",
            description="Reply Core SaaS 工作区的快速使用说明。",
            body=body,
            active_navigation="",
            tenant_id=None,
        )
    )
