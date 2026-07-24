"""运营后台控制台：总览 / 对话 / 决策 / 知识库 / 投递 / 账号与急停。

与 admin.py 共享服务端会话与 CSRF；全部查询和写操作按当前 Principal 租户范围过滤。
"""

import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import redis.asyncio as aioredis
from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import and_, desc, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from social_reply.application.account_management.admin import (
    _CSRF_COOKIE,
    _csrf,
    _form,
    _input,
    _page,
    _pill,
    _require_csrf,
    _secure_cookie,
    _web_principal,
    html,
)
from social_reply.application.account_management.auth import Principal
from social_reply.application.account_management.jobs import provisioning_job_is_in_flight
from social_reply.application.account_management.oauth.common import notice
from social_reply.application.account_management.provisioning import tenant_public_id
from social_reply.application.account_management.service import enable_xchat_for_account
from social_reply.application.account_management.xchat_activation import XChatActivationError
from social_reply.application.message_delivery.contracts import (
    build_direct_reply_destination,
)
from social_reply.application.reply_decision.persist import _idempotency_key
from social_reply.domain.automation.state_machine import (
    AutomationStateEnum,
    can_transition,
    flip_to_human_active,
)
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.queue.dispatch import dispatch_actor
from social_reply.shared.config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin-console"])


def _fmt(dt: datetime | None) -> str:
    return dt.strftime("%m-%d %H:%M") if dt else "—"


def _ensure_csrf(response: Response, request: Request, csrf: str) -> Response:
    if not request.cookies.get(_CSRF_COOKIE):
        response.set_cookie(
            _CSRF_COOKIE, csrf, httponly=False, samesite="lax", secure=_secure_cookie(request)
        )
    return response


def _tenant_input(principal: Principal) -> str:
    if principal.tenant_id is not None:
        return _input(
            "tenant_id",
            "Tenant",
            value=principal.tenant_id,
            readonly=True,
        )
    options = "".join(
        f'<option value="{html.escape(tenant)}">{html.escape(tenant)}</option>'
        for tenant in sorted(principal.allowed_tenants)
    )
    return (
        '<label for="f-tenant-select">Tenant</label>'
        f'<select id="f-tenant-select" name="tenant_id" required>{options}</select>'
    )


# ---------- 总览 ----------


_RAW_ACTION_STATUSES = (
    "INITIAL_DISPATCH_DEAD",
    "DECISION_NEEDS_REVIEW",
    "XCHAT_PIN_REQUIRED",
    "XCHAT_KEY_RECOVERY_REQUIRED",
    "XCHAT_DECRYPT_FAILED",
    "XCHAT_RETRY_EXHAUSTED",
    "XCHAT_REAUTHORIZATION_REQUIRED",
    "XCHAT_ACCESS_FORBIDDEN",
    "XCHAT_DECRYPT_MISSING_OUTPUT",
    "XCHAT_PUBLIC_KEY_LOOKUP_FAILED",
)
_RAW_WARNING_STATUSES = (
    "INITIAL_DISPATCH_RETRY",
    "INITIAL_DISPATCHING",
    "DECISION_PENDING",
    "DECISION_DEFERRED",
    "XCHAT_DECRYPTION_PENDING",
    "XCHAT_PROCESSING",
    "XCHAT_RETRYABLE_ERROR",
)


def _raw_action_condition():
    return or_(
        models.RawEvent.processing_status.in_(_RAW_ACTION_STATUSES),
        models.RawEvent.processing_status.like("XCHAT_PUBLIC_KEY_HTTP_%"),
    )


def _raw_warning_condition():
    return or_(
        and_(
            models.RawEvent.processing_status == "PENDING",
            models.RawEvent.context.op("?")("initial_dispatch"),
        ),
        models.RawEvent.processing_status.in_(_RAW_WARNING_STATUSES),
    )


@dataclass(frozen=True)
class _HealthMetric:
    key: str
    label: str
    action_count: int
    warning_count: int
    oldest_at: datetime | None
    href: str

    @property
    def level(self) -> str:
        if self.action_count:
            return "ACTION"
        if self.warning_count:
            return "WARNING"
        return "HEALTHY"


def _health_age(now: datetime, oldest_at: datetime | None) -> str:
    if oldest_at is None:
        return "—"
    if oldest_at.tzinfo is None:
        oldest_at = oldest_at.replace(tzinfo=UTC)
    seconds = max(int((now - oldest_at).total_seconds()), 0)
    if seconds < 60:
        return "刚刚"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} 分钟"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} 小时"
    return f"{hours // 24} 天"


async def _load_health_metrics(
    session, tenants: frozenset[str], now: datetime
) -> list[_HealthMetric]:
    raw_action = _raw_action_condition()
    raw_warning = _raw_warning_condition()
    raw_row = (
        await session.execute(
            select(
                func.count().filter(raw_action),
                func.count().filter(raw_warning),
                func.min(models.RawEvent.received_at).filter(or_(raw_action, raw_warning)),
            ).where(models.RawEvent.tenant_id.in_(tenants))
        )
    ).one()

    decision_action = models.DecisionJob.status == "NEEDS_REVIEW"
    decision_warning = models.DecisionJob.status.in_(
        ("PENDING", "PROCESSING", "FAILED", "DEFERRED_CHATWOOT")
    )
    decision_row = (
        await session.execute(
            select(
                func.count().filter(decision_action),
                func.count().filter(decision_warning),
                func.min(models.DecisionJob.created_at).filter(
                    or_(decision_action, decision_warning)
                ),
            )
            .select_from(models.DecisionJob)
            .join(
                models.PlatformAccount,
                models.PlatformAccount.id == models.DecisionJob.account_id,
            )
            .where(models.PlatformAccount.tenant_id.in_(tenants))
        )
    ).one()

    outbox_action = models.OutboxMessage.status == "NEEDS_REVIEW"
    outbox_warning = models.OutboxMessage.status.in_(("PENDING", "SENDING", "FAILED"))
    outbox_row = (
        await session.execute(
            select(
                func.count().filter(outbox_action),
                func.count().filter(outbox_warning),
                func.min(models.OutboxMessage.created_at).filter(
                    or_(outbox_action, outbox_warning)
                ),
            ).where(models.OutboxMessage.tenant_id.in_(tenants))
        )
    ).one()

    retry_grace = now - timedelta(minutes=2)
    provisioning_action = or_(
        models.ProvisioningJob.status == "NEEDS_ACTION",
        and_(
            models.ProvisioningJob.status == "FAILED",
            or_(
                models.ProvisioningJob.next_attempt_at.is_(None),
                models.ProvisioningJob.next_attempt_at < retry_grace,
            ),
        ),
    )
    provisioning_warning = or_(
        models.ProvisioningJob.status.in_(("PENDING", "PROCESSING", "PAUSED_PLATFORM_DISABLED")),
        and_(
            models.ProvisioningJob.status == "FAILED",
            models.ProvisioningJob.next_attempt_at >= retry_grace,
        ),
    )
    provisioning_row = (
        await session.execute(
            select(
                func.count().filter(provisioning_action),
                func.count().filter(provisioning_warning),
                func.min(models.ProvisioningJob.created_at).filter(
                    or_(provisioning_action, provisioning_warning)
                ),
            ).where(models.ProvisioningJob.tenant_id.in_(tenants))
        )
    ).one()

    active_gap = models.SyncGap.status.in_(("OPEN", "RETRYING"))
    sync_action = and_(active_gap, models.SyncGap.gap_type == "DECRYPT_ERROR")
    sync_warning = and_(active_gap, models.SyncGap.gap_type != "DECRYPT_ERROR")
    sync_row = (
        await session.execute(
            select(
                func.count().filter(sync_action),
                func.count().filter(sync_warning),
                func.min(models.SyncGap.created_at).filter(active_gap),
            )
            .select_from(models.SyncGap)
            .join(
                models.PlatformCheckpoint,
                models.PlatformCheckpoint.id == models.SyncGap.checkpoint_id,
            )
            .join(
                models.PlatformAccount,
                models.PlatformAccount.id == models.PlatformCheckpoint.platform_account_id,
            )
            .where(
                models.PlatformCheckpoint.tenant_id.in_(tenants),
                models.PlatformAccount.tenant_id.in_(tenants),
            )
        )
    ).one()

    account_action = models.PlatformAccount.status == "DISABLED"
    account_row = (
        await session.execute(
            select(
                func.count().filter(account_action),
                func.min(models.PlatformAccount.created_at).filter(account_action),
            ).where(models.PlatformAccount.tenant_id.in_(tenants))
        )
    ).one()

    return [
        _HealthMetric("ingestion", "入站恢复", *raw_row, "/admin/delivery"),
        _HealthMetric("decisions", "决策任务", *decision_row, "/admin/decisions"),
        _HealthMetric("delivery", "消息投递", *outbox_row, "/admin/delivery"),
        _HealthMetric("provisioning", "账号接入", *provisioning_row, "/admin/accounts"),
        _HealthMetric("sync", "X 同步", *sync_row, "/admin/accounts"),
        _HealthMetric(
            "accounts",
            "账号状态",
            int(account_row[0]),
            0,
            account_row[1],
            "/admin/accounts",
        ),
    ]


@router.get("", response_class=HTMLResponse)
async def overview(request: Request) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    tenants = principal.allowed_tenants
    now = datetime.now(UTC)
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = now - timedelta(days=7)
    async with get_session_factory()() as session:
        msg_today = (
            await session.execute(
                select(func.count())
                .select_from(models.Message)
                .join(models.Conversation, models.Message.conversation_id == models.Conversation.id)
                .where(
                    models.Conversation.tenant_id.in_(tenants),
                    models.Message.created_at >= today0,
                )
            )
        ).scalar_one()
        conv_total = (
            await session.execute(
                select(func.count())
                .select_from(models.Conversation)
                .where(models.Conversation.tenant_id.in_(tenants))
            )
        ).scalar_one()
        human_active = (
            await session.execute(
                select(func.count())
                .select_from(models.AutomationState)
                .join(
                    models.Conversation,
                    models.AutomationState.conversation_id == models.Conversation.id,
                )
                .where(
                    models.Conversation.tenant_id.in_(tenants),
                    models.AutomationState.state.in_(["HUMAN_ACTIVE", "HANDOFF_PENDING"]),
                )
            )
        ).scalar_one()
        action_counts = dict(
            (
                await session.execute(
                    select(models.ReplyDecision.action, func.count())
                    .where(
                        models.ReplyDecision.tenant_id.in_(tenants),
                        models.ReplyDecision.created_at >= week_ago,
                    )
                    .group_by(models.ReplyDecision.action)
                )
            ).all()
        )
        outbox_counts = dict(
            (
                await session.execute(
                    select(models.OutboxMessage.status, func.count())
                    .where(
                        models.OutboxMessage.tenant_id.in_(tenants),
                        models.OutboxMessage.created_at >= week_ago,
                    )
                    .group_by(models.OutboxMessage.status)
                )
            ).all()
        )
        recent = (
            (
                await session.execute(
                    select(models.ReplyDecision)
                    .where(models.ReplyDecision.tenant_id.in_(tenants))
                    .order_by(desc(models.ReplyDecision.created_at))
                    .limit(8)
                )
            )
            .scalars()
            .all()
        )
        health_metrics = await _load_health_metrics(session, tenants, now)

    auto = action_counts.get("auto_reply", 0)
    handled = auto + action_counts.get("draft", 0) + action_counts.get("handoff", 0)
    deflection = f"{auto / handled * 100:.0f}%" if handled else "—"
    sent = outbox_counts.get("SENT", 0)
    failed = outbox_counts.get("FAILED", 0) + outbox_counts.get("NEEDS_REVIEW", 0)
    send_total = sent + failed
    send_rate = f"{sent / send_total * 100:.0f}%" if send_total else "—"

    stats = f"""<div class="stats">
<div class="stat"><div class="num">{msg_today}</div><div class="lbl">今日消息</div></div>
<div class="stat"><div class="num">{deflection}</div><div class="lbl">7 日自动化处理率</div></div>
<div class="stat"><div class="num">{conv_total}</div><div class="lbl">累计对话</div></div>
<div class="stat"><div class="num">{human_active}</div><div class="lbl">待人工 / 接管中</div></div>
<div class="stat"><div class="num">{send_rate}</div><div class="lbl">7 日投递成功率</div></div>
</div>"""

    total_actions = sum(action_counts.values()) or 1
    tone_map = {"auto_reply": "ok", "draft": "warn", "handoff": "err", "ignore": "neutral"}
    label_map = {
        "auto_reply": "自动回复",
        "draft": "草稿",
        "handoff": "转人工",
        "ignore": "忽略",
    }
    bars = "".join(
        f'<div class="bar-row"><span class="bar-label">{label_map[a]}</span>'
        f'<div class="bar-track"><div class="bar {tone_map[a]}" style="width:{action_counts.get(a, 0) / total_actions * 100:.0f}%"></div></div>'
        f'<span class="bar-count">{action_counts.get(a, 0)}</span></div>'
        for a in ("auto_reply", "draft", "handoff", "ignore")
    )
    health_rows = "".join(
        f'<tr data-health="{metric.key}"><td><strong>{metric.label}</strong></td>'
        f"<td>{_pill(metric.level)}</td>"
        f"<td>{metric.action_count} 需处理 · {metric.warning_count} 恢复中</td>"
        f"<td class='muted'>{_health_age(now, metric.oldest_at)}</td>"
        f"<td><a href='{metric.href}'>查看</a></td></tr>"
        for metric in health_metrics
    )
    health = f"""<section class="card"><h2>运行健康</h2><p class="hint">当前积压与需人工处理项。</p>
<div class="tablewrap"><table><thead><tr><th>环节</th><th>状态</th><th>积压</th><th>最老等待</th><th></th></tr></thead><tbody>{health_rows}</tbody></table></div></section>"""
    recent_rows = (
        "".join(
            f"<tr><td class='muted'>{_fmt(d.created_at)}</td><td>{_pill(d.action)}</td>"
            f"<td class='muted'>{html.escape(d.intent or '—')}</td>"
            f"<td>{html.escape((d.reply_text or '—')[:46])}</td>"
            f"<td><a href='/admin/conversations/{d.conversation_id}'>查看对话</a></td></tr>"
            for d in recent
        )
        or "<tr><td colspan='5' class='muted'>暂无决策记录</td></tr>"
    )
    body = f"""<h1>总览</h1><p class="lede">自动回复运行状况与近 7 日决策分布。</p>{stats}{health}
<div class="grid" style="grid-template-columns:1fr 1.4fr">
<section class="card"><h2>决策分布</h2><p class="hint">近 7 日各动作占比。</p>{bars}</section>
<section class="card"><h2>最近决策</h2><p class="hint">最新 8 条 AI 决策。</p><div class="tablewrap"><table><thead><tr><th>时间</th><th>动作</th><th>意图</th><th>回复预览</th><th></th></tr></thead><tbody>{recent_rows}</tbody></table></div></section>
</div>"""
    return HTMLResponse(_page("总览", body, active="overview", show_users=principal.is_superadmin))


# ---------- 对话收件箱 ----------


@router.get("/conversations", response_class=HTMLResponse)
async def conversations_page(request: Request) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    tenants = principal.allowed_tenants
    last_msg = (
        select(
            models.Message.conversation_id.label("cid"),
            func.max(models.Message.created_at).label("last_at"),
        )
        .group_by(models.Message.conversation_id)
        .subquery()
    )
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(
                    models.Conversation,
                    models.Contact.display_name,
                    models.Contact.external_user_id,
                    models.AutomationState.state,
                    models.PlatformAccount.name.label("account_name"),
                    last_msg.c.last_at,
                )
                .join(models.Contact, models.Conversation.contact_id == models.Contact.id)
                .join(
                    models.AutomationState,
                    models.AutomationState.conversation_id == models.Conversation.id,
                    isouter=True,
                )
                .join(
                    models.PlatformAccount,
                    models.Conversation.platform_account_id == models.PlatformAccount.id,
                )
                .join(last_msg, last_msg.c.cid == models.Conversation.id, isouter=True)
                .where(models.Conversation.tenant_id.in_(tenants))
                .order_by(desc(func.coalesce(last_msg.c.last_at, models.Conversation.created_at)))
                .limit(50)
            )
        ).all()
    trs = (
        "".join(
            f"<tr><td>{html.escape(conv.platform)}</td>"
            f"<td><a href='/admin/conversations/{conv.id}'>{html.escape(display or external or '匿名用户')}</a></td>"
            f"<td class='muted'>{html.escape(account_name)}</td>"
            f"<td>{_pill(state or 'BOT_ACTIVE')}</td>"
            f"<td class='muted'>{_fmt(last_at or conv.created_at)}</td></tr>"
            for conv, display, external, state, account_name, last_at in rows
        )
        or "<tr><td colspan='5' class='muted'>暂无对话</td></tr>"
    )
    body = f"""<h1>对话</h1><p class="lede">最近活跃的客户对话；点击进入线程查看与接管。</p>
<section class="card"><div class="tablewrap"><table><thead><tr><th>平台</th><th>联系人</th><th>账号</th><th>自动化状态</th><th>最后活跃</th></tr></thead><tbody>{trs}</tbody></table></div></section>"""
    return HTMLResponse(
        _page("对话", body, active="conversations", show_users=principal.is_superadmin)
    )


_TRANSITION_LABELS = {
    "HUMAN_ACTIVE": ("人工接管", "btn-danger"),
    "BOT_ACTIVE": ("恢复自动回复", ""),
    "BOT_DRAFT_ONLY": ("切为草稿模式", "btn-ghost"),
    "BOT_COOLDOWN": ("结束接管（冷却）", "btn-ghost"),
}


@router.get("/conversations/{conversation_id}", response_class=HTMLResponse)
async def conversation_detail(request: Request, conversation_id: uuid.UUID) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    csrf = _csrf(request)
    async with get_session_factory()() as session:
        conv = await session.get(models.Conversation, conversation_id)
        if conv is None or conv.tenant_id not in principal.allowed_tenants:
            raise HTTPException(status_code=404, detail="conversation_not_found")
        contact = await session.get(models.Contact, conv.contact_id)
        state_row = (
            await session.execute(
                select(models.AutomationState).where(
                    models.AutomationState.conversation_id == conversation_id
                )
            )
        ).scalar_one_or_none()
        msgs = (
            (
                await session.execute(
                    select(models.Message)
                    .where(models.Message.conversation_id == conversation_id)
                    .order_by(models.Message.created_at)
                    .limit(200)
                )
            )
            .scalars()
            .all()
        )
        decisions = (
            (
                await session.execute(
                    select(models.ReplyDecision)
                    .where(models.ReplyDecision.conversation_id == conversation_id)
                    .order_by(desc(models.ReplyDecision.created_at))
                    .limit(10)
                )
            )
            .scalars()
            .all()
        )
    cur_state = state_row.state if state_row else "BOT_ACTIVE"
    bubbles = (
        "".join(
            f"<div class='msg {'in' if m.direction == 'inbound' else 'out'}'>"
            f"{html.escape(m.text or '（非文本消息）')}"
            f"<div class='meta'>{'客户' if m.direction == 'inbound' else '机器人/客服'} · {_fmt(m.created_at)}</div></div>"
            for m in msgs
        )
        or "<p class='muted'>暂无消息</p>"
    )
    cur = AutomationStateEnum(cur_state) if cur_state in AutomationStateEnum.__members__ else None
    buttons = "".join(
        f"""<form class="inline" method="post" action="/admin/conversations/{conversation_id}/state">
<input type="hidden" name="csrf_token" value="{csrf}"><input type="hidden" name="target" value="{dst}">
<input type="hidden" name="expect" value="{cur_state}"><button class="btn-sm {cls}">{label}</button></form>"""
        for dst, (label, cls) in _TRANSITION_LABELS.items()
        if cur is not None and can_transition(cur, AutomationStateEnum(dst))
    )
    decision_rows = (
        "".join(
            f"<tr><td class='muted'>{_fmt(d.created_at)}</td><td>{_pill(d.action)}</td>"
            f"<td class='muted'>{html.escape(d.intent or '—')}</td>"
            f"<td>{html.escape((d.reply_text or '—')[:60])}</td>"
            f"<td class='muted'>{d.confidence if d.confidence is not None else '—'}</td></tr>"
            for d in decisions
        )
        or "<tr><td colspan='5' class='muted'>暂无决策</td></tr>"
    )
    who = html.escape(
        (contact.display_name if contact else None)
        or (contact.external_user_id if contact else "")
        or "匿名用户"
    )
    body = f"""<a class="back" href="/admin/conversations">← 返回对话列表</a>
<section class="card"><h1 style="font-size:24px">{who}<span style="margin-left:12px">{_pill(cur_state)}</span></h1>
<p class="hint">{html.escape(conv.platform)} · 会话键 <code>{html.escape(conv.conversation_key)}</code></p>
<div style="margin:6px 0 4px">{buttons}</div>
<div class="thread">{bubbles}</div></section>
<section class="card"><h2>本会话决策</h2><div class="tablewrap"><table><thead><tr><th>时间</th><th>动作</th><th>意图</th><th>回复</th><th>置信度</th></tr></thead><tbody>{decision_rows}</tbody></table></div></section>"""
    response = HTMLResponse(
        _page(
            "对话详情",
            body,
            active="conversations",
            show_users=principal.is_superadmin,
        )
    )
    return _ensure_csrf(response, request, csrf)


@router.post("/conversations/{conversation_id}/state")
async def flip_conversation_state(request: Request, conversation_id: uuid.UUID) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    target, expect = form.get("target", ""), form.get("expect", "")
    if target not in _TRANSITION_LABELS or expect not in AutomationStateEnum.__members__:
        raise HTTPException(status_code=422, detail="invalid_state_transition")
    if not can_transition(AutomationStateEnum(expect), AutomationStateEnum(target)):
        raise HTTPException(status_code=422, detail="transition_not_allowed")
    async with get_session_factory()() as session:
        conv = await session.get(models.Conversation, conversation_id)
        if conv is None or conv.tenant_id not in principal.allowed_tenants:
            raise HTTPException(status_code=404, detail="conversation_not_found")
        # CAS：仅当仍处于提交时看到的状态才翻转（防并发接管竞态）
        if target == AutomationStateEnum.HUMAN_ACTIVE:
            await flip_to_human_active(
                session,
                conversation_id,
                principal.actor,
                "admin_manual",
                expected_state=AutomationStateEnum(expect),
            )
        else:
            await session.execute(
                update(models.AutomationState)
                .where(
                    models.AutomationState.conversation_id == conversation_id,
                    models.AutomationState.state == expect,
                )
                .values(
                    state=target,
                    state_version=models.AutomationState.state_version + 1,
                    state_changed_reason="admin_manual",
                )
            )
        await session.commit()
    return RedirectResponse(
        f"/admin/conversations/{conversation_id}", status_code=status.HTTP_303_SEE_OTHER
    )


# ---------- 决策与草稿审核 ----------


@router.get("/decisions", response_class=HTMLResponse)
async def decisions_page(request: Request, action: str = "") -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    csrf = _csrf(request)
    tenants = principal.allowed_tenants
    valid_actions = ("auto_reply", "draft", "handoff", "ignore")
    action = action if action in valid_actions else ""
    async with get_session_factory()() as session:
        pending_drafts = (
            (
                await session.execute(
                    select(models.ReplyDecision)
                    .where(
                        models.ReplyDecision.tenant_id.in_(tenants),
                        models.ReplyDecision.action == "draft",
                        models.ReplyDecision.outbox_id.is_(None),
                        models.ReplyDecision.reply_text.isnot(None),
                        ~models.ReplyDecision.reason_codes.contains(["ADMIN_DISCARDED"]),
                    )
                    .order_by(desc(models.ReplyDecision.created_at))
                    .limit(20)
                )
            )
            .scalars()
            .all()
        )
        stmt = (
            select(models.ReplyDecision)
            .where(models.ReplyDecision.tenant_id.in_(tenants))
            .order_by(desc(models.ReplyDecision.created_at))
            .limit(100)
        )
        if action:
            stmt = stmt.where(models.ReplyDecision.action == action)
        decisions = (await session.execute(stmt)).scalars().all()

    draft_cards = (
        "".join(
            f"""<div class="card" style="margin-bottom:14px"><p style="margin:0 0 4px">{html.escape(d.reply_text or "")}</p>
<p class="muted" style="margin:0 0 10px">意图 {html.escape(d.intent or "—")} · {_fmt(d.created_at)} · <a href="/admin/conversations/{d.conversation_id}">查看对话</a></p>
<form class="inline" method="post" action="/admin/decisions/{d.id}/approve"><input type="hidden" name="csrf_token" value="{csrf}"><button class="btn-sm">采纳并发送</button></form>
<form class="inline" method="post" action="/admin/decisions/{d.id}/discard"><input type="hidden" name="csrf_token" value="{csrf}"><button class="btn-sm btn-ghost">忽略</button></form>
</div>"""
            for d in pending_drafts
        )
        or "<p class='muted'>没有待审核的草稿。</p>"
    )
    chips = (
        '<div class="chips">'
        + "".join(
            f'<a class="chip{" active" if action == a else ""}" href="/admin/decisions{("?action=" + a) if a else ""}">{label}</a>'
            for a, label in (
                ("", "全部"),
                ("auto_reply", "自动回复"),
                ("draft", "草稿"),
                ("handoff", "转人工"),
                ("ignore", "忽略"),
            )
        )
        + "</div>"
    )
    rows = (
        "".join(
            f"<tr><td class='muted'>{_fmt(d.created_at)}</td><td>{_pill(d.action)}</td>"
            f"<td class='muted'>{html.escape(d.intent or '—')}</td>"
            f"<td>{html.escape((d.reply_text or '—')[:56])}</td>"
            f"<td class='muted'>{html.escape(d.source or '—')}</td>"
            f"<td><a href='/admin/conversations/{d.conversation_id}'>对话</a></td></tr>"
            for d in decisions
        )
        or "<tr><td colspan='6' class='muted'>暂无决策</td></tr>"
    )
    body = f"""<h1>决策</h1><p class="lede">AI 决策日志与草稿人工审核。</p>
<section class="card"><h2>待审核草稿</h2><p class="hint">草稿模式下生成的回复；采纳后按原文投递给客户。</p>{draft_cards}</section>
<section class="card"><h2>决策日志</h2>{chips}<div class="tablewrap"><table><thead><tr><th>时间</th><th>动作</th><th>意图</th><th>回复预览</th><th>来源</th><th></th></tr></thead><tbody>{rows}</tbody></table></div></section>"""
    response = HTMLResponse(
        _page("决策", body, active="decisions", show_users=principal.is_superadmin)
    )
    return _ensure_csrf(response, request, csrf)


async def _load_draft(
    session, decision_id: uuid.UUID, principal: Principal
) -> models.ReplyDecision:
    decision = await session.get(models.ReplyDecision, decision_id)
    if decision is None or decision.tenant_id not in principal.allowed_tenants:
        raise HTTPException(status_code=404, detail="decision_not_found")
    if decision.action != "draft" or decision.outbox_id is not None:
        raise HTTPException(status_code=409, detail="decision_not_pending_draft")
    return decision


@router.post("/decisions/{decision_id}/approve")
async def approve_draft(request: Request, decision_id: uuid.UUID) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    async with get_session_factory()() as session:
        decision = await _load_draft(session, decision_id, principal)
        conv = await session.get(models.Conversation, decision.conversation_id)
        if conv is None or conv.tenant_id != decision.tenant_id:
            raise HTTPException(status_code=409, detail="decision_tenant_scope_mismatch")
        account = await session.get(models.PlatformAccount, conv.platform_account_id)
        if account is None or account.tenant_id != decision.tenant_id:
            raise HTTPException(status_code=409, detail="decision_tenant_scope_mismatch")
        if (account.config or {}).get("delivery_mode") != "direct":
            raise HTTPException(status_code=409, detail="approve_only_supports_direct_platforms")
        reply_target: dict = {}
        occurred_at = None
        if decision.message_id is not None:
            msg = await session.get(models.Message, decision.message_id)
            if msg is None or msg.conversation_id != decision.conversation_id:
                raise HTTPException(status_code=409, detail="decision_message_scope_mismatch")
            reply_target = dict(msg.reply_target or {})
            occurred_at = msg.occurred_at
        visibility = decision.reply_visibility or "public"
        try:
            destination = build_direct_reply_destination(
                platform=account.platform,
                reply_target=reply_target,
                visibility=visibility,
                occurred_at=occurred_at,
                now=datetime.now(UTC),
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=409,
                detail="unsupported_platform_for_approve",
            ) from exc
        destination_type = destination.destination_type
        reply_target = destination.target
        valid_until = destination.valid_until
        outbox_id = (
            await session.execute(
                pg_insert(models.OutboxMessage)
                .values(
                    id=uuid.uuid4(),
                    tenant_id=account.tenant_id,
                    conversation_id=conv.id,
                    platform_account_id=account.id,
                    destination_type=destination_type,
                    destination_id=conv.conversation_key,
                    message_type="text",
                    payload={
                        "text": decision.reply_text or "",
                        "visibility": visibility,
                        "target": reply_target,
                        "approval": "admin",
                        "approved_by": principal.actor,
                    },
                    idempotency_key=_idempotency_key(
                        account.id, conv.id, decision.message_id or conv.id, "draft_approved"
                    ),
                    status="PENDING",
                    valid_until=valid_until,
                )
                .on_conflict_do_nothing(index_elements=["idempotency_key"])
                .returning(models.OutboxMessage.id)
            )
        ).scalar_one_or_none()
        if outbox_id is not None:
            await session.execute(
                update(models.ReplyDecision)
                .where(models.ReplyDecision.id == decision_id)
                .values(outbox_id=outbox_id)
            )
            await session.execute(
                models.AuditLog.__table__.insert().values(
                    tenant_id=account.tenant_id,
                    category="admin_action",
                    actor=principal.actor,
                    action="APPROVE_DRAFT",
                    subject_type="reply_decision",
                    subject_id=str(decision_id),
                    detail={"outbox_id": str(outbox_id)},
                )
            )
        await session.commit()
    if outbox_id is not None:
        from social_reply.application.message_delivery.actors import deliver_outbox_message
        from social_reply.application.message_delivery.outbox import deliver_outbox

        await dispatch_actor(
            deliver_outbox_message,
            str(outbox_id),
            inline=lambda: deliver_outbox(str(outbox_id)),
        )
    return RedirectResponse("/admin/decisions", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/decisions/{decision_id}/discard")
async def discard_draft(request: Request, decision_id: uuid.UUID) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    async with get_session_factory()() as session:
        decision = await _load_draft(session, decision_id, principal)
        await session.execute(
            update(models.ReplyDecision)
            .where(models.ReplyDecision.id == decision_id)
            .values(reason_codes=list(decision.reason_codes or []) + ["ADMIN_DISCARDED"])
        )
        await session.commit()
    return RedirectResponse("/admin/decisions", status_code=status.HTTP_303_SEE_OTHER)


# ---------- 知识库 ----------

_KB_BANNERS = {
    "added": ("ok", "知识条目已添加并完成向量化。"),
    "duplicate": ("err", "内容重复：相同问答已存在。"),
    "embed_failed": ("err", "向量化失败：请检查 Embedding 服务配置后重试。"),
    "toggled": ("ok", "状态已更新。"),
    "deleted": ("ok", "条目已删除。"),
    "import_bad_csv": (
        "err",
        "CSV 无效：需 UTF-8 编码，必需列 question,reply，最多 2000 行。",
    ),
    "import_too_large": ("err", "文件过大：请上传不超过 2MB 的 CSV。"),
}

_MAX_IMPORT_BYTES = 2 * 1024 * 1024


def _query_int(request: Request, name: str) -> int:
    try:
        return max(0, int(request.query_params.get(name) or 0))
    except ValueError:
        return 0


@router.get("/knowledge", response_class=HTMLResponse)
async def knowledge_page(request: Request, notice: str = "") -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    csrf = _csrf(request)
    tenants = principal.allowed_tenants
    async with get_session_factory()() as session:
        docs = (
            (
                await session.execute(
                    select(models.KnowledgeDocument)
                    .where(models.KnowledgeDocument.tenant_id.in_(tenants))
                    .order_by(desc(models.KnowledgeDocument.created_at))
                    .limit(200)
                )
            )
            .scalars()
            .all()
        )
    banner = ""
    if notice == "imported":
        n = _query_int(request, "inserted")
        s = _query_int(request, "skipped")
        b = _query_int(request, "blank")
        banner = (
            f'<div class="banner ok">导入完成：新增 {n} 条，跳过 {s} 条重复，忽略 {b} 条空行</div>'
        )
    elif notice in _KB_BANNERS:
        tone, text = _KB_BANNERS[notice]
        banner = f'<div class="banner {tone}">{text}</div>'
    rows = (
        "".join(
            f"<tr><td>{html.escape((d.question or '')[:40])}</td>"
            f"<td class='muted'>{html.escape((d.reply or '')[:48])}</td>"
            f"<td class='muted'>{html.escape(d.category or '—')}</td>"
            f"<td>{_pill(d.status)}</td>"
            f"""<td><form class="inline" method="post" action="/admin/knowledge/{d.id}/status"><input type="hidden" name="csrf_token" value="{csrf}"><button class="btn-sm btn-ghost">{"下架" if d.status == "published" else "上架"}</button></form>
<form class="inline" method="post" action="/admin/knowledge/{d.id}/delete"><input type="hidden" name="csrf_token" value="{csrf}"><button class="btn-sm btn-danger" onclick="return confirm('确认删除该知识条目？此操作不可恢复。')">删除</button></form></td></tr>"""
            for d in docs
        )
        or "<tr><td colspan='5' class='muted'>知识库为空</td></tr>"
    )
    add_form = f"""<details class="collapse"><summary>新增知识条目</summary><div class="inner">
<form method="post" action="/admin/knowledge/add"><input type="hidden" name="csrf_token" value="{csrf}">
{_tenant_input(principal)}{_input("question", "触发问题（用户会怎么问）")}
<label for="f-kb-reply">标准回复（命中后原文发送）</label><textarea id="f-kb-reply" name="reply" required></textarea>
{_input("category", "分类（可选）", required=False)}{_input("brand_id", "Brand（默认 default）", required=False)}
<button class="btn-block">添加并向量化</button></form></div></details>"""
    import_form = f"""<details class="collapse"><summary>批量导入 CSV</summary><div class="inner">
<form method="post" action="/admin/knowledge/import" enctype="multipart/form-data"><input type="hidden" name="csrf_token" value="{csrf}">
{_tenant_input(principal)}{_input("brand_id", "Brand（默认 default）", required=False)}
<label for="f-kb-csv">CSV 文件</label><input id="f-kb-csv" type="file" name="file" accept=".csv" required>
<p class="hint">必需列 question,reply；可选 brand_id,platform,category。UTF-8 编码，最多 2000 行 / 2MB。</p>
<button class="btn-block">上传并导入</button></form></div></details>"""
    body = f"""<h1>知识库</h1><p class="lede">回复模板管理：命中即原文直答；下架条目不参与检索。</p>{banner}
{add_form}
{import_form}
<section class="card"><h2>模板列表</h2><p class="hint">共 {len(docs)} 条（最多显示 200）。</p><div class="tablewrap"><table><thead><tr><th>问题</th><th>回复</th><th>分类</th><th>状态</th><th>操作</th></tr></thead><tbody>{rows}</tbody></table></div></section>"""
    response = HTMLResponse(
        _page("知识库", body, active="knowledge", show_users=principal.is_superadmin)
    )
    return _ensure_csrf(response, request, csrf)


@router.post("/knowledge/add")
async def knowledge_add(request: Request) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    tenant_id = (form.get("tenant_id") or "").strip()
    if tenant_id not in principal.allowed_tenants:
        raise HTTPException(status_code=403, detail="tenant_access_denied")
    question = (form.get("question") or "").strip()
    reply = (form.get("reply") or "").strip()
    if not question or not reply:
        raise HTTPException(status_code=422, detail="question_and_reply_required")
    content = f"问：{question}\n答：{reply}"
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    from social_reply.application.reply_decision.runner import _get_embedder

    try:
        embedder = _get_embedder()
        embedding = (await embedder.embed([question]))[0]
    except Exception:
        return RedirectResponse(
            "/admin/knowledge?notice=embed_failed", status_code=status.HTTP_303_SEE_OTHER
        )
    async with get_session_factory()() as session:
        exists = (
            await session.execute(
                select(models.KnowledgeChunk.id)
                .join(
                    models.KnowledgeDocument,
                    models.KnowledgeChunk.document_id == models.KnowledgeDocument.id,
                )
                .where(
                    models.KnowledgeDocument.tenant_id == tenant_id,
                    models.KnowledgeChunk.content_hash == content_hash,
                )
            )
        ).first()
        if exists:
            return RedirectResponse(
                "/admin/knowledge?notice=duplicate", status_code=status.HTTP_303_SEE_OTHER
            )
        doc = models.KnowledgeDocument(
            tenant_id=tenant_id,
            brand_id=(form.get("brand_id") or "").strip() or "default",
            category=(form.get("category") or "").strip() or None,
            question=question,
            reply=reply,
            source_file="admin-console",
        )
        session.add(doc)
        await session.flush()
        session.add(
            models.KnowledgeChunk(
                tenant_id=tenant_id,
                document_id=doc.id,
                content=content,
                embed_text=question,
                content_hash=content_hash,
                embedding_version=embedder.version,
                embedding=embedding,
            )
        )
        await session.commit()
    return RedirectResponse("/admin/knowledge?notice=added", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/knowledge/import")
async def knowledge_import(request: Request) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    form = await request.form()
    _require_csrf(request, {"csrf_token": str(form.get("csrf_token") or "")})
    tenant_id = str(form.get("tenant_id") or "").strip()
    if tenant_id not in principal.allowed_tenants:
        raise HTTPException(status_code=403, detail="tenant_access_denied")
    upload = form.get("file")
    filename = getattr(upload, "filename", None)
    read = getattr(upload, "read", None)
    if not callable(read):
        return RedirectResponse(
            "/admin/knowledge?notice=import_bad_csv", status_code=status.HTTP_303_SEE_OTHER
        )
    raw = await read(_MAX_IMPORT_BYTES + 1)
    if len(raw) > _MAX_IMPORT_BYTES:
        return RedirectResponse(
            "/admin/knowledge?notice=import_too_large", status_code=status.HTTP_303_SEE_OTHER
        )
    if not raw:
        return RedirectResponse(
            "/admin/knowledge?notice=import_bad_csv", status_code=status.HTTP_303_SEE_OTHER
        )
    try:
        text_csv = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return RedirectResponse(
            "/admin/knowledge?notice=import_bad_csv", status_code=status.HTTP_303_SEE_OTHER
        )
    brand_id_default = str(form.get("brand_id") or "").strip() or "default"
    source_name = (str(filename or "").strip() or "import.csv")[:256]
    import io

    from social_reply.application.knowledge.importer import import_knowledge_rows
    from social_reply.application.reply_decision.runner import _get_embedder

    try:
        embedder = _get_embedder()
        report = await import_knowledge_rows(
            io.StringIO(text_csv),
            source_name=source_name,
            embedder=embedder,
            tenant_id=tenant_id,
            brand_id_default=brand_id_default,
        )
    except ValueError:
        return RedirectResponse(
            "/admin/knowledge?notice=import_bad_csv", status_code=status.HTTP_303_SEE_OTHER
        )
    except Exception:
        return RedirectResponse(
            "/admin/knowledge?notice=embed_failed", status_code=status.HTTP_303_SEE_OTHER
        )
    return RedirectResponse(
        f"/admin/knowledge?notice=imported&inserted={report.inserted}"
        f"&skipped={report.skipped}&blank={report.blank}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/knowledge/{doc_id}/status")
async def knowledge_toggle(request: Request, doc_id: uuid.UUID) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    async with get_session_factory()() as session:
        doc = await session.get(models.KnowledgeDocument, doc_id)
        if doc is None or doc.tenant_id not in principal.allowed_tenants:
            raise HTTPException(status_code=404, detail="knowledge_not_found")
        doc.status = "draft" if doc.status == "published" else "published"
        await session.commit()
    return RedirectResponse(
        "/admin/knowledge?notice=toggled", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/knowledge/{doc_id}/delete")
async def knowledge_delete(request: Request, doc_id: uuid.UUID) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    async with get_session_factory()() as session:
        doc = await session.get(models.KnowledgeDocument, doc_id)
        if doc is None or doc.tenant_id not in principal.allowed_tenants:
            raise HTTPException(status_code=404, detail="knowledge_not_found")
        await session.delete(doc)  # chunk 级联删除（FK ondelete=CASCADE）
        await session.commit()
    return RedirectResponse(
        "/admin/knowledge?notice=deleted", status_code=status.HTTP_303_SEE_OTHER
    )


# ---------- 投递监控 ----------


@router.get("/delivery", response_class=HTMLResponse)
async def delivery_page(request: Request) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    csrf = _csrf(request)
    tenants = principal.allowed_tenants
    day_ago = datetime.now(UTC) - timedelta(hours=24)
    async with get_session_factory()() as session:
        outbox = (
            (
                await session.execute(
                    select(models.OutboxMessage)
                    .where(models.OutboxMessage.tenant_id.in_(tenants))
                    .order_by(desc(models.OutboxMessage.created_at))
                    .limit(50)
                )
            )
            .scalars()
            .all()
        )
        ingress = (
            await session.execute(
                select(
                    models.RawEvent.source,
                    models.RawEvent.processing_status,
                    func.count(func.distinct(models.RawEvent.id)),
                    func.max(models.RawEvent.received_at),
                )
                .outerjoin(
                    models.NormalizedEvent,
                    models.NormalizedEvent.raw_event_id == models.RawEvent.id,
                )
                .where(
                    or_(
                        models.RawEvent.tenant_id.in_(tenants),
                        and_(
                            models.RawEvent.tenant_id.is_(None),
                            models.NormalizedEvent.tenant_id.in_(tenants),
                        ),
                    ),
                    or_(
                        models.RawEvent.received_at >= day_ago,
                        _raw_action_condition(),
                        _raw_warning_condition(),
                    ),
                )
                .group_by(models.RawEvent.source, models.RawEvent.processing_status)
            )
        ).all()
    rows = (
        "".join(
            f"<tr><td class='muted'>{_fmt(o.created_at)}</td><td>{_pill(o.status)}</td>"
            f"<td class='muted'>{html.escape(o.destination_type)}</td>"
            f"<td>{html.escape((o.payload or {}).get('text', '')[:42])}</td>"
            f"<td class='muted'>{o.attempt_count}</td>"
            f"<td class='muted'>{html.escape(o.last_error_code or '—')}</td>"
            + (
                f"""<td><form class="inline" method="post" action="/admin/delivery/{o.id}/retry"><input type="hidden" name="csrf_token" value="{csrf}"><button class="btn-sm btn-ghost">重试</button></form></td>"""
                if o.status == "FAILED"
                else "<td></td>"
            )
            + "</tr>"
            for o in outbox
        )
        or "<tr><td colspan='7' class='muted'>暂无投递记录</td></tr>"
    )
    ingress_rows = (
        "".join(
            f"<tr><td>{html.escape(source)}</td><td>{_pill(processing_status)}</td>"
            f"<td class='muted'>{count}</td><td class='muted'>{_fmt(last_at)}</td></tr>"
            for source, processing_status, count, last_at in ingress
        )
        or "<tr><td colspan='4' class='muted'>24 小时内无入站事件</td></tr>"
    )
    body = f"""<h1>投递</h1><p class="lede">出站消息投递状态与入站事件健康度。</p>
<section class="card"><h2>Outbox</h2><p class="hint">最近 50 条出站消息；失败可手动重试（由补扫循环拾取）。</p><div class="tablewrap"><table><thead><tr><th>时间</th><th>状态</th><th>目的地</th><th>内容</th><th>尝试</th><th>错误</th><th></th></tr></thead><tbody>{rows}</tbody></table></div></section>
<section class="card"><h2>入站健康（24h）</h2><div class="tablewrap"><table><thead><tr><th>来源</th><th>处理状态</th><th>事件数</th><th>最后接收</th></tr></thead><tbody>{ingress_rows}</tbody></table></div></section>"""
    response = HTMLResponse(
        _page("投递", body, active="delivery", show_users=principal.is_superadmin)
    )
    return _ensure_csrf(response, request, csrf)


@router.post("/delivery/{outbox_id}/retry")
async def delivery_retry(request: Request, outbox_id: uuid.UUID) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    async with get_session_factory()() as session:
        row = await session.get(models.OutboxMessage, outbox_id)
        if row is None or row.tenant_id not in principal.allowed_tenants:
            raise HTTPException(status_code=404, detail="outbox_not_found")
        if row.status != "FAILED":
            raise HTTPException(status_code=409, detail="outbox_not_retryable")
        await session.execute(
            update(models.OutboxMessage)
            .where(models.OutboxMessage.id == outbox_id)
            .values(status="PENDING", next_attempt_at=None, locked_at=None, locked_by=None)
        )
        await session.execute(
            models.AuditLog.__table__.insert().values(
                tenant_id=row.tenant_id,
                category="admin_action",
                actor=principal.actor,
                action="RETRY_CONFIRMED_FAILURE",
                subject_type="outbox",
                subject_id=str(outbox_id),
                detail={"previous_error_code": row.last_error_code},
            )
        )
        await session.commit()
    return RedirectResponse("/admin/delivery", status_code=status.HTTP_303_SEE_OTHER)


# ---------- 账号 / 急停 / 接入 ----------


def _kill_keys(tenant: str, account_ids: list[str]) -> list[str]:
    return [f"killswitch:global:{tenant}"] + [
        f"killswitch:account:{tenant}:{aid}" for aid in account_ids
    ]


@router.get("/accounts", response_class=HTMLResponse)
async def accounts_page(request: Request) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    oauth_banner = ""
    if request.query_params.get("provider") == "x":
        oauth_status = request.query_params.get("status")
        if oauth_status == "connected":
            oauth_banner = '<div class="banner ok">X 账号授权并连接成功。</div>'
        elif oauth_status == "processing":
            oauth_banner = '<div class="banner info">X 账号连接正在后台完成，请稍后刷新。</div>'
        elif oauth_status == "error":
            raw_code = request.query_params.get("code") or "oauth_failed"
            safe_code = (
                "".join(
                    character
                    for character in raw_code[:64]
                    if character.isascii() and (character.isalnum() or character in {"_", "-"})
                )
                or "oauth_failed"
            )
            oauth_banner = (
                '<div class="banner err">X 授权未完成。错误代码：'
                f"<code>{html.escape(safe_code)}</code></div>"
            )
    csrf = _csrf(request)
    settings = get_settings()
    tenants = principal.allowed_tenants
    async with get_session_factory()() as session:
        accounts = (
            (
                await session.execute(
                    select(models.PlatformAccount)
                    .where(models.PlatformAccount.tenant_id.in_(tenants))
                    .order_by(models.PlatformAccount.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        jobs = (
            (
                await session.execute(
                    select(models.ProvisioningJob)
                    .where(models.ProvisioningJob.tenant_id.in_(tenants))
                    .order_by(models.ProvisioningJob.created_at.desc())
                    .limit(20)
                )
            )
            .scalars()
            .all()
        )
    redis = aioredis.from_url(settings.redis_url)
    try:
        account_keys = [f"killswitch:account:{a.tenant_id}:{a.id}" for a in accounts]
        account_flags = await redis.mget(account_keys) if account_keys else []
        global_flags = (
            await redis.mget([f"killswitch:global:{tenant}" for tenant in tenants])
            if principal.is_superadmin
            else []
        )
    finally:
        await redis.aclose()
    account_stopped = {
        str(account.id): account_flags[index] is not None for index, account in enumerate(accounts)
    }
    killswitch_card = ""
    if principal.is_superadmin:
        global_rows = "".join(
            f"<form class='inline' method='post' action='/admin/killswitch/toggle'>"
            f"<input type='hidden' name='csrf_token' value='{csrf}'>"
            f"<input type='hidden' name='scope' value='global'>"
            f"<input type='hidden' name='tenant_id' value='{html.escape(tenant)}'>"
            f"<button class='btn-sm {'btn-ghost' if global_flags[index] else 'btn-danger'}'>"
            f"{html.escape(tenant)}："
            f"{'解除急停' if global_flags[index] else '全局急停'}</button></form>"
            for index, tenant in enumerate(tenants)
        )
        killswitch_card = f"""<section class="card"><h2>自动回复总开关</h2>
<p class="hint">急停按租户隔离，启用后该租户自动回复降级为草稿。</p>{global_rows}</section>"""

    account_rows = ""
    for a in accounts:
        stopped = account_stopped.get(str(a.id), False)
        ks_pill = (
            '<span class="pill err">急停</span>' if stopped else '<span class="pill ok">正常</span>'
        )
        ks_btn = "解除" if stopped else "急停"
        ks_cls = "btn-ghost" if stopped else "btn-danger"
        auto_target = "BOT_DRAFT_ONLY" if a.automation_default == "BOT_ACTIVE" else "BOT_ACTIVE"
        auto_label = "切为草稿" if a.automation_default == "BOT_ACTIVE" else "切为自动"
        xchat_form = ""
        channel_status = "—"
        if a.platform == "x":
            account_config = dict(a.config or {})
            capability = dict(a.capability or {})
            xchat_state = str(
                account_config.get("xchat_key_state")
                or ("READY" if capability.get("x_chat") else "UNKNOWN")
            )
            xchat_registered = account_config.get("xchat_registered") is True
            subscriptions = dict(account_config.get("x_activity_subscriptions") or {})
            legacy_subscription = str(
                (subscriptions.get("dm.received") or {}).get("status") or "UNKNOWN"
            )
            xchat_subscription = str(
                (subscriptions.get("chat.received") or {}).get("status") or "UNKNOWN"
            )
            channel_status = (
                f"<div>Legacy DM {_pill('READY' if capability.get('dm') else 'DISABLED')}</div>"
                f"<div>DM Activity {_pill(legacy_subscription)}</div>"
                f"<div>XChat Key {_pill(xchat_state)}</div>"
                f"<div>XChat Activity {_pill(xchat_subscription)}</div>"
            )
            if settings.xchat_enabled and xchat_registered and not capability.get("x_chat", False):
                xchat_form = f"""<form class="inline" method="post" action="/admin/accounts/{a.id}/xchat"><input type="hidden" name="csrf_token" value="{csrf}"><input type="password" name="xchat_pin" inputmode="numeric" pattern="[0-9]{{4}}" maxlength="4" placeholder="XChat PIN" required><button class="btn-sm btn-ghost">恢复 XChat 密钥</button></form>"""
        elif a.platform in {"facebook", "instagram"}:
            account_config = dict(a.config or {})
            health_status = str(account_config.get("meta_health_status") or "UNKNOWN")
            subscribed = ", ".join(account_config.get("meta_subscribed_fields") or []) or "—"
            error_code = str(account_config.get("meta_health_error_code") or "—")
            channel_status = (
                f"<div>Messaging {_pill(health_status)}</div>"
                f"<div class='muted'>{html.escape(subscribed)}</div>"
                f"<div class='muted'>{html.escape(error_code)}</div>"
            )
        account_rows += (
            f"<tr><td>{html.escape(a.platform)}</td><td>{html.escape(a.name)}</td>"
            f"<td>{_pill(a.status)}</td><td>{channel_status}</td>"
            f"<td>{_pill(a.automation_default)}</td><td>{ks_pill}</td>"
            f"""<td><form class="inline" method="post" action="/admin/accounts/{a.id}/automation"><input type="hidden" name="csrf_token" value="{csrf}"><input type="hidden" name="target" value="{auto_target}"><button class="btn-sm btn-ghost">{auto_label}</button></form>
<form class="inline" method="post" action="/admin/killswitch/toggle"><input type="hidden" name="csrf_token" value="{csrf}"><input type="hidden" name="scope" value="account"><input type="hidden" name="account_id" value="{a.id}"><input type="hidden" name="tenant_id" value="{html.escape(a.tenant_id)}"><button class="btn-sm {ks_cls}">{ks_btn}</button></form>{xchat_form}</td></tr>"""
        )
    account_rows = account_rows or "<tr><td colspan='7' class='muted'>尚未连接账号</td></tr>"

    job_rows = (
        "".join(
            f"<tr><td><a href='/admin/jobs/{row.id}'><code>{str(row.id)[:8]}</code></a></td>"
            f"<td>{html.escape(row.platform)}</td>"
            f"<td>{_pill('PROCESSING' if row.status == 'FAILED' and provisioning_job_is_in_flight(row) else row.status)}</td>"
            f"<td class='muted'>{html.escape(row.current_step)}</td>"
            f"<td class='muted'>{html.escape(row.last_error_code or '—')}</td></tr>"
            for row in jobs
        )
        or "<tr><td colspan='5' class='muted'>暂无任务</td></tr>"
    )
    default_tenant = "default" if "default" in tenants else sorted(tenants)[0]
    common = (
        f'<input type="hidden" name="csrf_token" value="{csrf}">'
        + _tenant_input(principal)
        + _input("brand_id", "Brand", required=True, value="default")
        + _input("name", "显示名称（可选）", required=False)
    )
    webhook_tenant = default_tenant
    oauth_callback = f"{settings.public_base_url.rstrip('/')}/admin/oauth/x/callback"
    x_webhook = (
        f"{settings.public_base_url.rstrip('/')}/webhooks/x/"
        f"{tenant_public_id('x_oauth', webhook_tenant)}"
    )
    meta_callback = f"{settings.public_base_url.rstrip('/')}/admin/oauth/meta/callback"
    meta_webhook = (
        f"{settings.public_base_url.rstrip('/')}/webhooks/meta/"
        f"{tenant_public_id('meta_oauth', webhook_tenant)}"
    )
    instagram_callback = f"{settings.public_base_url.rstrip('/')}/admin/oauth/instagram/callback"
    instagram_webhook = (
        f"{settings.public_base_url.rstrip('/')}/webhooks/meta/"
        f"{tenant_public_id('instagram_oauth', webhook_tenant)}"
    )
    oauth_common = (
        f'<input type="hidden" name="csrf_token" value="{csrf}">'
        + _tenant_input(principal)
        + _input("brand_id", "Brand", required=True, value="default")
    )
    xchat_oauth_input = (
        _input(
            "xchat_pin",
            "XChat 4 位 PIN（可选，启用加密私信）",
            secret=True,
            required=False,
        )
        if settings.xchat_enabled
        else ""
    )
    xchat_manual_input = (
        _input(
            "xchat_pin",
            "XChat 4 位 PIN（启用加密私信，建议填写）",
            secret=True,
            required=False,
        )
        if settings.xchat_enabled
        else ""
    )
    x_permission_hint = (
        "App permissions 设为 Read and write and Direct message；"
        "授权确认页必须明确列出 Direct Messages 权限"
        if settings.x_legacy_dm_enabled or settings.xchat_enabled
        else "Legacy DM 与 XChat 均已关闭，不要求 Direct Messages 权限"
    )
    x_activity_hint = (
        f"Activity/Webhook URL 使用 <code>{html.escape(x_webhook)}</code>。"
        if settings.x_activity_enabled
        else "Activity webhook 已关闭，系统仅使用低频 reconciliation。"
    )
    x_card_style = "" if settings.x_integration_enabled else ' style="display:none"'
    meta_enabled = settings.facebook_messenger_enabled or settings.instagram_messaging_enabled
    meta_card_style = "" if meta_enabled else ' style="display:none"'
    instagram_card_style = "" if settings.instagram_messaging_enabled else ' style="display:none"'
    whatsapp_card_style = "" if settings.whatsapp_enabled else ' style="display:none"'
    meta_options = "".join(
        option
        for enabled, option in (
            (
                settings.facebook_messenger_enabled,
                '<option value="facebook">Facebook Page</option>',
            ),
            (
                settings.instagram_messaging_enabled,
                '<option value="instagram">关联 Facebook Page 的 Instagram 专业账号</option>',
            ),
        )
        if enabled
    )
    connect_open = "" if principal.is_superadmin else " open"
    connect_title = "接入新平台账号" if principal.is_superadmin else "授权新平台账号"
    connect_forms = f"""<details class="collapse"{connect_open}><summary>{connect_title}</summary><div class="inner">
<p class="hint">提交后创建持久化任务；凭证只进入 Secret 存储，不写入任务 JSON。OAuth 卡片会跳转平台授权页自动换取凭证；手工卡片用于粘贴已有凭证。</p>
<div class="grid">
<form class="card"{x_card_style} method="post" action="/admin/oauth/x/start"><h3>X · OAuth 一键授权（推荐）</h3>{oauth_common}{xchat_oauth_input}<p class="hint">使用环境变量 <code>X_API_KEY</code> / <code>X_API_SECRET</code> 中的 Consumer Keys；每次点击都可授权一个账号，账号 Token 会独立加密入库。X Developer Portal 中将 {x_permission_hint}、App type 设为 Web App，并精确登记 Callback URI <code>{html.escape(oauth_callback)}</code>。{x_activity_hint}</p><button class="btn-block">授权新的 X 账号</button></form>
<form class="card"{meta_card_style} method="post" action="/admin/oauth/meta/start"><h3>Facebook Login · 多账号 OAuth</h3>{oauth_common}<label for="f-oauth-meta-platform">目标</label><select id="f-oauth-meta-platform" name="platform">{meta_options}</select><p class="hint">使用部署级 <code>FACEBOOK_APP_ID</code> / <code>FACEBOOK_APP_SECRET</code> / <code>META_VERIFY_TOKEN</code>，授权后列出全部可管理目标并选择一个接入，可反复授权多个账号。Callback：<code>{html.escape(meta_callback)}</code>；Webhook：<code>{html.escape(meta_webhook)}</code>。</p><button class="btn-block">跳转 Facebook 授权</button></form>
<form class="card"{instagram_card_style} method="post" action="/admin/oauth/instagram/start"><h3>Instagram Login · 独立账号 OAuth</h3>{oauth_common}<p class="hint">无需 Facebook Page，适用于 Instagram 专业账号。使用 <code>INSTAGRAM_APP_ID</code> / <code>INSTAGRAM_APP_SECRET</code>；Callback：<code>{html.escape(instagram_callback)}</code>；Webhook：<code>{html.escape(instagram_webhook)}</code>。</p><button class="btn-block">跳转 Instagram 授权</button></form>
<form class="card" method="post" action="/admin/connect/telegram"><h3>Telegram</h3>{common}{_input("token", "Bot Token", secret=True)}<p class="hint">Telegram 无 OAuth：在 Telegram 中找 @BotFather 发送 /newbot 创建机器人，把返回的 Token 粘贴到上方，提交后自动校验并注册 webhook。</p><button class="btn-block">连接 Telegram</button></form>
<form class="card"{meta_card_style} method="post" action="/admin/connect/meta"><h3>Facebook / Instagram</h3>{common}<label for="f-meta-platform">平台</label><select id="f-meta-platform" name="platform">{meta_options}</select>{_input("external_account_id", "Page / IG Account ID")}{_input("page_id", "Facebook Page ID（关联 Page 的 Instagram 必填）", required=False)}{_input("access_token", "Access Token", secret=True)}{_input("app_secret", "Meta App Secret", secret=True)}{_input("app_id", "Meta App ID", required=False)}{_input("app_public_id", "Existing App Public ID", required=False)}{_input("verify_token", "Webhook Verify Token", secret=True)}<input type="hidden" name="instagram_login_mode" value="facebook_login"><input type="hidden" name="enable_dm" value="true"><input type="hidden" name="enable_comments" value="false"><input type="hidden" name="automation_default" value="BOT_DRAFT_ONLY"><button class="btn-block">连接 Meta 私信</button></form>
<form class="card"{whatsapp_card_style} method="post" action="/admin/connect/whatsapp"><h3>WhatsApp</h3>{common}{_input("external_account_id", "Phone Number ID")}{_input("access_token", "Access Token", secret=True)}{_input("app_secret", "Meta App Secret", secret=True)}{_input("app_id", "Meta App ID", required=False)}{_input("app_public_id", "Existing App Public ID", required=False)}{_input("verify_token", "Webhook Verify Token", secret=True)}<button class="btn-block">连接 WhatsApp</button></form>
<form class="card"{x_card_style} method="post" action="/admin/connect/x"><h3>X</h3>{common}{_input("consumer_key", "Consumer Key", secret=True)}{_input("consumer_secret", "Consumer Secret", secret=True)}{_input("access_token", "Access Token", secret=True)}{_input("access_token_secret", "Access Token Secret", secret=True)}<input type="hidden" name="environment" value="oauth">{xchat_manual_input}<button class="btn-block">连接 X</button></form>
</div></div></details>"""
    account_card = f"""<section class="card"><h2>平台账号</h2><div class="tablewrap"><table><thead><tr><th>平台</th><th>名称</th><th>状态</th><th>消息通道</th><th>新会话默认</th><th>急停</th><th>操作</th></tr></thead><tbody>{account_rows}</tbody></table></div></section>"""
    jobs_card = f"""<section class="card"><h2>Provisioning Jobs</h2><p class="hint">最近 20 条接入任务。</p><div class="tablewrap"><table><thead><tr><th>ID</th><th>平台</th><th>状态</th><th>步骤</th><th>错误</th></tr></thead><tbody>{job_rows}</tbody></table></div></section>"""
    if principal.is_superadmin:
        body = f"""<h1>账号</h1><p class="lede">已接入账号的运行控制、急停开关与接入任务。</p>
{oauth_banner}{killswitch_card}{account_card}{jobs_card}{connect_forms}"""
    else:
        body = f"""<h1>账号授权</h1><p class="lede">授权并管理当前 Tenant 的平台账号。</p>
{oauth_banner}{connect_forms}{account_card}{jobs_card}"""
    response = HTMLResponse(
        _page("账号", body, active="accounts", show_users=principal.is_superadmin)
    )
    return _ensure_csrf(response, request, csrf)


@router.post("/accounts/{account_id}/xchat")
async def enable_account_xchat(request: Request, account_id: uuid.UUID) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    if not get_settings().xchat_enabled:
        raise HTTPException(status_code=503, detail="xchat_disabled")
    form = await _form(request)
    _require_csrf(request, form)
    async with get_session_factory()() as session:
        account = await session.get(models.PlatformAccount, account_id)
    if (
        account is None
        or account.tenant_id not in principal.allowed_tenants
        or account.platform != "x"
    ):
        raise HTTPException(status_code=404, detail="x_account_not_found")
    pin = (form.get("xchat_pin") or "").strip()
    if len(pin) != 4 or not pin.isdigit():
        raise HTTPException(status_code=422, detail="invalid_xchat_pin")
    try:
        await enable_xchat_for_account(account_id=account_id, pin=pin)
    except XChatActivationError as exc:
        logger.warning("xchat activation failed account=%s code=%s", account_id, exc.code)
        return notice(
            "启用 XChat 失败",
            f"{exc.operator_message}（错误代码：{exc.code}）",
            status_code=exc.status_code,
        )
    except Exception as exc:  # noqa: BLE001 - platform boundary; never echo the PIN
        logger.exception(
            "unexpected xchat activation failure account=%s type=%s",
            account_id,
            type(exc).__name__,
        )
        return notice(
            "启用 XChat 失败",
            "系统未能完成 XChat 密钥恢复，请稍后重试。"
            "如果问题持续存在，请检查 Railway API 日志。"
            "（错误代码：XCHAT_ACTIVATION_FAILED）",
            status_code=500,
        )
    return RedirectResponse("/admin/accounts", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/accounts/{account_id}/automation")
async def flip_account_automation(request: Request, account_id: uuid.UUID) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    target = form.get("target", "")
    if target not in {"BOT_ACTIVE", "BOT_DRAFT_ONLY"}:
        raise HTTPException(status_code=422, detail="invalid_automation_default")
    async with get_session_factory()() as session:
        account = await session.get(models.PlatformAccount, account_id)
        if account is None or account.tenant_id not in principal.allowed_tenants:
            raise HTTPException(status_code=404, detail="account_not_found")
        account.automation_default = target
        await session.commit()
    return RedirectResponse("/admin/accounts", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/killswitch/toggle")
async def killswitch_toggle(request: Request) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    settings = get_settings()
    tenant_id = form.get("tenant_id", "")
    if tenant_id not in principal.allowed_tenants:
        raise HTTPException(status_code=403, detail="tenant_access_denied")
    scope = form.get("scope", "")
    if scope == "global":
        if not principal.is_superadmin:
            raise HTTPException(status_code=403, detail="superadmin_required")
        key = f"killswitch:global:{tenant_id}"
    elif scope == "account":
        account_id = form.get("account_id", "")
        try:
            parsed_account_id = uuid.UUID(account_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid_account_id") from exc
        async with get_session_factory()() as session:
            account = await session.get(models.PlatformAccount, parsed_account_id)
        if account is None or account.tenant_id != tenant_id:
            raise HTTPException(status_code=404, detail="account_not_found")
        key = f"killswitch:account:{tenant_id}:{account_id}"
    else:
        raise HTTPException(status_code=422, detail="invalid_killswitch_scope")
    redis = aioredis.from_url(settings.redis_url)
    try:
        if await redis.get(key) is None:
            await redis.set(key, "1")
        else:
            await redis.delete(key)
    finally:
        await redis.aclose()
    return RedirectResponse("/admin/accounts", status_code=status.HTTP_303_SEE_OTHER)
