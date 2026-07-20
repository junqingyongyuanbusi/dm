"""运营后台控制台：总览 / 对话收件箱 / 决策审核 / 知识库 / 投递监控 / 账号与急停。

与 admin.py 共享会话与 CSRF 机制；全部服务端渲染零 JS，复用 _page 外壳。
所有查询按 allowed_admin_tenants 过滤（与既有 admin 一致的租户边界）。
"""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import redis.asyncio as aioredis
from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import desc, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from social_reply.application.account_management.admin import (
    _CSRF_COOKIE,
    _csrf,
    _current_actor,
    _form,
    _input,
    _page,
    _pill,
    _require_csrf,
    _secure_cookie,
    html,
)
from social_reply.application.account_management.service import enable_xchat_for_account
from social_reply.application.reply_decision.persist import _idempotency_key
from social_reply.domain.automation.state_machine import AutomationStateEnum, can_transition
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.queue.dispatch import dispatch_actor
from social_reply.shared.config import get_settings

router = APIRouter(prefix="/admin", tags=["admin-console"])

_LOGIN = "/admin/login"


def _fmt(dt: datetime | None) -> str:
    return dt.strftime("%m-%d %H:%M") if dt else "—"


def _ensure_csrf(response: Response, request: Request, csrf: str) -> Response:
    if not request.cookies.get(_CSRF_COOKIE):
        response.set_cookie(
            _CSRF_COOKIE, csrf, httponly=False, samesite="strict", secure=_secure_cookie(request)
        )
    return response


def _tenants() -> frozenset[str]:
    return get_settings().allowed_admin_tenants


# ---------- 总览 ----------


@router.get("", response_class=HTMLResponse)
async def overview(request: Request) -> Response:
    if _current_actor(request) is None:
        return RedirectResponse(_LOGIN, status_code=status.HTTP_303_SEE_OTHER)
    tenants = _tenants()
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
    body = f"""<h1>总览</h1><p class="lede">自动回复运行状况与近 7 日决策分布。</p>{stats}
<div class="grid" style="grid-template-columns:1fr 1.4fr">
<section class="card"><h2>决策分布</h2><p class="hint">近 7 日各动作占比。</p>{bars}</section>
<section class="card"><h2>最近决策</h2><p class="hint">最新 8 条 AI 决策。</p><div class="tablewrap"><table><thead><tr><th>时间</th><th>动作</th><th>意图</th><th>回复预览</th><th></th></tr></thead><tbody>{recent_rows}</tbody></table></div></section>
</div>"""
    return HTMLResponse(_page("总览", body, active="overview"))


# ---------- 对话收件箱 ----------


@router.get("/conversations", response_class=HTMLResponse)
async def conversations_page(request: Request) -> Response:
    if _current_actor(request) is None:
        return RedirectResponse(_LOGIN, status_code=status.HTTP_303_SEE_OTHER)
    tenants = _tenants()
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
    return HTMLResponse(_page("对话", body, active="conversations"))


_TRANSITION_LABELS = {
    "HUMAN_ACTIVE": ("人工接管", "btn-danger"),
    "BOT_ACTIVE": ("恢复自动回复", ""),
    "BOT_DRAFT_ONLY": ("切为草稿模式", "btn-ghost"),
    "BOT_COOLDOWN": ("结束接管（冷却）", "btn-ghost"),
}


@router.get("/conversations/{conversation_id}", response_class=HTMLResponse)
async def conversation_detail(request: Request, conversation_id: uuid.UUID) -> Response:
    if _current_actor(request) is None:
        return RedirectResponse(_LOGIN, status_code=status.HTTP_303_SEE_OTHER)
    csrf = _csrf(request)
    async with get_session_factory()() as session:
        conv = await session.get(models.Conversation, conversation_id)
        if conv is None or conv.tenant_id not in _tenants():
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
    response = HTMLResponse(_page("对话详情", body, active="conversations"))
    return _ensure_csrf(response, request, csrf)


@router.post("/conversations/{conversation_id}/state")
async def flip_conversation_state(request: Request, conversation_id: uuid.UUID) -> Response:
    if _current_actor(request) is None:
        return RedirectResponse(_LOGIN, status_code=status.HTTP_303_SEE_OTHER)
    form = await _form(request)
    _require_csrf(request, form)
    target, expect = form.get("target", ""), form.get("expect", "")
    if target not in _TRANSITION_LABELS or expect not in AutomationStateEnum.__members__:
        raise HTTPException(status_code=422, detail="invalid_state_transition")
    if not can_transition(AutomationStateEnum(expect), AutomationStateEnum(target)):
        raise HTTPException(status_code=422, detail="transition_not_allowed")
    async with get_session_factory()() as session:
        conv = await session.get(models.Conversation, conversation_id)
        if conv is None or conv.tenant_id not in _tenants():
            raise HTTPException(status_code=404, detail="conversation_not_found")
        # CAS：仅当仍处于提交时看到的状态才翻转（防并发接管竞态）
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
    if _current_actor(request) is None:
        return RedirectResponse(_LOGIN, status_code=status.HTTP_303_SEE_OTHER)
    csrf = _csrf(request)
    tenants = _tenants()
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
    response = HTMLResponse(_page("决策", body, active="decisions"))
    return _ensure_csrf(response, request, csrf)


async def _load_draft(session, decision_id: uuid.UUID) -> models.ReplyDecision:
    decision = await session.get(models.ReplyDecision, decision_id)
    if decision is None or decision.tenant_id not in _tenants():
        raise HTTPException(status_code=404, detail="decision_not_found")
    if decision.action != "draft" or decision.outbox_id is not None:
        raise HTTPException(status_code=409, detail="decision_not_pending_draft")
    return decision


@router.post("/decisions/{decision_id}/approve")
async def approve_draft(request: Request, decision_id: uuid.UUID) -> Response:
    if _current_actor(request) is None:
        return RedirectResponse(_LOGIN, status_code=status.HTTP_303_SEE_OTHER)
    form = await _form(request)
    _require_csrf(request, form)
    async with get_session_factory()() as session:
        decision = await _load_draft(session, decision_id)
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
        kind = reply_target.get("kind", "dm")
        visibility = decision.reply_visibility or "public"
        # 目的地映射与 persist_decision 保持一致（DRY 的最小可行复制，含会话时限）
        if account.platform == "telegram":
            destination_type = "telegram_dm"
        elif account.platform == "x":
            destination_type = (
                "x_post_reply"
                if kind == "reply"
                else "x_chat_message"
                if kind == "x_chat"
                else "x_dm"
            )
        elif account.platform == "whatsapp":
            destination_type = "whatsapp_session_message"
        elif account.platform in {"facebook", "instagram"}:
            if visibility == "private" and kind == "comment":
                destination_type = "meta_private_reply"
                reply_target = {**reply_target, "kind": "private_reply"}
            elif kind == "comment":
                destination_type = "meta_public_comment"
            else:
                destination_type = (
                    "meta_messenger_dm" if account.platform == "facebook" else "meta_instagram_dm"
                )
        else:
            raise HTTPException(status_code=409, detail="unsupported_platform_for_approve")
        valid_until = None
        if destination_type in {
            "meta_messenger_dm",
            "meta_instagram_dm",
            "whatsapp_session_message",
        }:
            valid_until = (occurred_at or datetime.now(UTC)) + timedelta(hours=24)
        elif destination_type == "meta_private_reply":
            valid_until = datetime.now(UTC) + timedelta(days=7)
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
                        "approved_by": _current_actor(request),
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
                    actor=_current_actor(request) or "admin",
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
    if _current_actor(request) is None:
        return RedirectResponse(_LOGIN, status_code=status.HTTP_303_SEE_OTHER)
    form = await _form(request)
    _require_csrf(request, form)
    async with get_session_factory()() as session:
        decision = await _load_draft(session, decision_id)
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
}


@router.get("/knowledge", response_class=HTMLResponse)
async def knowledge_page(request: Request, notice: str = "") -> Response:
    if _current_actor(request) is None:
        return RedirectResponse(_LOGIN, status_code=status.HTTP_303_SEE_OTHER)
    csrf = _csrf(request)
    tenants = _tenants()
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
    if notice in _KB_BANNERS:
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
{_input("tenant_id", "Tenant")}{_input("question", "触发问题（用户会怎么问）")}
<label for="f-kb-reply">标准回复（命中后原文发送）</label><textarea id="f-kb-reply" name="reply" required></textarea>
{_input("category", "分类（可选）", required=False)}{_input("brand_id", "Brand（默认 default）", required=False)}
<button class="btn-block">添加并向量化</button></form></div></details>"""
    body = f"""<h1>知识库</h1><p class="lede">回复模板管理：命中即原文直答；下架条目不参与检索。</p>{banner}
{add_form}
<section class="card"><h2>模板列表</h2><p class="hint">共 {len(docs)} 条（最多显示 200）。</p><div class="tablewrap"><table><thead><tr><th>问题</th><th>回复</th><th>分类</th><th>状态</th><th>操作</th></tr></thead><tbody>{rows}</tbody></table></div></section>"""
    response = HTMLResponse(_page("知识库", body, active="knowledge"))
    return _ensure_csrf(response, request, csrf)


@router.post("/knowledge/add")
async def knowledge_add(request: Request) -> Response:
    if _current_actor(request) is None:
        return RedirectResponse(_LOGIN, status_code=status.HTTP_303_SEE_OTHER)
    form = await _form(request)
    _require_csrf(request, form)
    tenant_id = (form.get("tenant_id") or "").strip()
    if tenant_id not in _tenants():
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


@router.post("/knowledge/{doc_id}/status")
async def knowledge_toggle(request: Request, doc_id: uuid.UUID) -> Response:
    if _current_actor(request) is None:
        return RedirectResponse(_LOGIN, status_code=status.HTTP_303_SEE_OTHER)
    form = await _form(request)
    _require_csrf(request, form)
    async with get_session_factory()() as session:
        doc = await session.get(models.KnowledgeDocument, doc_id)
        if doc is None or doc.tenant_id not in _tenants():
            raise HTTPException(status_code=404, detail="knowledge_not_found")
        doc.status = "draft" if doc.status == "published" else "published"
        await session.commit()
    return RedirectResponse(
        "/admin/knowledge?notice=toggled", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/knowledge/{doc_id}/delete")
async def knowledge_delete(request: Request, doc_id: uuid.UUID) -> Response:
    if _current_actor(request) is None:
        return RedirectResponse(_LOGIN, status_code=status.HTTP_303_SEE_OTHER)
    form = await _form(request)
    _require_csrf(request, form)
    async with get_session_factory()() as session:
        doc = await session.get(models.KnowledgeDocument, doc_id)
        if doc is None or doc.tenant_id not in _tenants():
            raise HTTPException(status_code=404, detail="knowledge_not_found")
        await session.delete(doc)  # chunk 级联删除（FK ondelete=CASCADE）
        await session.commit()
    return RedirectResponse(
        "/admin/knowledge?notice=deleted", status_code=status.HTTP_303_SEE_OTHER
    )


# ---------- 投递监控 ----------


@router.get("/delivery", response_class=HTMLResponse)
async def delivery_page(request: Request) -> Response:
    if _current_actor(request) is None:
        return RedirectResponse(_LOGIN, status_code=status.HTTP_303_SEE_OTHER)
    csrf = _csrf(request)
    tenants = _tenants()
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
                    func.count(models.RawEvent.id),
                    func.max(models.RawEvent.received_at),
                )
                .outerjoin(
                    models.NormalizedEvent,
                    models.NormalizedEvent.raw_event_id == models.RawEvent.id,
                )
                .where(
                    models.RawEvent.received_at >= day_ago,
                    (
                        models.NormalizedEvent.tenant_id.in_(tenants)
                        | models.NormalizedEvent.id.is_(None)
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
    response = HTMLResponse(_page("投递", body, active="delivery"))
    return _ensure_csrf(response, request, csrf)


@router.post("/delivery/{outbox_id}/retry")
async def delivery_retry(request: Request, outbox_id: uuid.UUID) -> Response:
    if _current_actor(request) is None:
        return RedirectResponse(_LOGIN, status_code=status.HTTP_303_SEE_OTHER)
    form = await _form(request)
    _require_csrf(request, form)
    async with get_session_factory()() as session:
        row = await session.get(models.OutboxMessage, outbox_id)
        if row is None or row.tenant_id not in _tenants():
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
                actor=_current_actor(request) or "admin",
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
    if _current_actor(request) is None:
        return RedirectResponse(_LOGIN, status_code=status.HTTP_303_SEE_OTHER)
    csrf = _csrf(request)
    settings = get_settings()
    tenants = _tenants()
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
        global_flags = await redis.mget([f"killswitch:global:{tenant}" for tenant in tenants])
    finally:
        await redis.aclose()
    account_stopped = {
        str(account.id): account_flags[index] is not None for index, account in enumerate(accounts)
    }
    global_rows = "".join(
        f"<form class='inline' method='post' action='/admin/killswitch/toggle'>"
        f"<input type='hidden' name='csrf_token' value='{csrf}'>"
        f"<input type='hidden' name='scope' value='global'>"
        f"<input type='hidden' name='tenant_id' value='{html.escape(tenant)}'>"
        f"<button class='btn-sm {'btn-ghost' if global_flags[index] else 'btn-danger'}'>"
        f"{html.escape(tenant)}：{'解除急停' if global_flags[index] else '全局急停'}</button></form>"
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
        if a.platform == "x" and not (a.capability or {}).get("x_chat", False):
            xchat_form = f"""<form class="inline" method="post" action="/admin/accounts/{a.id}/xchat"><input type="hidden" name="csrf_token" value="{csrf}"><input type="password" name="xchat_pin" inputmode="numeric" pattern="[0-9]{{4}}" maxlength="4" placeholder="XChat PIN" required><button class="btn-sm btn-ghost">启用 XChat</button></form>"""
        account_rows += (
            f"<tr><td>{html.escape(a.platform)}</td><td>{html.escape(a.name)}</td>"
            f"<td>{_pill(a.status)}</td><td>{_pill(a.automation_default)}</td><td>{ks_pill}</td>"
            f"""<td><form class="inline" method="post" action="/admin/accounts/{a.id}/automation"><input type="hidden" name="csrf_token" value="{csrf}"><input type="hidden" name="target" value="{auto_target}"><button class="btn-sm btn-ghost">{auto_label}</button></form>
<form class="inline" method="post" action="/admin/killswitch/toggle"><input type="hidden" name="csrf_token" value="{csrf}"><input type="hidden" name="scope" value="account"><input type="hidden" name="account_id" value="{a.id}"><input type="hidden" name="tenant_id" value="{html.escape(a.tenant_id)}"><button class="btn-sm {ks_cls}">{ks_btn}</button></form>{xchat_form}</td></tr>"""
        )
    account_rows = account_rows or "<tr><td colspan='6' class='muted'>尚未连接账号</td></tr>"

    job_rows = (
        "".join(
            f"<tr><td><a href='/admin/jobs/{row.id}'><code>{str(row.id)[:8]}</code></a></td>"
            f"<td>{html.escape(row.platform)}</td><td>{_pill(row.status)}</td>"
            f"<td class='muted'>{html.escape(row.current_step)}</td>"
            f"<td class='muted'>{html.escape(row.last_error_code or '—')}</td></tr>"
            for row in jobs
        )
        or "<tr><td colspan='5' class='muted'>暂无任务</td></tr>"
    )
    common = (
        f'<input type="hidden" name="csrf_token" value="{csrf}">'
        + _input("tenant_id", "Tenant", required=True)
        + _input("brand_id", "Brand", required=True)
        + _input("name", "显示名称（可选）", required=False)
    )
    oauth_callback = f"{settings.public_base_url.rstrip('/')}/admin/oauth/x/callback"
    meta_callback = f"{settings.public_base_url.rstrip('/')}/admin/oauth/meta/callback"
    oauth_common = (
        f'<input type="hidden" name="csrf_token" value="{csrf}">'
        + _input("tenant_id", "Tenant", required=True)
        + _input("brand_id", "Brand", required=True)
    )
    connect_forms = f"""<details class="collapse"><summary>接入新平台账号</summary><div class="inner">
<p class="hint">提交后创建持久化任务；凭证只进入 Secret 存储，不写入任务 JSON。OAuth 卡片会跳转平台授权页自动换取凭证；手工卡片用于粘贴已有凭证。</p>
<div class="grid">
<form class="card" method="post" action="/admin/oauth/x/start"><h3>X · OAuth 一键授权（推荐）</h3>{oauth_common}{_input("xchat_pin", "XChat 4 位 PIN（可选，启用加密私信）", secret=True, required=False)}<p class="hint">跳转 X 授权页，用要接入的账号登录并授权，凭证自动换取入库。前提（一次性）：X 开发者后台该 App 开启 User authentication，回调 URL 登记 <code>{html.escape(oauth_callback)}</code>，App 类型选 Native App。</p><button class="btn-block">跳转 X 授权</button></form>
<form class="card" method="post" action="/admin/oauth/meta/start"><h3>Facebook / Instagram · OAuth 一键授权（推荐）</h3>{oauth_common}<label for="f-oauth-meta-platform">平台</label><select id="f-oauth-meta-platform" name="platform"><option value="facebook">Facebook Page</option><option value="instagram">Instagram（专业账号，经 Facebook 授权）</option></select><p class="hint">跳转 Meta 授权页，授权后自动列出你管理的 Page（多个时可选择），凭证自动换取入库。前提（一次性）：Meta App 添加 Facebook Login 产品，Valid OAuth Redirect URIs 登记 <code>{html.escape(meta_callback)}</code>；需已用手工表单接入过一次（创建 App 凭证记录）；App 未过审时仅 App 角色账号可授权。Instagram 需为专业账号并关联 Facebook Page。</p><button class="btn-block">跳转 Meta 授权</button></form>
<form class="card" method="post" action="/admin/connect/telegram"><h3>Telegram</h3>{common}{_input("token", "Bot Token", secret=True)}<p class="hint">Telegram 无 OAuth：在 Telegram 中找 @BotFather 发送 /newbot 创建机器人，把返回的 Token 粘贴到上方，提交后自动校验并注册 webhook。</p><button class="btn-block">连接 Telegram</button></form>
<form class="card" method="post" action="/admin/connect/meta"><h3>Facebook / Instagram</h3>{common}<label for="f-meta-platform">平台</label><select id="f-meta-platform" name="platform"><option>facebook</option><option>instagram</option></select>{_input("external_account_id", "Page / IG Account ID")}{_input("access_token", "Access Token", secret=True)}{_input("app_secret", "Meta App Secret", secret=True)}{_input("app_id", "Meta App ID", required=False)}{_input("app_public_id", "Existing App Public ID", required=False)}{_input("verify_token", "Webhook Verify Token", secret=True)}<button class="btn-block">连接 Meta</button></form>
<form class="card" method="post" action="/admin/connect/whatsapp"><h3>WhatsApp</h3>{common}{_input("external_account_id", "Phone Number ID")}{_input("access_token", "Access Token", secret=True)}{_input("app_secret", "Meta App Secret", secret=True)}{_input("app_id", "Meta App ID", required=False)}{_input("app_public_id", "Existing App Public ID", required=False)}{_input("verify_token", "Webhook Verify Token", secret=True)}<button class="btn-block">连接 WhatsApp</button></form>
<form class="card" method="post" action="/admin/connect/x"><h3>X</h3>{common}{_input("consumer_key", "Consumer Key", secret=True)}{_input("consumer_secret", "Consumer Secret", secret=True)}{_input("access_token", "Access Token", secret=True)}{_input("access_token_secret", "Access Token Secret", secret=True)}{_input("environment", "Account Activity Environment")}{_input("xchat_pin", "XChat 4 位 PIN（启用加密私信，建议填写）", secret=True, required=False)}<button class="btn-block">连接 X</button></form>
</div></div></details>"""
    body = f"""<h1>账号</h1><p class="lede">已接入账号的运行控制、急停开关与接入任务。</p>
{killswitch_card}
<section class="card"><h2>平台账号</h2><div class="tablewrap"><table><thead><tr><th>平台</th><th>名称</th><th>状态</th><th>新会话默认</th><th>急停</th><th>操作</th></tr></thead><tbody>{account_rows}</tbody></table></div></section>
<section class="card"><h2>Provisioning Jobs</h2><p class="hint">最近 20 条接入任务。</p><div class="tablewrap"><table><thead><tr><th>ID</th><th>平台</th><th>状态</th><th>步骤</th><th>错误</th></tr></thead><tbody>{job_rows}</tbody></table></div></section>
{connect_forms}"""
    response = HTMLResponse(_page("账号", body, active="accounts"))
    return _ensure_csrf(response, request, csrf)


@router.post("/accounts/{account_id}/xchat")
async def enable_account_xchat(request: Request, account_id: uuid.UUID) -> Response:
    if _current_actor(request) is None:
        return RedirectResponse(_LOGIN, status_code=status.HTTP_303_SEE_OTHER)
    form = await _form(request)
    _require_csrf(request, form)
    async with get_session_factory()() as session:
        account = await session.get(models.PlatformAccount, account_id)
    if account is None or account.tenant_id not in _tenants() or account.platform != "x":
        raise HTTPException(status_code=404, detail="x_account_not_found")
    pin = (form.get("xchat_pin") or "").strip()
    if len(pin) != 4 or not pin.isdigit():
        raise HTTPException(status_code=422, detail="invalid_xchat_pin")
    try:
        await enable_xchat_for_account(account_id=account_id, pin=pin)
    except Exception as exc:  # noqa: BLE001 - show a stable operator error, never echo PIN
        raise HTTPException(status_code=422, detail="xchat_unlock_failed") from exc
    return RedirectResponse("/admin/accounts", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/accounts/{account_id}/automation")
async def flip_account_automation(request: Request, account_id: uuid.UUID) -> Response:
    if _current_actor(request) is None:
        return RedirectResponse(_LOGIN, status_code=status.HTTP_303_SEE_OTHER)
    form = await _form(request)
    _require_csrf(request, form)
    target = form.get("target", "")
    if target not in {"BOT_ACTIVE", "BOT_DRAFT_ONLY"}:
        raise HTTPException(status_code=422, detail="invalid_automation_default")
    async with get_session_factory()() as session:
        account = await session.get(models.PlatformAccount, account_id)
        if account is None or account.tenant_id not in _tenants():
            raise HTTPException(status_code=404, detail="account_not_found")
        account.automation_default = target
        await session.commit()
    return RedirectResponse("/admin/accounts", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/killswitch/toggle")
async def killswitch_toggle(request: Request) -> Response:
    if _current_actor(request) is None:
        return RedirectResponse(_LOGIN, status_code=status.HTTP_303_SEE_OTHER)
    form = await _form(request)
    _require_csrf(request, form)
    settings = get_settings()
    tenant_id = form.get("tenant_id", "")
    if tenant_id not in _tenants():
        raise HTTPException(status_code=403, detail="tenant_access_denied")
    scope = form.get("scope", "")
    if scope == "global":
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
