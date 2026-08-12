"""运营后台控制台：总览 / 对话 / 决策 / 知识库 / 投递 / 账号与急停。

与 admin.py 共享服务端会话与 CSRF；全部查询和写操作按当前 Principal 租户范围过滤。
"""

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import NoReturn
from urllib.parse import urlencode

import redis.asyncio as aioredis
from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import and_, desc, func, or_, select, update

from social_reply.application.account_management.admin import (
    _csrf,
    _ensure_csrf,
    _form,
    _input,
    _page,
    _pill,
    _require_csrf,
    _web_principal,
    html,
    tenant_id_or_default,
)
from social_reply.application.account_management.auth import Principal
from social_reply.application.account_management.human_workflow import (
    HumanWorkflowConflict,
    HumanWorkflowError,
    claim_human_work_item,
    ensure_open_human_work_item,
    require_work_conversation_tenant,
    resolve_human_work_item,
    resume_bot,
    send_human_reply,
)
from social_reply.application.account_management.jobs import provisioning_job_is_in_flight
from social_reply.application.account_management.oauth.common import notice
from social_reply.application.account_management.service import enable_xchat_for_account
from social_reply.application.account_management.xchat_activation import XChatActivationError
from social_reply.application.knowledge.drafts import (
    build_knowledge_draft,
    existing_content_hashes,
    persist_knowledge_draft,
)
from social_reply.application.knowledge.importer import import_knowledge_rows
from social_reply.application.message_delivery.contracts import (
    build_direct_reply_destination,
)
from social_reply.application.message_delivery.intents import (
    OutboxActor,
    OutboxIdempotencyConflict,
    OutboxIntentError,
    OutboxOrigin,
    create_or_get_outbox_intent,
)
from social_reply.application.reply_decision.persona import (
    compile_voice_preferences,
    load_persona,
    parse_voice_preferences,
)
from social_reply.connectors.feishu.contracts import FEISHU_API_BASE_URL, FEISHU_GROUP_MODE
from social_reply.domain.automation.state_machine import (
    AutomationStateEnum,
    can_transition,
    flip_to_human_active,
)
from social_reply.domain.platform_accounts import capability_text_limit
from social_reply.domain.reply.guard import redact_pii
from social_reply.domain.reply.llm import LLMContext
from social_reply.domain.reply.openai_client import CONTRACT_PROMPT
from social_reply.domain.reply.voice import (
    VOICE_PREFERENCE_FIELDS,
    VoiceEmoji,
    VoiceEmpathy,
    VoiceLength,
    VoiceTone,
)
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.queue.dispatch import dispatch_actor
from social_reply.shared.config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin-console"])

_ADMIN_PLATFORMS = (
    "telegram",
    "facebook",
    "instagram",
    "whatsapp",
    "x",
    "feishu",
    "email",
)


def _fmt(dt: datetime | None) -> str:
    return dt.strftime("%m-%d %H:%M") if dt else "—"


def _fmt_iso_timestamp(value: object) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return "—"
    else:
        return "—"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


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
    field_id = f"f-tenant-{uuid.uuid4().hex[:6]}"
    return (
        f'<label for="{field_id}">Tenant</label>'
        f'<select id="{field_id}" name="tenant_id" required>{options}</select>'
    )


_REASON_LABELS = {
    "RISK_WORD": "高风险内容",
    "OPENAI": "模型主动转人工",
    "EMPTY_OR_NON_TEXT": "消息缺少文本",
    "INSUFFICIENT_KNOWLEDGE": "知识不足",
    "LLM_REFUSAL": "模型拒答",
    "LLM_SCHEMA_FAIL": "模型返回异常",
    "LLM_UNAVAILABLE": "模型故障",
    "GUARD_PII_LEAK": "PII Guard",
    "GUARD_TOO_LONG": "长度 Guard",
    "CAPABILITY_NOT_ALLOWED": "平台能力限制",
    "CAPABILITY_TEXT_TOO_LONG": "平台长度限制",
    "DELIVERY_WINDOW_EXPIRED": "发送窗口已关闭",
    "UNSUPPORTED_ATTACHMENT": "暂不支持的附件",
    "AMBIGUOUS_SEND": "发送结果不确定",
}


def _reason_label(code: str | None) -> str:
    if not code:
        return "—"
    return _REASON_LABELS.get(code, code)


def _target_label(target: dict | None) -> str:
    value = dict(target or {})
    kind = str(value.get("kind") or "dm")
    labels = {
        "dm": "私信会话",
        "x_chat": "X Chat 会话",
        "comment": "公开评论",
        "reply": "公开帖子回复",
        "session_message": "WhatsApp 会话",
    }
    return f"{labels.get(kind, kind)} · {json.dumps(value, ensure_ascii=False, sort_keys=True)}"


def _attachment_text(attachment: object) -> str:
    if not isinstance(attachment, dict):
        return "附件"
    media_type = str(
        attachment.get("type")
        or attachment.get("media_type")
        or attachment.get("mime_type")
        or "附件"
    )
    reference = str(attachment.get("url") or attachment.get("href") or attachment.get("id") or "")
    return f"{media_type}{f' · {reference}' if reference else ''}"


def _workflow_error(exc: HumanWorkflowError) -> HTTPException:
    detail = str(exc)
    if detail in {"human_work_item_not_found", "conversation_not_found"}:
        return HTTPException(status_code=404, detail=detail)
    if isinstance(exc, HumanWorkflowConflict):
        return HTTPException(status_code=409, detail=detail)
    return HTTPException(status_code=422, detail=detail)


def _expected_version(form: dict[str, str]) -> int:
    try:
        value = int(form.get("version", ""))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid_work_item_version") from exc
    if value < 1:
        raise HTTPException(status_code=422, detail="invalid_work_item_version")
    return value


def _selected(value: str, expected: str) -> str:
    return " selected" if value == expected else ""


_CHANNEL_FILTERS = {"all", "dm", "comment"}
_CONVERSATION_CHANNEL_LABELS = {
    "dm": "私信",
    "comment": "评论",
    "mention": "提及",
}


def _channel_label(channel_type: str) -> str:
    return _CONVERSATION_CHANNEL_LABELS.get(channel_type, channel_type)


def _channel_condition(channel: str):
    if channel == "dm":
        return models.Conversation.channel_type == "dm"
    if channel == "comment":
        return models.Conversation.channel_type.in_(("comment", "mention"))
    return None


def _reviewable_draft_condition():
    draft_text = func.btrim(
        func.coalesce(
            models.ReplyDecision.original_reply_text,
            models.ReplyDecision.reply_text,
            "",
        )
    )
    return and_(
        models.ReplyDecision.action == "draft",
        func.coalesce(models.ReplyDecision.review_action, "PENDING") == "PENDING",
        models.ReplyDecision.outbox_id.is_(None),
        models.ReplyDecision.message_id.is_not(None),
        func.length(draft_text) > 0,
    )


def _scope_inbox_statement(
    statement,
    *,
    tenant_column,
    tenants: frozenset[str],
    tenant_id: str,
    account_id: uuid.UUID | None,
    platform: str,
    channel: str,
):
    statement = statement.where(
        tenant_column.in_(tenants),
        models.Conversation.tenant_id == tenant_column,
        models.PlatformAccount.tenant_id == tenant_column,
    )
    if tenant_id:
        statement = statement.where(tenant_column == tenant_id)
    if account_id is not None:
        statement = statement.where(models.Conversation.platform_account_id == account_id)
    if platform:
        statement = statement.where(models.PlatformAccount.platform == platform)
    channel_filter = _channel_condition(channel)
    if channel_filter is not None:
        statement = statement.where(channel_filter)
    return statement


async def _load_inbox_summary(
    session,
    tenants: frozenset[str],
    *,
    tenant_id: str = "",
    account_id: uuid.UUID | None = None,
    platform: str = "",
    channel: str = "all",
) -> dict[str, tuple[int, datetime | None]]:
    scope = {
        "tenants": tenants,
        "tenant_id": tenant_id,
        "account_id": account_id,
        "platform": platform,
        "channel": channel,
    }
    human_statement = (
        select(func.count(), func.min(models.HumanWorkItem.created_at))
        .join(
            models.Conversation,
            models.Conversation.id == models.HumanWorkItem.conversation_id,
        )
        .join(
            models.PlatformAccount,
            models.PlatformAccount.id == models.Conversation.platform_account_id,
        )
        .where(models.HumanWorkItem.status.in_(("WAITING", "CLAIMED")))
    )
    human = (
        await session.execute(
            _scope_inbox_statement(
                human_statement,
                tenant_column=models.HumanWorkItem.tenant_id,
                **scope,
            )
        )
    ).one()
    draft_statement = (
        select(func.count(), func.min(models.ReplyDecision.created_at))
        .join(
            models.Conversation,
            models.Conversation.id == models.ReplyDecision.conversation_id,
        )
        .join(
            models.PlatformAccount,
            models.PlatformAccount.id == models.Conversation.platform_account_id,
        )
        .where(_reviewable_draft_condition())
    )
    drafts = (
        await session.execute(
            _scope_inbox_statement(
                draft_statement,
                tenant_column=models.ReplyDecision.tenant_id,
                **scope,
            )
        )
    ).one()
    delivery_statement = (
        select(func.count(), func.min(models.OutboxMessage.created_at))
        .join(
            models.Conversation,
            models.Conversation.id == models.OutboxMessage.conversation_id,
        )
        .join(
            models.PlatformAccount,
            models.PlatformAccount.id == models.Conversation.platform_account_id,
        )
        .where(models.OutboxMessage.status.in_(("FAILED", "NEEDS_REVIEW")))
    )
    delivery = (
        await session.execute(
            _scope_inbox_statement(
                delivery_statement,
                tenant_column=models.OutboxMessage.tenant_id,
                **scope,
            )
        )
    ).one()
    return {
        "human": (int(human[0]), human[1]),
        "drafts": (int(drafts[0]), drafts[1]),
        "delivery": (int(delivery[0]), delivery[1]),
    }


def _inbox_filter_form(
    *,
    queue: str,
    principal: Principal,
    accounts: list[models.PlatformAccount],
    tenant_id: str,
    account_id: str,
    platform: str,
    channel: str,
    queue_status: str,
    reason: str,
) -> str:
    tenant_options = '<option value="">全部 Tenant</option>' + "".join(
        f'<option value="{html.escape(value)}"{_selected(tenant_id, value)}>{html.escape(value)}</option>'
        for value in sorted(principal.allowed_tenants)
    )
    account_options = '<option value="">全部账号</option>' + "".join(
        f'<option value="{account.id}"{_selected(account_id, str(account.id))}>{html.escape(account.name)}</option>'
        for account in accounts
        if not tenant_id or account.tenant_id == tenant_id
    )
    platform_options = '<option value="">全部平台</option>' + "".join(
        f'<option value="{value}"{_selected(platform, value)}>{html.escape(value)}</option>'
        for value in _ADMIN_PLATFORMS
    )
    status_values = {
        "human": ("WAITING", "CLAIMED", "RESOLVED", "CANCELLED"),
        "drafts": ("PENDING", "ACCEPTED", "EDITED", "REJECTED"),
        "delivery": ("FAILED", "NEEDS_REVIEW"),
    }[queue]
    status_options = '<option value="">全部状态</option>' + "".join(
        f'<option value="{value}"{_selected(queue_status, value)}>{value}</option>'
        for value in status_values
    )
    return f"""<form class="filters" method="get" action="/admin/inbox">
<input type="hidden" name="queue" value="{queue}">
<input type="hidden" name="channel" value="{channel}">
<div><label for="inbox-tenant">Tenant</label><select id="inbox-tenant" name="tenant_id">{tenant_options}</select></div>
<div><label for="inbox-account">账号</label><select id="inbox-account" name="account_id">{account_options}</select></div>
<div><label for="inbox-platform">平台</label><select id="inbox-platform" name="platform">{platform_options}</select></div>
<div><label for="inbox-status">状态</label><select id="inbox-status" name="status">{status_options}</select></div>
<div><label for="inbox-reason">原因代码</label><input id="inbox-reason" name="reason" value="{html.escape(reason, quote=True)}" maxlength="128" placeholder="全部原因"></div>
<button>筛选</button></form>"""


def _conversation_filter_form(
    *,
    principal: Principal,
    accounts: list[models.PlatformAccount],
    tenant_id: str,
    account_id: str,
    platform: str,
    channel: str,
) -> str:
    tenant_options = '<option value="">全部 Tenant</option>' + "".join(
        f'<option value="{html.escape(value)}"{_selected(tenant_id, value)}>{html.escape(value)}</option>'
        for value in sorted(principal.allowed_tenants)
    )
    account_options = '<option value="">全部账号</option>' + "".join(
        f'<option value="{account.id}"{_selected(account_id, str(account.id))}>{html.escape(account.name)}</option>'
        for account in accounts
        if not tenant_id or account.tenant_id == tenant_id
    )
    platform_options = '<option value="">全部平台</option>' + "".join(
        f'<option value="{value}"{_selected(platform, value)}>{html.escape(value)}</option>'
        for value in _ADMIN_PLATFORMS
    )
    return f"""<form class="filters" method="get" action="/admin/conversations">
<input type="hidden" name="channel" value="{channel}">
<div><label for="conversation-tenant">Tenant</label><select id="conversation-tenant" name="tenant_id">{tenant_options}</select></div>
<div><label for="conversation-account">账号</label><select id="conversation-account" name="account_id">{account_options}</select></div>
<div><label for="conversation-platform">平台</label><select id="conversation-platform" name="platform">{platform_options}</select></div>
<button>筛选</button></form>"""


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


def _elapsed(started_at: datetime, finished_at: datetime | None) -> str:
    if finished_at is None:
        return "—"
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    if finished_at.tzinfo is None:
        finished_at = finished_at.replace(tzinfo=UTC)
    seconds = max(int((finished_at - started_at).total_seconds()), 0)
    if seconds < 60:
        return f"{seconds} 秒"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} 分钟"
    hours = minutes // 60
    return f"{hours} 小时 {minutes % 60} 分钟"


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
        _HealthMetric("ingestion", "入站恢复", *raw_row, "/admin/health#ingress"),
        _HealthMetric("decisions", "决策任务", *decision_row, "/admin/health#decisions"),
        _HealthMetric("delivery", "消息投递", *outbox_row, "/admin/inbox?queue=delivery"),
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


# ---------- Unified inbox ----------


def _draft_review_card(
    *,
    decision: models.ReplyDecision,
    conversation: models.Conversation,
    display_name: str,
    platform: str,
    channel_type: str,
    account_name: str,
    csrf: str,
    now: datetime,
) -> str:
    review_action = decision.review_action or "PENDING"
    original_text = decision.original_reply_text or decision.reply_text or ""
    heading = (
        f'<section class="card"><h3>{html.escape(display_name)} · '
        f"{html.escape(platform)} · {html.escape(_channel_label(channel_type))}</h3>"
        f'<p class="hint">{html.escape(account_name)} · {_pill(review_action)} · '
        f"等待 {_health_age(now, decision.created_at)} · "
        f'<a href="/admin/conversations/{conversation.id}">查看上下文</a></p>'
    )
    if review_action == "PENDING":
        controls = f"""<form method="post" action="/admin/decisions/{decision.id}/approve"><input type="hidden" name="csrf_token" value="{csrf}"><label for="draft-{decision.id}">回复内容</label><textarea id="draft-{decision.id}" name="final_reply_text" required maxlength="10000">{html.escape(original_text)}</textarea><button class="btn-sm">发送</button></form>
<form method="post" action="/admin/decisions/{decision.id}/discard"><input type="hidden" name="csrf_token" value="{csrf}"><label for="reject-{decision.id}">拒绝原因</label><input id="reject-{decision.id}" name="review_reason" maxlength="500" required><button class="btn-sm btn-ghost">拒绝</button></form>"""
        return f"{heading}{controls}</section>"

    final_text = decision.final_reply_text or "—"
    review_reason = decision.review_reason or "—"
    reviewed_by = decision.reviewed_by or "—"
    return f"""{heading}<dl class="channel-meta"><dt>AI 原始草稿</dt><dd>{html.escape(original_text or "—")}</dd>
<dt>最终回复</dt><dd>{html.escape(final_text)}</dd><dt>审核人</dt><dd>{html.escape(reviewed_by)}</dd>
<dt>审核时间</dt><dd>{_fmt(decision.reviewed_at)}</dd><dt>审核耗时</dt><dd>{_elapsed(decision.created_at, decision.reviewed_at)}</dd>
<dt>审核原因</dt><dd>{html.escape(review_reason)}</dd></dl></section>"""


@router.get("/inbox/counts", response_class=JSONResponse)
async def inbox_counts(request: Request) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    tenant_id = request.query_params.get("tenant_id", "").strip()
    if tenant_id:
        principal.require_tenant(tenant_id)
    account_id = request.query_params.get("account_id", "").strip()
    account_uuid: uuid.UUID | None = None
    if account_id:
        try:
            account_uuid = uuid.UUID(account_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid_account_filter") from exc
    platform = request.query_params.get("platform", "").strip()
    if platform and platform not in _ADMIN_PLATFORMS:
        raise HTTPException(status_code=422, detail="invalid_platform_filter")
    channel = request.query_params.get("channel", "all").strip()
    if channel not in _CHANNEL_FILTERS:
        raise HTTPException(status_code=422, detail="invalid_channel_filter")
    async with get_session_factory()() as session:
        if account_uuid is not None:
            account_exists = await session.scalar(
                select(func.count())
                .select_from(models.PlatformAccount)
                .where(
                    models.PlatformAccount.id == account_uuid,
                    models.PlatformAccount.tenant_id.in_(principal.allowed_tenants),
                )
            )
            if not account_exists:
                raise HTTPException(status_code=404, detail="account_not_found")
        summary = await _load_inbox_summary(
            session,
            principal.allowed_tenants,
            tenant_id=tenant_id,
            account_id=account_uuid,
            platform=platform,
            channel=channel,
        )
    return JSONResponse({key: value[0] for key, value in summary.items()})


@router.get("/inbox", response_class=HTMLResponse)
async def inbox_page(request: Request) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    csrf = _csrf(request)
    tenants = principal.allowed_tenants
    queue = request.query_params.get("queue", "human")
    if queue not in {"human", "drafts", "delivery"}:
        queue = "human"
    tenant_id = request.query_params.get("tenant_id", "").strip()
    if tenant_id:
        principal.require_tenant(tenant_id)
    account_id = request.query_params.get("account_id", "").strip()
    account_uuid: uuid.UUID | None = None
    if account_id:
        try:
            account_uuid = uuid.UUID(account_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid_account_filter") from exc
    platform = request.query_params.get("platform", "").strip()
    if platform and platform not in _ADMIN_PLATFORMS:
        raise HTTPException(status_code=422, detail="invalid_platform_filter")
    channel = request.query_params.get("channel", "all").strip()
    if channel not in _CHANNEL_FILTERS:
        raise HTTPException(status_code=422, detail="invalid_channel_filter")
    queue_status = request.query_params.get("status", "").strip()
    valid_statuses = {
        "human": {"WAITING", "CLAIMED", "RESOLVED", "CANCELLED"},
        "drafts": {"PENDING", "ACCEPTED", "EDITED", "REJECTED"},
        "delivery": {"FAILED", "NEEDS_REVIEW"},
    }[queue]
    if queue_status and queue_status not in valid_statuses:
        raise HTTPException(status_code=422, detail="invalid_inbox_status_filter")
    reason = request.query_params.get("reason", "").strip()
    if len(reason) > 128:
        raise HTTPException(status_code=422, detail="invalid_reason_filter")

    async with get_session_factory()() as session:
        accounts = list(
            (
                await session.execute(
                    select(models.PlatformAccount)
                    .where(models.PlatformAccount.tenant_id.in_(tenants))
                    .order_by(models.PlatformAccount.name)
                )
            ).scalars()
        )
        if account_uuid is not None and all(account.id != account_uuid for account in accounts):
            raise HTTPException(status_code=404, detail="account_not_found")
        summary = await _load_inbox_summary(
            session,
            tenants,
            tenant_id=tenant_id,
            account_id=account_uuid,
            platform=platform,
            channel=channel,
        )
        now = datetime.now(UTC)

        if queue == "human":
            statement = (
                select(
                    models.HumanWorkItem,
                    models.Conversation,
                    models.Contact.display_name,
                    models.Contact.external_user_id,
                    models.PlatformAccount.name.label("account_name"),
                    models.PlatformAccount.platform,
                    models.AutomationState.state,
                )
                .join(
                    models.Conversation,
                    models.Conversation.id == models.HumanWorkItem.conversation_id,
                )
                .join(models.Contact, models.Contact.id == models.Conversation.contact_id)
                .join(
                    models.PlatformAccount,
                    models.PlatformAccount.id == models.Conversation.platform_account_id,
                )
                .join(
                    models.AutomationState,
                    models.AutomationState.conversation_id == models.Conversation.id,
                    isouter=True,
                )
                .where(
                    models.HumanWorkItem.tenant_id.in_(tenants),
                    models.Conversation.tenant_id == models.HumanWorkItem.tenant_id,
                    models.PlatformAccount.tenant_id == models.HumanWorkItem.tenant_id,
                    models.Contact.tenant_id == models.HumanWorkItem.tenant_id,
                )
            )
            if queue_status:
                statement = statement.where(models.HumanWorkItem.status == queue_status)
            else:
                statement = statement.where(models.HumanWorkItem.status.in_(("WAITING", "CLAIMED")))
            if reason:
                statement = statement.where(models.HumanWorkItem.reason_code == reason)
            if tenant_id:
                statement = statement.where(models.HumanWorkItem.tenant_id == tenant_id)
            if account_uuid:
                statement = statement.where(models.Conversation.platform_account_id == account_uuid)
            if platform:
                statement = statement.where(models.PlatformAccount.platform == platform)
            channel_filter = _channel_condition(channel)
            if channel_filter is not None:
                statement = statement.where(channel_filter)
            items = (
                await session.execute(
                    statement.order_by(models.HumanWorkItem.created_at).limit(100)
                )
            ).all()
        elif queue == "drafts":
            statement = (
                select(
                    models.ReplyDecision,
                    models.Conversation,
                    models.Contact.display_name,
                    models.Contact.external_user_id,
                    models.PlatformAccount.name.label("account_name"),
                    models.PlatformAccount.platform,
                )
                .join(
                    models.Conversation,
                    models.Conversation.id == models.ReplyDecision.conversation_id,
                )
                .join(models.Contact, models.Contact.id == models.Conversation.contact_id)
                .join(
                    models.PlatformAccount,
                    models.PlatformAccount.id == models.Conversation.platform_account_id,
                )
                .where(
                    models.ReplyDecision.tenant_id.in_(tenants),
                    models.ReplyDecision.action == "draft",
                    models.Conversation.tenant_id == models.ReplyDecision.tenant_id,
                    models.PlatformAccount.tenant_id == models.ReplyDecision.tenant_id,
                    models.Contact.tenant_id == models.ReplyDecision.tenant_id,
                )
            )
            if queue_status and queue_status != "PENDING":
                statement = statement.where(models.ReplyDecision.review_action == queue_status)
            else:
                statement = statement.where(_reviewable_draft_condition())
            if reason:
                statement = statement.where(models.ReplyDecision.reason_codes.contains([reason]))
            if tenant_id:
                statement = statement.where(models.ReplyDecision.tenant_id == tenant_id)
            if account_uuid:
                statement = statement.where(models.Conversation.platform_account_id == account_uuid)
            if platform:
                statement = statement.where(models.PlatformAccount.platform == platform)
            channel_filter = _channel_condition(channel)
            if channel_filter is not None:
                statement = statement.where(channel_filter)
            items = (
                await session.execute(
                    statement.order_by(models.ReplyDecision.created_at).limit(100)
                )
            ).all()
        else:
            statement = (
                select(
                    models.OutboxMessage,
                    models.Conversation,
                    models.Contact.display_name,
                    models.Contact.external_user_id,
                    models.PlatformAccount.name.label("account_name"),
                    models.PlatformAccount.platform,
                )
                .join(
                    models.Conversation,
                    models.Conversation.id == models.OutboxMessage.conversation_id,
                )
                .join(models.Contact, models.Contact.id == models.Conversation.contact_id)
                .join(
                    models.PlatformAccount,
                    models.PlatformAccount.id == models.Conversation.platform_account_id,
                )
                .where(
                    models.OutboxMessage.tenant_id.in_(tenants),
                    models.Conversation.tenant_id == models.OutboxMessage.tenant_id,
                    models.PlatformAccount.tenant_id == models.OutboxMessage.tenant_id,
                    models.Contact.tenant_id == models.OutboxMessage.tenant_id,
                    models.OutboxMessage.status.in_(
                        (queue_status,) if queue_status else ("FAILED", "NEEDS_REVIEW")
                    ),
                )
            )
            if reason:
                statement = statement.where(models.OutboxMessage.last_error_code == reason)
            if tenant_id:
                statement = statement.where(models.OutboxMessage.tenant_id == tenant_id)
            if account_uuid:
                statement = statement.where(
                    models.OutboxMessage.platform_account_id == account_uuid
                )
            if platform:
                statement = statement.where(models.PlatformAccount.platform == platform)
            channel_filter = _channel_condition(channel)
            if channel_filter is not None:
                statement = statement.where(channel_filter)
            items = (
                await session.execute(
                    statement.order_by(models.OutboxMessage.created_at).limit(100)
                )
            ).all()

    shared_query = {
        key: value
        for key, value in (
            ("tenant_id", tenant_id),
            ("account_id", account_id),
            ("platform", platform),
            ("channel", channel if channel != "all" else ""),
        )
        if value
    }
    queue_tabs = (
        '<div class="queue-tabs">'
        + "".join(
            f'<a class="queue-tab{" active" if queue == key else ""}" href="/admin/inbox?{urlencode({"queue": key, **shared_query})}">'
            f"<strong>{summary[key][0]}</strong><span>{label} · 最老 {_health_age(now, summary[key][1])}</span></a>"
            for key, label in (
                ("human", "待人工"),
                ("drafts", "待审核"),
                ("delivery", "投递异常"),
            )
        )
        + "</div>"
    )
    channel_query = {
        key: value
        for key, value in (
            ("queue", queue),
            ("tenant_id", tenant_id),
            ("account_id", account_id),
            ("platform", platform),
            ("status", queue_status),
            ("reason", reason),
        )
        if value
    }
    channel_tabs = (
        '<div class="chips" aria-label="互动类型">'
        + "".join(
            f'<a class="chip{" active" if channel == value else ""}" href="/admin/inbox?{urlencode({**channel_query, "channel": value})}">{label}</a>'
            for value, label in (("all", "全部"), ("dm", "私信"), ("comment", "评论与提及"))
        )
        + "</div>"
    )
    filters = _inbox_filter_form(
        queue=queue,
        principal=principal,
        accounts=accounts,
        tenant_id=tenant_id,
        account_id=account_id,
        platform=platform,
        channel=channel,
        queue_status=queue_status,
        reason=reason,
    )

    if queue == "human":
        rows = (
            "".join(
                f"<tr><td class='muted'>{_health_age(now, work.created_at)}</td>"
                f"<td><a href='/admin/conversations/{conv.id}'>{html.escape(display or external or '匿名用户')}</a></td>"
                f"<td>{html.escape(platform_name)} · {html.escape(_channel_label(conv.channel_type))}<br><span class='muted'>{html.escape(account_name)}</span></td>"
                f"<td>{_pill(work.status)}</td><td>{html.escape(_reason_label(work.reason_code))}</td>"
                f"<td class='muted'>{html.escape(work.assigned_actor or str(work.assigned_user_id or '未认领'))}</td>"
                "<td>"
                + (
                    f'<form class="inline" method="post" action="/admin/work-items/{work.id}/claim"><input type="hidden" name="csrf_token" value="{csrf}"><input type="hidden" name="version" value="{work.version}"><button class="btn-sm">认领并接管</button></form>'
                    if work.status == "WAITING"
                    else ""
                )
                + f"<a href='/admin/conversations/{conv.id}'>打开</a>"
                + "</td></tr>"
                for work, conv, display, external, account_name, platform_name, _automation in items
            )
            or "<tr><td colspan='7' class='muted'>当前没有匹配的人工工作项</td></tr>"
        )
        content = f"<section class='card'><div class='tablewrap'><table><thead><tr><th>等待</th><th>联系人</th><th>渠道</th><th>状态</th><th>原因</th><th>负责人</th><th></th></tr></thead><tbody>{rows}</tbody></table></div></section>"
    elif queue == "drafts":
        cards = (
            "".join(
                _draft_review_card(
                    decision=decision,
                    conversation=conv,
                    display_name=display or external or "匿名用户",
                    platform=platform_name,
                    channel_type=conv.channel_type,
                    account_name=account_name,
                    csrf=csrf,
                    now=now,
                )
                for decision, conv, display, external, account_name, platform_name in items
            )
            or "<section class='card'><p class='muted'>当前没有匹配的待审核草稿</p></section>"
        )
        content = cards
    else:
        rows = (
            "".join(
                f"<tr><td class='muted'>{_health_age(now, outbox.created_at)}</td>"
                f"<td><a href='/admin/conversations/{conv.id}'>{html.escape(display or external or '匿名用户')}</a></td>"
                f"<td>{html.escape(platform_name)} · {html.escape(_channel_label(conv.channel_type))}<br><span class='muted'>{html.escape(account_name)}</span></td>"
                f"<td>{_pill(outbox.status)}</td><td>{html.escape(_reason_label(outbox.last_error_code))}</td>"
                f"<td class='muted'>{html.escape(outbox.last_error_message or '—')}</td>"
                + (
                    f'<td><form class="inline" method="post" action="/admin/delivery/{outbox.id}/retry"><input type="hidden" name="csrf_token" value="{csrf}"><button class="btn-sm btn-ghost">重试</button></form></td>'
                    if outbox.status == "FAILED"
                    else "<td><span class='muted'>需核实平台结果</span></td>"
                )
                + "</tr>"
                for outbox, conv, display, external, account_name, platform_name in items
            )
            or "<tr><td colspan='7' class='muted'>当前没有匹配的投递异常</td></tr>"
        )
        content = f"<section class='card'><div class='tablewrap'><table><thead><tr><th>等待</th><th>联系人</th><th>渠道</th><th>状态</th><th>错误</th><th>详情</th><th></th></tr></thead><tbody>{rows}</tbody></table></div></section>"

    body = f"""<h1>收件箱</h1><p class="lede">人工处理、草稿审核与投递异常的统一工作队列。</p>{queue_tabs}{channel_tabs}
<section class="card">{filters}</section>{content}"""
    response = HTMLResponse(
        _page(
            "收件箱",
            body,
            active="inbox",
            refresh_seconds=0 if queue == "drafts" else 20,
            show_users=principal.is_superadmin,
        )
    )
    return _ensure_csrf(response, request, csrf)


# ---------- Conversations ----------


@router.get("/conversations", response_class=HTMLResponse)
async def conversations_page(request: Request) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    tenants = principal.allowed_tenants
    tenant_id = request.query_params.get("tenant_id", "").strip()
    if tenant_id:
        principal.require_tenant(tenant_id)
    account_id = request.query_params.get("account_id", "").strip()
    account_uuid: uuid.UUID | None = None
    if account_id:
        try:
            account_uuid = uuid.UUID(account_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid_account_filter") from exc
    platform = request.query_params.get("platform", "").strip()
    if platform and platform not in _ADMIN_PLATFORMS:
        raise HTTPException(status_code=422, detail="invalid_platform_filter")
    channel = request.query_params.get("channel", "all").strip()
    if channel not in _CHANNEL_FILTERS:
        raise HTTPException(status_code=422, detail="invalid_channel_filter")
    last_msg = (
        select(
            models.Message.conversation_id.label("cid"),
            func.max(models.Message.created_at).label("last_at"),
        )
        .group_by(models.Message.conversation_id)
        .subquery()
    )
    async with get_session_factory()() as session:
        accounts = list(
            (
                await session.execute(
                    select(models.PlatformAccount)
                    .where(models.PlatformAccount.tenant_id.in_(tenants))
                    .order_by(models.PlatformAccount.name)
                )
            ).scalars()
        )
        if account_uuid is not None and all(account.id != account_uuid for account in accounts):
            raise HTTPException(status_code=404, detail="account_not_found")
        statement = (
            select(
                models.Conversation,
                models.Contact.display_name,
                models.Contact.external_user_id,
                models.PlatformAccount.name.label("account_name"),
                last_msg.c.last_at,
            )
            .join(models.Contact, models.Conversation.contact_id == models.Contact.id)
            .join(
                models.PlatformAccount,
                models.Conversation.platform_account_id == models.PlatformAccount.id,
            )
            .join(last_msg, last_msg.c.cid == models.Conversation.id, isouter=True)
            .where(
                models.Conversation.tenant_id.in_(tenants),
                models.Contact.tenant_id == models.Conversation.tenant_id,
                models.PlatformAccount.tenant_id == models.Conversation.tenant_id,
            )
        )
        if tenant_id:
            statement = statement.where(models.Conversation.tenant_id == tenant_id)
        if account_uuid:
            statement = statement.where(models.Conversation.platform_account_id == account_uuid)
        if platform:
            statement = statement.where(models.PlatformAccount.platform == platform)
        channel_filter = _channel_condition(channel)
        if channel_filter is not None:
            statement = statement.where(channel_filter)
        rows = (
            await session.execute(
                statement.order_by(
                    desc(func.coalesce(last_msg.c.last_at, models.Conversation.created_at))
                ).limit(50)
            )
        ).all()
    shared_query = {
        key: value
        for key, value in (
            ("tenant_id", tenant_id),
            ("account_id", account_id),
            ("platform", platform),
        )
        if value
    }
    channel_tabs = (
        '<div class="chips">'
        + "".join(
            f'<a class="chip{" active" if channel == value else ""}" href="/admin/conversations?{urlencode({"channel": value, **shared_query})}">{label}</a>'
            for value, label in (("all", "全部"), ("dm", "私信"), ("comment", "评论与提及"))
        )
        + "</div>"
    )
    filters = _conversation_filter_form(
        principal=principal,
        accounts=accounts,
        tenant_id=tenant_id,
        account_id=account_id,
        platform=platform,
        channel=channel,
    )
    trs = (
        "".join(
            f"<tr><td>{html.escape(conv.platform)} · {html.escape(_channel_label(conv.channel_type))}</td>"
            f"<td><a href='/admin/conversations/{conv.id}'>{html.escape(display or external or '匿名用户')}</a></td>"
            f"<td class='muted'>{html.escape(account_name)}</td>"
            f"<td class='muted'>{_fmt(last_at or conv.created_at)}</td></tr>"
            for conv, display, external, account_name, last_at in rows
        )
        or "<tr><td colspan='4' class='muted'>暂无对话</td></tr>"
    )
    body = f"""<h1>对话</h1><p class="lede">按渠道浏览最近活跃的客户对话。</p>{channel_tabs}
<section class="card">{filters}</section>
<section class="card"><div class="tablewrap"><table><thead><tr><th>渠道</th><th>联系人</th><th>账号</th><th>最后活跃</th></tr></thead><tbody>{trs}</tbody></table></div></section>"""
    return HTMLResponse(
        _page("对话", body, active="conversations", show_users=principal.is_superadmin)
    )


_TRANSITION_LABELS = {
    "HUMAN_ACTIVE": ("人工接管", "btn-danger"),
    "BOT_ACTIVE": ("恢复自动回复", ""),
    "BOT_DRAFT_ONLY": ("切为草稿模式", "btn-ghost"),
    "BOT_COOLDOWN": ("结束接管（冷却）", "btn-ghost"),
}


def _fail_conversation_detail_scope(
    *,
    conversation_id: uuid.UUID,
    relation: str,
    related_id: uuid.UUID | None,
) -> NoReturn:
    logger.warning(
        "conversation detail scope mismatch conversation_id=%s relation=%s related_id=%s",
        conversation_id,
        relation,
        related_id,
    )
    raise HTTPException(status_code=404, detail="conversation_not_found")


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
        contact = (
            await session.execute(
                select(models.Contact).where(
                    models.Contact.id == conv.contact_id,
                    models.Contact.tenant_id == conv.tenant_id,
                    models.Contact.platform_account_id == conv.platform_account_id,
                )
            )
        ).scalar_one_or_none()
        if contact is None:
            _fail_conversation_detail_scope(
                conversation_id=conversation_id,
                relation="contact",
                related_id=conv.contact_id,
            )
        account = (
            await session.execute(
                select(models.PlatformAccount).where(
                    models.PlatformAccount.id == conv.platform_account_id,
                    models.PlatformAccount.tenant_id == conv.tenant_id,
                )
            )
        ).scalar_one_or_none()
        if account is None:
            _fail_conversation_detail_scope(
                conversation_id=conversation_id,
                relation="platform_account",
                related_id=conv.platform_account_id,
            )
        state_row = (
            await session.execute(
                select(models.AutomationState).where(
                    models.AutomationState.conversation_id == conversation_id
                )
            )
        ).scalar_one_or_none()
        message_source_scope_mismatch = await session.scalar(
            select(models.Message.id)
            .join(
                models.OutboxMessage,
                models.Message.source_outbox_id == models.OutboxMessage.id,
            )
            .where(
                models.Message.conversation_id == conversation_id,
                or_(
                    models.OutboxMessage.tenant_id != conv.tenant_id,
                    models.OutboxMessage.conversation_id != conversation_id,
                    models.OutboxMessage.platform_account_id != conv.platform_account_id,
                ),
            )
            .limit(1)
        )
        if message_source_scope_mismatch is not None:
            _fail_conversation_detail_scope(
                conversation_id=conversation_id,
                relation="message_source_outbox",
                related_id=message_source_scope_mismatch,
            )
        newest_messages = (
            (
                await session.execute(
                    select(models.Message)
                    .where(models.Message.conversation_id == conversation_id)
                    .order_by(desc(models.Message.history_seq))
                    .limit(200)
                )
            )
            .scalars()
            .all()
        )
        msgs = list(reversed(newest_messages))
        decision_scope_mismatch = await session.scalar(
            select(models.ReplyDecision.id)
            .where(
                models.ReplyDecision.conversation_id == conversation_id,
                models.ReplyDecision.tenant_id != conv.tenant_id,
            )
            .limit(1)
        )
        if decision_scope_mismatch is not None:
            _fail_conversation_detail_scope(
                conversation_id=conversation_id,
                relation="reply_decision",
                related_id=decision_scope_mismatch,
            )
        decisions = (
            (
                await session.execute(
                    select(models.ReplyDecision)
                    .where(
                        models.ReplyDecision.conversation_id == conversation_id,
                        models.ReplyDecision.tenant_id == conv.tenant_id,
                    )
                    .order_by(desc(models.ReplyDecision.created_at))
                    .limit(10)
                )
            )
            .scalars()
            .all()
        )
        work_item_scope_mismatch = await session.scalar(
            select(models.HumanWorkItem.id)
            .where(
                models.HumanWorkItem.conversation_id == conversation_id,
                models.HumanWorkItem.tenant_id != conv.tenant_id,
            )
            .limit(1)
        )
        if work_item_scope_mismatch is not None:
            _fail_conversation_detail_scope(
                conversation_id=conversation_id,
                relation="human_work_item",
                related_id=work_item_scope_mismatch,
            )
        work_item = (
            (
                await session.execute(
                    select(models.HumanWorkItem)
                    .where(
                        models.HumanWorkItem.conversation_id == conversation_id,
                        models.HumanWorkItem.tenant_id == conv.tenant_id,
                    )
                    .order_by(desc(models.HumanWorkItem.created_at))
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        if work_item is not None:
            try:
                require_work_conversation_tenant(work_item, conversation_tenant_id=conv.tenant_id)
            except HumanWorkflowError as exc:
                raise _workflow_error(exc) from exc
        outbox_scope_mismatch = await session.scalar(
            select(models.OutboxMessage.id)
            .where(
                models.OutboxMessage.conversation_id == conversation_id,
                or_(
                    models.OutboxMessage.tenant_id != conv.tenant_id,
                    models.OutboxMessage.platform_account_id != conv.platform_account_id,
                ),
            )
            .limit(1)
        )
        if outbox_scope_mismatch is not None:
            _fail_conversation_detail_scope(
                conversation_id=conversation_id,
                relation="outbox_message",
                related_id=outbox_scope_mismatch,
            )
        outboxes = (
            (
                await session.execute(
                    select(models.OutboxMessage)
                    .where(
                        models.OutboxMessage.conversation_id == conversation_id,
                        models.OutboxMessage.tenant_id == conv.tenant_id,
                        models.OutboxMessage.platform_account_id == conv.platform_account_id,
                    )
                    .order_by(desc(models.OutboxMessage.created_at))
                    .limit(20)
                )
            )
            .scalars()
            .all()
        )
        audit_subjects = [
            and_(
                models.AuditLog.subject_type == "conversation",
                models.AuditLog.subject_id == str(conversation_id),
            )
        ]
        if work_item is not None:
            audit_subjects.append(
                and_(
                    models.AuditLog.subject_type == "human_work_item",
                    models.AuditLog.subject_id == str(work_item.id),
                )
            )
        if decisions:
            audit_subjects.append(
                and_(
                    models.AuditLog.subject_type == "reply_decision",
                    models.AuditLog.subject_id.in_(
                        tuple(str(decision.id) for decision in decisions)
                    ),
                )
            )
        if outboxes:
            audit_subjects.append(
                and_(
                    models.AuditLog.subject_type == "outbox",
                    models.AuditLog.subject_id.in_(tuple(str(outbox.id) for outbox in outboxes)),
                )
            )
        audit_scope_mismatch = await session.scalar(
            select(models.AuditLog.id)
            .where(
                models.AuditLog.tenant_id != conv.tenant_id,
                or_(*audit_subjects),
            )
            .limit(1)
        )
        if audit_scope_mismatch is not None:
            _fail_conversation_detail_scope(
                conversation_id=conversation_id,
                relation="audit_log",
                related_id=audit_scope_mismatch,
            )
        audit_logs = (
            (
                await session.execute(
                    select(models.AuditLog)
                    .where(
                        models.AuditLog.tenant_id == conv.tenant_id,
                        or_(*audit_subjects),
                    )
                    .order_by(desc(models.AuditLog.created_at))
                    .limit(30)
                )
            )
            .scalars()
            .all()
        )
    cur_state = state_row.state if state_row else "BOT_DRAFT_ONLY"
    bubbles = (
        "".join(
            f"<div class='msg {'in' if m.direction == 'inbound' else 'out'}'>"
            f"{html.escape(m.text or '（非文本消息）')}"
            + "".join(
                f"<div class='target-choice'>{html.escape(_attachment_text(attachment))}</div>"
                for attachment in (m.attachments or [])
            )
            + f"<div class='meta'>{'客户' if m.sender_type == 'contact' else '人工客服' if m.sender_type == 'agent' else '机器人'} · {_fmt(m.occurred_at or m.created_at)}</div></div>"
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
        if cur is not None
        and can_transition(cur, AutomationStateEnum(dst))
        and get_settings().automation_default_allowed(account.platform, dst)
        and (dst == "HUMAN_ACTIVE" or work_item is None)
    )
    decision_rows = (
        "".join(
            f"<tr><td class='muted'>{_fmt(d.created_at)}</td><td>{_pill(d.action)}</td>"
            f"<td class='muted'>{html.escape(d.intent or '—')}</td>"
            f"<td>{html.escape((d.final_reply_text or d.original_reply_text or d.reply_text or '—')[:60])}</td>"
            f"<td>{html.escape('、'.join(_reason_label(str(code)) for code in (d.reason_codes or [])) or '—')}</td>"
            f"<td class='muted'>{d.confidence if d.confidence is not None else '—'}</td></tr>"
            for d in decisions
        )
        or "<tr><td colspan='6' class='muted'>暂无决策</td></tr>"
    )
    who = html.escape(
        (contact.display_name if contact else None)
        or (contact.external_user_id if contact else "")
        or "匿名用户"
    )
    reply_candidates = [message for message in msgs if message.direction == "inbound"]
    reply_heading = "公开评论回复" if conv.channel_type in {"comment", "mention"} else "私信回复"
    text_limit = capability_text_limit(account.platform, dict(account.capability or {})) or 2000
    destination_label = "—"
    window_label = "无平台时限"
    if reply_candidates:
        latest_inbound = reply_candidates[-1]
        try:
            destination = build_direct_reply_destination(
                platform=account.platform,
                reply_target=dict(latest_inbound.reply_target or {}),
                visibility="public",
                occurred_at=latest_inbound.occurred_at,
                now=datetime.now(UTC),
            )
            destination_label = destination.destination_type
            if destination.valid_until is not None:
                window_label = (
                    f"截止 {_fmt(destination.valid_until)}"
                    if destination.valid_until > datetime.now(UTC)
                    else "发送窗口已关闭"
                )
        except ValueError:
            destination_label = "当前渠道不支持直接回复"

    target_choices = "".join(
        f"""<label class="target-choice"><input type="radio" name="reply_to_message_id" value="{message.id}" {"checked" if message is reply_candidates[-1] else ""} required>
{html.escape(_target_label(message.reply_target))}<br><span class="muted">{html.escape((message.text or "非文本消息")[:90])} · {_fmt(message.occurred_at or message.created_at)}</span></label>"""
        for message in reply_candidates
    )
    work_status = work_item.status if work_item is not None else "NONE"
    assigned = (
        work_item.assigned_actor or str(work_item.assigned_user_id or "未认领")
        if work_item is not None
        else "—"
    )
    effective_policy = account.automation_default
    if not get_settings().automation_default_allowed(account.platform, effective_policy):
        effective_policy = "BOT_DRAFT_ONLY"
    policy_label = "自动回复" if effective_policy == "BOT_ACTIVE" else "草稿模式"
    work_actions = ""
    if work_item is not None and work_item.status == "WAITING":
        work_actions += f"""<form class="inline" method="post" action="/admin/work-items/{work_item.id}/claim"><input type="hidden" name="csrf_token" value="{csrf}"><input type="hidden" name="version" value="{work_item.version}"><button class="btn-sm">认领并接管</button></form>"""
    if (
        work_item is not None
        and work_item.status == "CLAIMED"
        and (principal.is_superadmin or work_item.assigned_actor == principal.actor)
    ):
        work_actions += f"""<form class="inline" method="post" action="/admin/work-items/{work_item.id}/resolve"><input type="hidden" name="csrf_token" value="{csrf}"><input type="hidden" name="version" value="{work_item.version}"><button class="btn-sm btn-ghost">解决并恢复{policy_label}</button></form>"""
    if (
        work_item is not None
        and work_item.status == "RESOLVED"
        and cur_state
        in {
            "HANDOFF_PENDING",
            "HUMAN_ACTIVE",
            "BOT_COOLDOWN",
        }
    ):
        resume_auto = (
            '<button class="btn-sm btn-ghost" name="target" value="BOT_ACTIVE">恢复自动</button>'
            if get_settings().automation_default_allowed(account.platform, "BOT_ACTIVE")
            else ""
        )
        work_actions += f"""<form class="inline" method="post" action="/admin/conversations/{conversation_id}/resume"><input type="hidden" name="csrf_token" value="{csrf}"><button class="btn-sm" name="target" value="BOT_DRAFT_ONLY">恢复为草稿</button>{resume_auto}</form>"""

    work_fields = ""
    if work_item is not None and work_item.status in {"WAITING", "CLAIMED"}:
        work_fields = f'<input type="hidden" name="work_item_id" value="{work_item.id}"><input type="hidden" name="version" value="{work_item.version}">'
    can_handle = (
        work_item is None
        or work_item.status != "CLAIMED"
        or principal.is_superadmin
        or work_item.assigned_actor == principal.actor
    )
    composer = (
        f"""<section class="card composer"><h2>{reply_heading}</h2>
<dl class="channel-meta"><dt>回复渠道</dt><dd>{html.escape(destination_label)}</dd><dt>平台发送窗口</dt><dd>{html.escape(window_label)}</dd><dt>文本限制</dt><dd>{text_limit} 字符</dd></dl>
<form method="post" action="/admin/conversations/{conversation_id}/reply"><input type="hidden" name="csrf_token" value="{csrf}"><input type="hidden" name="idempotency_key" value="{uuid.uuid4()}">{work_fields}
<label>回复目标</label>{target_choices}<label for="manual-reply">回复内容</label><textarea id="manual-reply" name="text" required maxlength="{text_limit}"></textarea>
<div class="composer-meta"><span>由当前管理员发送</span><span>最多 {text_limit} 字符</span></div><button>发送回复</button></form></section>"""
        if reply_candidates and can_handle
        else f"<section class='card'><h2>{reply_heading}</h2><div class='banner err'>"
        + (
            "此工作项已由其他客服认领。"
            if reply_candidates
            else "当前会话没有可绑定的入站消息，无法安全发送。"
        )
        + "</div></section>"
    )
    outbox_rows = (
        "".join(
            f"<tr><td class='muted'>{_fmt(row.created_at)}</td><td>{_pill(row.status)}</td>"
            f"<td>{html.escape(row.origin_kind)}</td><td>{html.escape((row.payload or {}).get('text', '')[:52])}</td>"
            f"<td class='muted'>{html.escape(_reason_label(row.last_error_code))}"
            + (f"<br>{html.escape(row.last_error_message)}" if row.last_error_message else "")
            + "</td></tr>"
            for row in outboxes
        )
        or "<tr><td colspan='5' class='muted'>暂无发送记录</td></tr>"
    )
    audit_items = (
        "".join(
            f"<li><strong>{html.escape(entry.action)}</strong> · {html.escape(entry.actor)}"
            f"<div class='muted'>{_fmt(entry.created_at)} · {html.escape(json.dumps(entry.detail or {}, ensure_ascii=False, sort_keys=True))}</div></li>"
            for entry in audit_logs
        )
        or "<li class='muted'>暂无审计记录</li>"
    )
    human_times = [
        message.occurred_at or message.created_at
        for message in msgs
        if message.direction == "outbound" and message.sender_type == "agent"
    ]
    bot_times = [
        message.occurred_at or message.created_at
        for message in msgs
        if message.direction == "outbound" and message.sender_type == "bot"
    ]
    if state_row is not None and state_row.last_human_message_at is not None:
        human_times.append(state_row.last_human_message_at)
    if state_row is not None and state_row.last_bot_message_at is not None:
        bot_times.append(state_row.last_bot_message_at)
    handoff_reason = work_item.reason_code if work_item is not None else None
    if handoff_reason is None:
        handoff_reason = next(
            (
                str(code)
                for decision in decisions
                if decision.action == "handoff"
                for code in (decision.reason_codes or [])
            ),
            None,
        )
    sidebar = f"""<section class="card"><h2>处理状态</h2><table class="kv"><tbody>
<tr><th>Automation</th><td>{_pill(cur_state)}</td></tr><tr><th>人工工作项</th><td>{_pill(work_status)}</td></tr>
<tr><th>转人工原因</th><td>{html.escape(_reason_label(handoff_reason))}</td></tr><tr><th>负责人</th><td>{html.escape(assigned)}</td></tr>
<tr><th>等待时间</th><td>{_health_age(datetime.now(UTC), work_item.created_at) if work_item is not None else "—"}</td></tr>
<tr><th>最近人工发送</th><td>{_fmt(max(human_times, default=None))}</td></tr>
<tr><th>最近机器人发送</th><td>{_fmt(max(bot_times, default=None))}</td></tr>
</tbody></table><div style="margin-top:14px">{work_actions}{buttons}</div></section>
<section class="card"><h2>审计时间线</h2><ul class="audit-list">{audit_items}</ul></section>"""
    body = f"""<a class="back" href="/admin/inbox">← 返回收件箱</a>
<section class="card"><h1 style="font-size:24px">{who}</h1>
<p class="hint">{html.escape(conv.platform)} · {html.escape(_channel_label(conv.channel_type))} · {html.escape(account.name)}</p></section>
<div class="detail-grid"><div class="detail-stack"><section class="card"><h2>消息线程</h2><div class="thread">{bubbles}</div></section>{composer}</div><aside>{sidebar}</aside></div>
<section class="card"><h2>发送状态</h2><div class="tablewrap"><table><thead><tr><th>时间</th><th>状态</th><th>来源</th><th>内容</th><th>平台错误</th></tr></thead><tbody>{outbox_rows}</tbody></table></div></section>
<section class="card"><h2>本会话决策</h2><div class="tablewrap"><table><thead><tr><th>时间</th><th>动作</th><th>意图</th><th>回复</th><th>原因</th><th>置信度</th></tr></thead><tbody>{decision_rows}</tbody></table></div></section>"""
    response = HTMLResponse(
        _page(
            "对话详情",
            body,
            active="conversations",
            show_users=principal.is_superadmin,
        )
    )
    return _ensure_csrf(response, request, csrf)


@router.post("/work-items/{work_item_id}/claim")
async def claim_work_item(request: Request, work_item_id: uuid.UUID) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    try:
        await claim_human_work_item(
            work_item_id=work_item_id,
            allowed_tenants=principal.allowed_tenants,
            actor=principal.actor,
            user_id=principal.user_id,
            expected_version=_expected_version(form),
        )
    except HumanWorkflowError as exc:
        raise _workflow_error(exc) from exc
    return RedirectResponse("/admin/inbox?queue=human", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/work-items/{work_item_id}/resolve")
async def resolve_work_item(request: Request, work_item_id: uuid.UUID) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    try:
        await resolve_human_work_item(
            work_item_id=work_item_id,
            allowed_tenants=principal.allowed_tenants,
            actor=principal.actor,
            expected_version=_expected_version(form),
            allow_override=principal.is_superadmin,
        )
    except HumanWorkflowError as exc:
        raise _workflow_error(exc) from exc
    return RedirectResponse("/admin/inbox?queue=human", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/conversations/{conversation_id}/resume")
async def resume_conversation(request: Request, conversation_id: uuid.UUID) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    target = form.get("target", "")
    if target not in {"BOT_DRAFT_ONLY", "BOT_ACTIVE"}:
        raise HTTPException(status_code=422, detail="resume_target_invalid")
    try:
        await resume_bot(
            conversation_id=conversation_id,
            allowed_tenants=principal.allowed_tenants,
            actor=principal.actor,
            target=target,
        )
    except HumanWorkflowError as exc:
        raise _workflow_error(exc) from exc
    return RedirectResponse(
        f"/admin/conversations/{conversation_id}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/conversations/{conversation_id}/reply")
async def send_manual_reply(request: Request, conversation_id: uuid.UUID) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    text = form.get("text", "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="reply_text_required")
    try:
        reply_to_message_id = uuid.UUID(form.get("reply_to_message_id", ""))
        browser_key = str(uuid.UUID(form.get("idempotency_key", "")))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid_manual_reply_form") from exc
    work_item_id: uuid.UUID | None = None
    expected_version: int | None = None
    if form.get("work_item_id"):
        try:
            work_item_id = uuid.UUID(form["work_item_id"])
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid_work_item_id") from exc
        expected_version = _expected_version(form)
    try:
        await send_human_reply(
            conversation_id=conversation_id,
            reply_to_message_id=reply_to_message_id,
            text=text,
            idempotency_key=browser_key,
            allowed_tenants=principal.allowed_tenants,
            actor=principal.actor,
            user_id=principal.user_id,
            allow_override=principal.is_superadmin,
            work_item_id=work_item_id,
            expected_version=expected_version,
        )
    except HumanWorkflowError as exc:
        raise _workflow_error(exc) from exc
    except OutboxIdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OutboxIntentError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RedirectResponse(
        f"/admin/conversations/{conversation_id}", status_code=status.HTTP_303_SEE_OTHER
    )


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
        account = await session.get(models.PlatformAccount, conv.platform_account_id)
        if account is None or account.tenant_id != conv.tenant_id:
            raise HTTPException(status_code=404, detail="conversation_not_found")
        if not get_settings().automation_default_allowed(account.platform, target):
            raise HTTPException(status_code=422, detail="automation_default_not_allowed")
        # CAS：仅当仍处于提交时看到的状态才翻转（防并发接管竞态）
        if target == AutomationStateEnum.HUMAN_ACTIVE:
            flipped = await flip_to_human_active(
                session,
                conversation_id,
                principal.actor,
                "admin_manual",
                expected_state=AutomationStateEnum(expect),
            )
            if not flipped:
                raise HTTPException(status_code=409, detail="automation_state_version_conflict")
            await ensure_open_human_work_item(
                session,
                tenant_id=conv.tenant_id,
                conversation_id=conversation_id,
                reason_code="ADMIN_MANUAL",
            )
        else:
            open_work = (
                await session.execute(
                    select(models.HumanWorkItem).where(
                        models.HumanWorkItem.conversation_id == conversation_id,
                        models.HumanWorkItem.status.in_(("WAITING", "CLAIMED")),
                    )
                )
            ).scalar_one_or_none()
            if open_work is not None:
                try:
                    require_work_conversation_tenant(
                        open_work, conversation_tenant_id=conv.tenant_id
                    )
                except HumanWorkflowError as exc:
                    raise _workflow_error(exc) from exc
                raise HTTPException(status_code=409, detail="human_work_item_still_open")
            changed = await session.execute(
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
            if changed.rowcount != 1:
                raise HTTPException(status_code=409, detail="automation_state_version_conflict")
        await session.commit()
    return RedirectResponse(
        f"/admin/conversations/{conversation_id}", status_code=status.HTTP_303_SEE_OTHER
    )


# ---------- 决策与草稿审核 ----------


@router.get("/decisions")
async def decisions_page(request: Request) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    return RedirectResponse("/admin/inbox?queue=drafts", status_code=status.HTTP_303_SEE_OTHER)


async def _load_draft(
    session, decision_id: uuid.UUID, principal: Principal
) -> models.ReplyDecision:
    decision = (
        await session.execute(
            select(models.ReplyDecision)
            .where(models.ReplyDecision.id == decision_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if decision is None or decision.tenant_id not in principal.allowed_tenants:
        raise HTTPException(status_code=404, detail="decision_not_found")
    if (
        decision.action != "draft"
        or decision.outbox_id is not None
        or (decision.review_action or "PENDING") != "PENDING"
    ):
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
        original_text = (decision.original_reply_text or decision.reply_text or "").strip()
        final_text = form.get("final_reply_text", original_text).strip()
        if not final_text:
            raise HTTPException(status_code=422, detail="draft_reply_text_required")
        if len(final_text) > 10000:
            raise HTTPException(status_code=422, detail="draft_reply_text_too_long")
        conv = await session.get(models.Conversation, decision.conversation_id)
        if conv is None or conv.tenant_id != decision.tenant_id:
            raise HTTPException(status_code=409, detail="decision_tenant_scope_mismatch")
        account = await session.get(models.PlatformAccount, conv.platform_account_id)
        if account is None or account.tenant_id != decision.tenant_id:
            raise HTTPException(status_code=409, detail="decision_tenant_scope_mismatch")
        try:
            outbox_id = await create_or_get_outbox_intent(
                session,
                conversation_id=conv.id,
                platform_account_id=account.id,
                reply_to_message_id=decision.message_id,
                text=final_text,
                origin_kind=OutboxOrigin.DRAFT_APPROVAL,
                actor_kind=OutboxActor.ADMIN_HUMAN,
                actor_id=principal.actor,
                idempotency_key=f"draft-approval:{decision.id}",
                visibility=decision.reply_visibility or "public",
                payload_metadata={"approval": "admin", "approved_by": principal.actor},
            )
        except OutboxIdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except OutboxIntentError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        review_action = "ACCEPTED" if final_text == original_text else "EDITED"
        await session.execute(
            update(models.ReplyDecision)
            .where(
                models.ReplyDecision.id == decision_id,
                models.ReplyDecision.outbox_id.is_(None),
            )
            .values(
                original_reply_text=original_text,
                final_reply_text=final_text,
                review_action=review_action,
                reviewed_by=principal.actor,
                reviewed_at=datetime.now(UTC),
                review_reason=None,
                outbox_id=outbox_id,
            )
        )
        await session.execute(
            models.AuditLog.__table__.insert().values(
                tenant_id=account.tenant_id,
                category="admin_action",
                actor=principal.actor,
                action="APPROVE_DRAFT",
                subject_type="reply_decision",
                subject_id=str(decision_id),
                detail={"outbox_id": str(outbox_id), "review_action": review_action},
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
    return RedirectResponse("/admin/inbox?queue=drafts", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/decisions/{decision_id}/discard")
async def discard_draft(request: Request, decision_id: uuid.UUID) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    review_reason = form.get("review_reason", "").strip()
    if not review_reason:
        raise HTTPException(status_code=422, detail="draft_review_reason_required")
    if len(review_reason) > 500:
        raise HTTPException(status_code=422, detail="draft_review_reason_too_long")
    async with get_session_factory()() as session:
        decision = await _load_draft(session, decision_id, principal)
        reason_codes = list(decision.reason_codes or [])
        if "ADMIN_DISCARDED" not in reason_codes:
            reason_codes.append("ADMIN_DISCARDED")
        await session.execute(
            update(models.ReplyDecision)
            .where(models.ReplyDecision.id == decision_id)
            .values(
                original_reply_text=decision.original_reply_text or decision.reply_text,
                final_reply_text=None,
                review_action="REJECTED",
                reviewed_by=principal.actor,
                reviewed_at=datetime.now(UTC),
                review_reason=review_reason,
                reason_codes=reason_codes,
            )
        )
        await session.execute(
            models.AuditLog.__table__.insert().values(
                tenant_id=decision.tenant_id,
                category="admin_action",
                actor=principal.actor,
                action="REJECT_DRAFT",
                subject_type="reply_decision",
                subject_id=str(decision_id),
                detail={"reason": review_reason},
            )
        )
        await session.commit()
    return RedirectResponse("/admin/inbox?queue=drafts", status_code=status.HTTP_303_SEE_OTHER)


# ---------- 知识库 ----------


def _log_knowledge_exception(message: str, exc: Exception) -> None:
    sanitized = RuntimeError("exception details redacted")
    logger.exception(
        "%s: exception_type=%s",
        message,
        type(exc).__name__,
        exc_info=(RuntimeError, sanitized, exc.__traceback__),
    )


_KB_BANNERS = {
    "added": ("ok", "知识条目已添加为草稿并完成向量化；明确发布前不会参与检索。"),
    "duplicate": ("err", "内容重复：相同问答已存在。"),
    "embed_failed": ("err", "向量化失败：请检查 Embedding 服务配置后重试。"),
    "status_changed": ("ok", "知识条目状态已更新并记录审计。"),
    "classification_changed": ("ok", "官方联系方式分类已更新并记录审计。"),
    "deleted": ("ok", "条目已删除。"),
    "import_bad_csv": (
        "err",
        "CSV 无效：需 UTF-8 编码，必需列 question,reply；is_official_contact 仅接受 true/false/1/0/yes/no。",
    ),
    "import_too_large": ("err", "文件过大：请上传不超过 2MB 的 CSV。"),
}

_MAX_IMPORT_BYTES = 2 * 1024 * 1024


def _query_int(request: Request, name: str) -> int:
    try:
        return max(0, int(request.query_params.get(name) or 0))
    except ValueError:
        return 0


def _knowledge_actions(doc: models.KnowledgeDocument, csrf: str) -> str:
    status_target = "draft" if doc.status == "published" else "published"
    status_label = "下架" if doc.status == "published" else "明确发布"
    if doc.status == "draft":
        official_target = "false" if doc.is_official_contact else "true"
        official_label = "取消官方分类" if doc.is_official_contact else "分类为官方联系方式"
        classification = (
            f'<form class="inline" method="post" '
            f'action="/admin/knowledge/{doc.id}/official-contact">'
            f'<input type="hidden" name="csrf_token" value="{csrf}">'
            f'<input type="hidden" name="target" value="{official_target}">'
            f'<button class="btn-sm btn-ghost">{official_label}</button></form>'
        )
    else:
        classification = '<span class="muted">先下架再更改分类</span>'
    return f"""<form class="inline" method="post" action="/admin/knowledge/{doc.id}/status"><input type="hidden" name="csrf_token" value="{csrf}"><input type="hidden" name="target" value="{status_target}"><button class="btn-sm btn-ghost">{status_label}</button></form>
{classification}
<form class="inline" method="post" action="/admin/knowledge/{doc.id}/delete"><input type="hidden" name="csrf_token" value="{csrf}"><button class="btn-sm btn-danger" onclick="return confirm('确认删除该知识条目？此操作不可恢复。')">删除</button></form>"""


@router.get("/content/knowledge", response_class=HTMLResponse)
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
            f"<td>{'是' if d.is_official_contact else '否'}</td>"
            f"<td>{_pill(d.status)}</td>"
            f"<td>{_knowledge_actions(d, csrf)}</td></tr>"
            for d in docs
        )
        or "<tr><td colspan='6' class='muted'>知识库为空</td></tr>"
    )
    add_form = f"""<details class="collapse"><summary>新增知识条目</summary><div class="inner">
<form method="post" action="/admin/knowledge/add"><input type="hidden" name="csrf_token" value="{csrf}">
{_tenant_input(principal)}{_input("question", "触发问题（用户会怎么问）")}
<label for="f-kb-reply">标准回复（命中后原文发送）</label><textarea id="f-kb-reply" name="reply" required></textarea>
{_input("category", "分类（可选）", required=False)}{_input("brand_id", "Brand（默认 default）", required=False)}
<label><input type="checkbox" name="is_official_contact" value="true"> 这是已审核的官方联系方式模板</label>
<p class="hint">新条目始终保存为草稿，明确发布前不会参与检索或发送。</p>
<button class="btn-block">添加草稿并向量化</button></form></div></details>"""
    import_form = f"""<details class="collapse"><summary>批量导入 CSV</summary><div class="inner">
<form method="post" action="/admin/knowledge/import" enctype="multipart/form-data"><input type="hidden" name="csrf_token" value="{csrf}">
{_tenant_input(principal)}{_input("brand_id", "Brand（默认 default）", required=False)}
<label for="f-kb-csv">CSV 文件</label><input id="f-kb-csv" type="file" name="file" accept=".csv" required>
<p class="hint">必需列 question,reply；可选 brand_id,platform,category,is_official_contact。布尔值仅接受 true/false/1/0/yes/no（不区分大小写）；空白为 false。所有导入行均为草稿，明确发布前不会参与检索。UTF-8 编码，最多 2000 行 / 2MB。</p>
<button class="btn-block">上传并导入草稿</button></form></div></details>"""
    body = f"""<h1>知识库</h1><p class="lede">回复模板管理：新建和导入默认草稿；只有经过明确发布的条目才参与检索。</p>{banner}
{add_form}
{import_form}
<section class="card"><h2>模板列表</h2><p class="hint">共 {len(docs)} 条（最多显示 200）。官方联系方式必须先分类、复核，再明确发布。</p><div class="tablewrap"><table><thead><tr><th>问题</th><th>回复</th><th>分类</th><th>官方联系方式</th><th>状态</th><th>操作</th></tr></thead><tbody>{rows}</tbody></table></div></section>"""
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
    official_value = (form.get("is_official_contact") or "").strip().casefold()
    if official_value not in {"", "true"}:
        raise HTTPException(status_code=422, detail="invalid_is_official_contact")
    is_official_contact = official_value == "true"
    if not question or not reply:
        raise HTTPException(status_code=422, detail="question_and_reply_required")
    draft = build_knowledge_draft(
        tenant_id=tenant_id,
        question=question,
        reply=reply,
        brand_id=(form.get("brand_id") or "").strip() or "default",
        category=(form.get("category") or "").strip() or None,
        is_official_contact=is_official_contact,
        source_file="admin-console",
    )
    from social_reply.application.reply_decision.runner import _get_embedder

    async with get_session_factory()() as session:
        existing = await existing_content_hashes(
            session,
            tenant_id=tenant_id,
            content_hashes=[draft.content_hash],
        )
    if existing:
        return RedirectResponse(
            "/admin/knowledge?notice=duplicate", status_code=status.HTTP_303_SEE_OTHER
        )
    try:
        embedder = _get_embedder()
        embedding = (await embedder.embed([draft.embed_text]))[0]
    except Exception as exc:
        _log_knowledge_exception("Knowledge manual add embedding failed", exc)
        return RedirectResponse(
            "/admin/knowledge?notice=embed_failed", status_code=status.HTTP_303_SEE_OTHER
        )
    async with get_session_factory()() as session:
        await persist_knowledge_draft(
            session,
            draft,
            embedding_version=embedder.version,
            embedding=embedding,
            actor=principal.actor,
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

    from social_reply.application.reply_decision.runner import _get_embedder

    try:
        embedder = _get_embedder()
        report = await import_knowledge_rows(
            io.StringIO(text_csv),
            source_name=source_name,
            embedder=embedder,
            tenant_id=tenant_id,
            brand_id_default=brand_id_default,
            actor=principal.actor,
        )
    except ValueError:
        return RedirectResponse(
            "/admin/knowledge?notice=import_bad_csv", status_code=status.HTTP_303_SEE_OTHER
        )
    except Exception as exc:
        _log_knowledge_exception("Knowledge CSV import failed", exc)
        return RedirectResponse(
            "/admin/knowledge?notice=embed_failed", status_code=status.HTTP_303_SEE_OTHER
        )
    return RedirectResponse(
        f"/admin/knowledge?notice=imported&inserted={report.inserted}"
        f"&skipped={report.skipped}&blank={report.blank}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/knowledge/{doc_id}/status")
async def knowledge_set_status(request: Request, doc_id: uuid.UUID) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    target = (form.get("target") or "").strip()
    if target not in {"draft", "published"}:
        raise HTTPException(status_code=422, detail="invalid_knowledge_status")
    async with get_session_factory()() as session:
        doc = (
            await session.execute(
                select(models.KnowledgeDocument)
                .where(
                    models.KnowledgeDocument.id == doc_id,
                    models.KnowledgeDocument.tenant_id.in_(principal.allowed_tenants),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if doc is None:
            raise HTTPException(status_code=404, detail="knowledge_not_found")
        previous = doc.status
        if previous != target:
            doc.status = target
            await session.execute(
                models.AuditLog.__table__.insert().values(
                    tenant_id=doc.tenant_id,
                    category="admin_action",
                    actor=principal.actor,
                    action=(
                        "PUBLISH_KNOWLEDGE" if target == "published" else "UNPUBLISH_KNOWLEDGE"
                    ),
                    subject_type="knowledge_document",
                    subject_id=str(doc.id),
                    detail={
                        "from": previous,
                        "to": target,
                        "brand": doc.brand_id,
                        "platform": doc.platform,
                        "is_official_contact": doc.is_official_contact,
                    },
                )
            )
        await session.commit()
    return RedirectResponse(
        "/admin/knowledge?notice=status_changed", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/knowledge/{doc_id}/official-contact")
async def knowledge_set_official_contact(request: Request, doc_id: uuid.UUID) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    target_value = (form.get("target") or "").strip()
    if target_value not in {"true", "false"}:
        raise HTTPException(status_code=422, detail="invalid_official_contact_target")
    target = target_value == "true"
    async with get_session_factory()() as session:
        doc = (
            await session.execute(
                select(models.KnowledgeDocument)
                .where(
                    models.KnowledgeDocument.id == doc_id,
                    models.KnowledgeDocument.tenant_id.in_(principal.allowed_tenants),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if doc is None:
            raise HTTPException(status_code=404, detail="knowledge_not_found")
        if doc.status != "draft":
            raise HTTPException(status_code=409, detail="unpublish_before_classification")
        previous = doc.is_official_contact
        if previous != target:
            content_hash = await session.scalar(
                select(models.KnowledgeChunk.content_hash).where(
                    models.KnowledgeChunk.document_id == doc.id
                )
            )
            doc.is_official_contact = target
            await session.execute(
                models.AuditLog.__table__.insert().values(
                    tenant_id=doc.tenant_id,
                    category="admin_action",
                    actor=principal.actor,
                    action="SET_KNOWLEDGE_OFFICIAL_CONTACT",
                    subject_type="knowledge_document",
                    subject_id=str(doc.id),
                    detail={
                        "from": previous,
                        "to": target,
                        "brand": doc.brand_id,
                        "platform": doc.platform,
                        "status": doc.status,
                        "content_hash": content_hash,
                    },
                )
            )
        await session.commit()
    return RedirectResponse(
        "/admin/knowledge?notice=classification_changed",
        status_code=status.HTTP_303_SEE_OTHER,
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


# ---------- 提示词品牌表达偏好 ----------

_PROMPT_BANNERS = {
    "saved": ("ok", "结构化品牌语气偏好已保存，下一条 LLM 决策立即生效。"),
    "voice_preferences_invalid": ("err", "品牌语气偏好无效，请只选择页面提供的选项。"),
}

_VOICE_UI = {
    "tone": (
        "语气",
        (
            (VoiceTone.PROFESSIONAL.value, "专业"),
            (VoiceTone.WARM.value, "温暖"),
            (VoiceTone.FORMAL.value, "正式"),
        ),
    ),
    "length": (
        "篇幅",
        (
            (VoiceLength.CONCISE.value, "简洁"),
            (VoiceLength.BALANCED.value, "均衡"),
        ),
    ),
    "empathy": (
        "同理心",
        (
            (VoiceEmpathy.STANDARD.value, "标准"),
            (VoiceEmpathy.HIGH.value, "高同理心"),
        ),
    ),
    "emoji": (
        "Emoji",
        (
            (VoiceEmoji.NEVER.value, "不使用"),
            (VoiceEmoji.SPARINGLY.value, "少量使用"),
        ),
    ),
}


def _voice_select(name: str, current: str) -> str:
    field_label, field_options = _VOICE_UI[name]
    options = "".join(
        f'<option value="{value}"{" selected" if value == current else ""}>{label}</option>'
        for value, label in field_options
    )
    return (
        f'<label for="f-{name}">{field_label}</label>'
        f'<select id="f-{name}" name="{name}" required>{options}</select>'
    )


def _prompt_tenant(principal: Principal, requested: str) -> str:
    return tenant_id_or_default(principal, requested)


@router.get("/content/brand-voice", response_class=HTMLResponse)
@router.get("/prompt", response_class=HTMLResponse)
async def prompt_page(request: Request, notice: str = "", tenant_id: str = "") -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    csrf = _csrf(request)
    tenant = _prompt_tenant(principal, tenant_id)
    brand = (request.query_params.get("brand_id") or "default").strip() or "default"
    async with get_session_factory()() as session:
        resolved = await load_persona(session, tenant, brand)
    banner = ""
    if notice in _PROMPT_BANNERS:
        tone, text = _PROMPT_BANNERS[notice]
        banner = f'<div class="banner {tone}">{html.escape(text)}</div>'
    origin = (
        '<span class="pill warn">代码内置默认</span>'
        if resolved.is_default
        else f'<span class="pill ok">已自定义 · 第 {resolved.revision} 版</span>'
    )
    trial = ""
    if request.query_params.get("trial"):
        trial = _render_trial(request)
    preferences = resolved.preferences
    voice_fields = "".join(
        _voice_select(name, getattr(preferences, name).value) for name in _VOICE_UI
    )
    body = f"""<h1>品牌语气</h1><p class="lede">设置有限、可审计的品牌表达偏好；后台不接受任意系统指令，系统身份、事实边界、动作含义和安全规则固定且不可覆盖。</p>{banner}
<section class="card"><h2>结构化品牌语气偏好 {origin}</h2>
<p class="hint">这些选项只影响需要 LLM 生成回复时的代码内置表达条款。知识库原文直答不经过它；无法添加自由文本或覆盖安全契约。</p>
<form method="post" action="/admin/prompt/save"><input type="hidden" name="csrf_token" value="{csrf}">
<input type="hidden" name="tenant_id" value="{html.escape(tenant)}">
<input type="hidden" name="brand_id" value="{html.escape(brand)}">
{voice_fields}
<button class="btn-block">保存</button></form>
<details class="collapse"><summary>查看代码编译后的语气条款</summary><div class="inner"><pre class="thread" style="white-space:pre-wrap">{html.escape(resolved.text)}</pre></div></details></section>

<section class="card"><h2>系统固定追加</h2>
<p class="hint">以下不可变契约始终拼在代码编译的语气条款之后，后台无法删除或覆盖；严格六字段 schema 也由代码控制。</p>
<pre class="thread" style="white-space:pre-wrap">{html.escape(CONTRACT_PROMPT)}</pre></section>

<section class="card"><h2>试运行</h2>
<p class="hint">用当前保存并由代码编译的语气偏好跑一次真实 LLM 调用，只看结果，不写库、不建 outbox、不发送。</p>
<form method="post" action="/admin/prompt/trial"><input type="hidden" name="csrf_token" value="{csrf}">
<input type="hidden" name="tenant_id" value="{html.escape(tenant)}">
<input type="hidden" name="brand_id" value="{html.escape(brand)}">
{_input("text", "测试消息（模拟客户发来的内容）")}
<button class="btn-block">试运行</button></form>{trial}</section>"""
    response = HTMLResponse(
        _page("品牌语气", body, active="brand-voice", show_users=principal.is_superadmin)
    )
    return _ensure_csrf(response, request, csrf)


def _render_trial(request: Request) -> str:
    q = request.query_params
    if q.get("trial") == "failed":
        return '<div class="banner err">试运行失败：LLM 调用出错，请检查供应商配置与额度。</div>'
    rows = "".join(
        f"<tr><td class='muted'>{html.escape(label)}</td><td>{html.escape(q.get(key) or '—')}</td></tr>"
        for label, key in (
            ("动作", "action"),
            ("意图", "intent"),
            ("风险", "risk"),
            ("置信度", "confidence"),
            ("原因码", "codes"),
        )
    )
    reply = q.get("reply") or ""
    reply_block = (
        f"<div class='msg out' style='margin-top:10px'>{html.escape(reply)}</div>"
        if reply
        else "<p class='muted'>该动作不产生对外回复。</p>"
    )
    return f"""<div class="tablewrap" style="margin-top:14px"><table><tbody>{rows}</tbody></table></div>{reply_block}"""


@router.post("/prompt/save")
async def prompt_save(request: Request) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    tenant = _prompt_tenant(principal, form.get("tenant_id", ""))
    brand = (form.get("brand_id") or "default").strip() or "default"
    allowed_fields = {"csrf_token", "tenant_id", "brand_id"} | VOICE_PREFERENCE_FIELDS
    try:
        if set(form) - allowed_fields:
            raise ValueError("voice_preferences_invalid")
        preferences = parse_voice_preferences(
            {name: form.get(name) or "" for name in VOICE_PREFERENCE_FIELDS}
        )
    except ValueError:
        return RedirectResponse(
            "/admin/prompt?notice=voice_preferences_invalid",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    persona = compile_voice_preferences(preferences)
    voice_preferences = preferences.to_dict()
    async with get_session_factory()() as session:
        row = (
            await session.execute(
                select(models.ReplyPrompt).where(
                    models.ReplyPrompt.tenant_id == tenant,
                    models.ReplyPrompt.brand_id == brand,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            revision = 1
            session.add(
                models.ReplyPrompt(
                    tenant_id=tenant,
                    brand_id=brand,
                    persona=persona,
                    voice_preferences=voice_preferences,
                    revision=revision,
                    updated_by=principal.actor,
                )
            )
        else:
            revision = row.revision + 1
            row.persona = persona
            row.voice_preferences = voice_preferences
            row.revision = revision
            row.updated_by = principal.actor
        await session.execute(
            models.AuditLog.__table__.insert().values(
                tenant_id=tenant,
                category="admin_action",
                actor=principal.actor,
                action="SET_REPLY_PERSONA",
                subject_type="reply_prompt",
                subject_id=f"{tenant}:{brand}",
                detail={
                    "revision": revision,
                    "voice_preferences": voice_preferences,
                },
            )
        )
        await session.commit()
    return RedirectResponse("/admin/prompt?notice=saved", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/prompt/trial")
async def prompt_trial(request: Request) -> Response:
    """Run compiled voice preferences through the LLM without persistence or delivery."""
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    tenant = _prompt_tenant(principal, form.get("tenant_id", ""))
    brand = (form.get("brand_id") or "default").strip() or "default"
    text = (form.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="text_required")
    from social_reply.application.reply_decision.runner import _get_llm

    async with get_session_factory()() as session:
        resolved = await load_persona(session, tenant, brand)
    try:
        decision = await _get_llm().decide(
            LLMContext(
                text=redact_pii(text),
                conversation_key=f"trial:{tenant}",
                voice_preferences=resolved.preferences,
            )
        )
    except Exception:
        logger.exception("prompt trial failed tenant=%s", tenant)
        return RedirectResponse("/admin/prompt?trial=failed", status_code=status.HTTP_303_SEE_OTHER)
    params = urlencode(
        {
            "trial": "1",
            "action": decision.action.value,
            "intent": decision.intent or "",
            "risk": decision.risk_level.value,
            "confidence": f"{decision.confidence:.2f}",
            "codes": ",".join(decision.reason_codes),
            "reply": decision.reply_text or "",
        }
    )
    return RedirectResponse(f"/admin/prompt?{params}", status_code=status.HTTP_303_SEE_OTHER)


# ---------- System health ----------


@router.get("/system/health", response_class=HTMLResponse)
@router.get("/health", response_class=HTMLResponse)
async def health_page(request: Request) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    tenants = principal.allowed_tenants
    now = datetime.now(UTC)
    day_ago = now - timedelta(hours=24)
    async with get_session_factory()() as session:
        health_metrics = await _load_health_metrics(session, tenants, now)
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
    metric_rows = "".join(
        f'<tr id="{metric.key}"><td><strong>{metric.label}</strong></td>'
        f"<td>{_pill(metric.level)}</td>"
        f"<td>{metric.action_count} 需处理 · {metric.warning_count} 恢复中</td>"
        f"<td class='muted'>{_health_age(now, metric.oldest_at)}</td>"
        f"<td><a href='{metric.href}'>查看</a></td></tr>"
        for metric in health_metrics
    )
    rows = (
        "".join(
            f"<tr><td class='muted'>{_fmt(o.created_at)}</td><td>{_pill(o.status)}</td>"
            f"<td class='muted'>{html.escape(o.destination_type)}</td>"
            f"<td>{html.escape((o.payload or {}).get('text', '')[:42])}</td>"
            f"<td class='muted'>{o.attempt_count}</td>"
            f"<td class='muted'>{html.escape(o.last_error_code or '—')}"
            + (f"<br>{html.escape(o.last_error_message)}" if o.last_error_message else "")
            + "</td></tr>"
            for o in outbox
        )
        or "<tr><td colspan='6' class='muted'>暂无投递记录</td></tr>"
    )
    ingress_rows = (
        "".join(
            f"<tr><td>{html.escape(source)}</td><td>{_pill(processing_status)}</td>"
            f"<td class='muted'>{count}</td><td class='muted'>{_fmt(last_at)}</td></tr>"
            for source, processing_status, count, last_at in ingress
        )
        or "<tr><td colspan='4' class='muted'>24 小时内无入站事件</td></tr>"
    )
    body = f"""<h1>系统健康</h1><p class="lede">只读查看核心处理链路、投递与入站事件状态。</p>
<section class="card"><h2>核心链路</h2><div class="tablewrap"><table><thead><tr><th>环节</th><th>状态</th><th>积压</th><th>最老等待</th><th></th></tr></thead><tbody>{metric_rows}</tbody></table></div></section>
<section class="card"><h2>Outbox</h2><p class="hint">最近 50 条出站消息；需要处理的失败项统一进入收件箱。</p><div class="tablewrap"><table><thead><tr><th>时间</th><th>状态</th><th>目的地</th><th>内容</th><th>尝试</th><th>错误</th></tr></thead><tbody>{rows}</tbody></table></div></section>
<section class="card" id="ingress"><h2>入站健康（24h）</h2><div class="tablewrap"><table><thead><tr><th>来源</th><th>处理状态</th><th>事件数</th><th>最后接收</th></tr></thead><tbody>{ingress_rows}</tbody></table></div></section>"""
    return HTMLResponse(
        _page("系统健康", body, active="health", show_users=principal.is_superadmin)
    )


@router.get("/delivery")
async def delivery_page(request: Request) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    return RedirectResponse("/admin/inbox?queue=delivery", status_code=status.HTTP_303_SEE_OTHER)


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
    return RedirectResponse("/admin/inbox?queue=delivery", status_code=status.HTTP_303_SEE_OTHER)


# ---------- 账号 / 急停 / 接入 ----------



_CHANNEL_LABELS = {
    "x": "X",
    "facebook": "Facebook",
    "instagram": "Instagram",
    "telegram": "Telegram",
    "whatsapp": "WhatsApp",
    "feishu": "Feishu",
    "email": "Email",
}

_CHANNEL_KINDS = {
    "x": "OAuth",
    "facebook": "OAuth",
    "instagram": "2 种登录方式",
    "telegram": "Bot Token",
    "whatsapp": "Cloud API",
    "feishu": "自建应用 Bot",
    "email": "IMAP / SMTP",
}


def _channel_icon(channel: str) -> str:
    return (
        '<span class="channel-icon" aria-hidden="true">'
        f'<img src="/static/channel-icons/{channel}.svg" alt="" width="36" height="36">'
        "</span>"
    )


def _channel_tile(channel: str, *, enabled: bool, selected: bool) -> str:
    label = _CHANNEL_LABELS[channel]
    icon = _channel_icon(channel)
    status = _CHANNEL_KINDS[channel] if enabled else "未启用"
    inner = (
        f'{icon}<span class="channel-name">{html.escape(label)}</span>'
        f'<span class="channel-kind">{html.escape(status)}</span>'
    )
    if not enabled:
        return (
            f'<div class="channel-tile disabled" data-channel="{channel}" '
            f'aria-disabled="true" aria-label="{html.escape(label)} 未启用">{inner}</div>'
        )
    current = ' aria-current="true"' if selected else ""
    return (
        f'<a class="channel-tile" data-channel="{channel}"{current} '
        f'href="/admin/integrations/accounts/new/{channel}" '
        f'aria-label="连接 {html.escape(label)}">{inner}</a>'
    )


def _channel_setup_head(channel: str, subtitle: str) -> str:
    return (
        '<div class="channel-setup-head">'
        f"{_channel_icon(channel)}<div><h2>连接 {html.escape(_CHANNEL_LABELS[channel])}</h2>"
        f"<p>{html.escape(subtitle)}</p></div></div>"
    )


@router.get("/integrations/accounts/new/{provider}", response_class=HTMLResponse)
@router.get("/integrations/accounts", response_class=HTMLResponse)
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
    finally:
        await redis.aclose()
    account_stopped = {
        str(account.id): account_flags[index] is not None for index, account in enumerate(accounts)
    }
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
        automation_form = ""
        # 部署 gate 关闭时账号只能向草稿收敛，因此仅对历史 BOT_ACTIVE 账号保留回退按钮。
        if settings.automation_default_allowed(a.platform, auto_target):
            automation_form = f"""<form class="inline" method="post" action="/admin/accounts/{a.id}/automation"><input type="hidden" name="csrf_token" value="{csrf}"><input type="hidden" name="target" value="{auto_target}"><button class="btn-sm btn-ghost">{auto_label}</button></form>"""
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
        elif a.platform == "feishu":
            account_config = dict(a.config or {})
            health_status = str(account_config.get("feishu_health_status") or "UNKNOWN")
            bot_status = (
                "ACTIVE" if account_config.get("feishu_bot_activate_status") == 2 else "UNKNOWN"
            )
            bot_name = str(account_config.get("feishu_bot_name") or "—")
            checked_at = str(account_config.get("feishu_health_checked_at") or "—")
            error_code = str(account_config.get("feishu_health_error_code") or "—")
            channel_status = (
                f"<div>Health {_pill(health_status)}</div>"
                f"<div>Bot {_pill(bot_status)}</div>"
                f"<div class='muted'>{html.escape(bot_name)}</div>"
                f"<div class='muted'>{html.escape(error_code)}</div>"
                f"<div class='muted'>{html.escape(checked_at)}</div>"
            )
        elif a.platform in {"facebook", "instagram"}:
            account_config = dict(a.config or {})
            capability = dict(a.capability or {})
            health_status = str(account_config.get("meta_health_status") or "UNKNOWN")
            subscribed = ", ".join(account_config.get("meta_subscribed_fields") or []) or "—"
            error_code = str(account_config.get("meta_health_error_code") or "—")
            comments_status = health_status if capability.get("comments") else "DISABLED"
            channel_status = (
                f"<div>Messaging {_pill(health_status)}</div>"
                f"<div>Comments {_pill(comments_status)}</div>"
                f"<div class='muted'>{html.escape(subscribed)}</div>"
                f"<div class='muted'>{html.escape(error_code)}</div>"
            )
        elif a.platform == "email":
            account_config = dict(a.config or {})
            probe_status = str(account_config.get("email_health_status") or "UNKNOWN")
            if probe_status == "READY":
                probe_result = '<span class="pill ok">通过</span>'
            elif probe_status == "UNKNOWN":
                probe_result = '<span class="pill neutral">未知</span>'
            else:
                probe_result = '<span class="pill err">错误</span>'
            mailbox = str(account_config.get("mailbox") or "—")
            smtp_security = str(account_config.get("smtp_security") or "—")
            checked_at = _fmt_iso_timestamp(account_config.get("email_health_checked_at"))
            raw_error_code = str(account_config.get("email_health_error_code") or "—")
            error_code = raw_error_code
            if raw_error_code != "—" and (
                len(raw_error_code) > 64
                or not raw_error_code.isascii()
                or not all(
                    character.isupper() or character.isdigit() or character in {"_", "-"}
                    for character in raw_error_code
                )
            ):
                error_code = "EMAIL_HEALTH_ERROR"
            channel_status = (
                f"<div>接入探测 {probe_result}</div>"
                f"<div class='muted'>Mailbox {html.escape(mailbox)}</div>"
                f"<div class='muted'>Security {html.escape(smtp_security)}</div>"
                f"<div class='muted'>探测时间 {html.escape(checked_at)}</div>"
                f"<div class='muted'>错误 {html.escape(error_code)}</div>"
                "<div class='muted'>仅表示最近一次凭证接入验证，不是持续监控</div>"
            )
        account_rows += (
            f"<tr><td>{html.escape(a.platform)}</td><td>{html.escape(a.name)}</td>"
            f"<td>{_pill(a.status)}</td><td>{channel_status}</td>"
            f"<td>{_pill(a.automation_default)}</td><td>{ks_pill}</td>"
            f"""<td>{automation_form}
<form class="inline" method="post" action="/admin/killswitch/toggle"><input type="hidden" name="csrf_token" value="{csrf}"><input type="hidden" name="scope" value="account"><input type="hidden" name="account_id" value="{a.id}"><input type="hidden" name="tenant_id" value="{html.escape(a.tenant_id)}"><button class="btn-sm {ks_cls}">{ks_btn}</button></form>{xchat_form}</td></tr>"""
        )
    account_rows = account_rows or "<tr><td colspan='7' class='muted'>尚未连接账号</td></tr>"

    job_rows = (
        "".join(
            f"<tr><td><a href='/admin/integrations/provisioning-jobs/{row.id}'><code>{str(row.id)[:8]}</code></a></td>"
            f"<td>{html.escape(row.platform)}</td>"
            f"<td>{_pill('PROCESSING' if row.status == 'FAILED' and provisioning_job_is_in_flight(row) else row.status)}</td>"
            f"<td class='muted'>{html.escape(row.current_step)}</td>"
            f"<td class='muted'>{html.escape(row.last_error_code or '—')}</td></tr>"
            for row in jobs
        )
        or "<tr><td colspan='5' class='muted'>暂无任务</td></tr>"
    )
    common = (
        f'<input type="hidden" name="csrf_token" value="{csrf}">'
        + _tenant_input(principal)
        + _input("brand_id", "Brand", required=True, value="default")
        + _input("name", "显示名称（可选）", required=False)
    )

    def oauth_fields() -> str:
        return (
            f'<input type="hidden" name="csrf_token" value="{csrf}">'
            + _tenant_input(principal)
            + _input("brand_id", "Brand", required=True, value="default")
        )

    x_callback = f"{settings.public_base_url.rstrip('/')}/admin/oauth/x/callback"
    meta_callback = f"{settings.public_base_url.rstrip('/')}/admin/oauth/meta/callback"
    instagram_callback = f"{settings.public_base_url.rstrip('/')}/admin/oauth/instagram/callback"
    xchat_oauth_input = (
        _input(
            "xchat_pin",
            "XChat 4 位 PIN（可选）",
            secret=True,
            required=False,
        )
        if settings.xchat_enabled
        else ""
    )
    xchat_manual_input = (
        _input(
            "xchat_pin",
            "XChat 4 位 PIN（可选）",
            secret=True,
            required=False,
        )
        if settings.xchat_enabled
        else ""
    )
    channel_enabled = {
        "x": settings.x_integration_enabled,
        "facebook": settings.facebook_messenger_enabled,
        "instagram": settings.instagram_messaging_enabled,
        "telegram": True,
        "whatsapp": settings.whatsapp_enabled,
        "feishu": settings.feishu_enabled,
        "email": settings.email_enabled,
    }
    requested_channel = request.path_params.get("provider") or request.query_params.get("connect", "")
    if request.path_params.get("provider") and requested_channel not in _CHANNEL_LABELS:
        raise HTTPException(status_code=404, detail="integration_provider_not_found")
    selected_channel = (
        requested_channel
        if requested_channel in _CHANNEL_LABELS and channel_enabled[requested_channel]
        else ""
    )
    channel_notice = ""
    if requested_channel in _CHANNEL_LABELS and not channel_enabled[requested_channel]:
        channel_notice = '<div class="banner info">该渠道尚未在当前部署启用。</div>'
    channel_tiles = "".join(
        _channel_tile(
            channel,
            enabled=channel_enabled[channel],
            selected=channel == selected_channel,
        )
        for channel in _CHANNEL_LABELS
    )
    channel_picker = f"""<section class="channel-section" aria-labelledby="add-channel-title">
<div class="channel-heading"><div><h2 id="add-channel-title">添加渠道</h2><p>选择要连接的平台</p></div><span class="muted">{len(_CHANNEL_LABELS)} 个平台</span></div>
<div class="channel-grid">{channel_tiles}</div></section>{channel_notice}"""
    facebook_comments = settings.meta_comment_reply_enabled
    facebook_policy_fields = (
        '<input type="hidden" name="instagram_login_mode" value="facebook_login">'
        '<input type="hidden" name="enable_dm" value="true">'
        f'<input type="hidden" name="enable_comments" value="{str(facebook_comments).lower()}">'
        '<input type="hidden" name="automation_default" value="BOT_DRAFT_ONLY">'
    )
    instagram_comments = settings.meta_comment_reply_enabled
    instagram_policy_fields = (
        '<input type="hidden" name="instagram_login_mode" value="facebook_login">'
        '<input type="hidden" name="enable_dm" value="true">'
        f'<input type="hidden" name="enable_comments" value="{str(instagram_comments).lower()}">'
        '<input type="hidden" name="automation_default" value="BOT_DRAFT_ONLY">'
    )
    selected_panel = ""
    if selected_channel == "x":
        selected_panel = f"""<section class="channel-setup" id="channel-setup">
{_channel_setup_head("x", "使用部署级 X OAuth 应用授权账号")}
<form class="channel-form" method="post" action="/admin/oauth/x/start">{oauth_fields()}{xchat_oauth_input}
<dl class="channel-meta"><dt>Callback URI</dt><dd><code>{html.escape(x_callback)}</code></dd><dt>授权范围</dt><dd>Read and write{"" if not (settings.x_legacy_dm_enabled or settings.xchat_enabled) else " and Direct message"}</dd></dl>
<button class="btn-block">继续使用 X 授权</button></form>
<details class="advanced-connect"><summary>高级连接：使用已有 Token</summary><div class="advanced-body"><form method="post" action="/admin/connect/x">{common}{_input("consumer_key", "Consumer Key", secret=True)}{_input("consumer_secret", "Consumer Secret", secret=True)}{_input("access_token", "Access Token", secret=True)}{_input("access_token_secret", "Access Token Secret", secret=True)}<input type="hidden" name="environment" value="oauth">{xchat_manual_input}<button class="btn-block">连接 X</button></form></div></details></section>"""
    elif selected_channel == "facebook":
        selected_panel = f"""<section class="channel-setup" id="channel-setup">
{_channel_setup_head("facebook", "连接 Facebook Page 的 Messenger 私信")}
<form class="channel-form" method="post" action="/admin/oauth/meta/start">{oauth_fields()}<input type="hidden" name="platform" value="facebook">
<dl class="channel-meta"><dt>Callback URI</dt><dd><code>{html.escape(meta_callback)}</code></dd><dt>权限</dt><dd>pages_show_list · pages_messaging · pages_manage_metadata{" · pages_read_engagement · pages_read_user_content · pages_manage_engagement" if facebook_comments else ""}</dd></dl>
<button class="btn-block">继续使用 Facebook 登录</button></form>
<details class="advanced-connect"><summary>高级连接：使用已有 Page Token</summary><div class="advanced-body"><form method="post" action="/admin/connect/meta">{common}<input type="hidden" name="platform" value="facebook">{_input("external_account_id", "Facebook Page ID")}{_input("access_token", "Page Access Token", secret=True)}{_input("app_secret", "Meta App Secret", secret=True)}{_input("app_id", "Meta App ID", required=False)}{_input("app_public_id", "Existing App Public ID", required=False)}{_input("verify_token", "Webhook Verify Token", secret=True)}{facebook_policy_fields}<button class="btn-block">连接 Facebook</button></form></div></details></section>"""
    elif selected_channel == "instagram":
        selected_panel = f"""<section class="channel-setup" id="channel-setup">
{_channel_setup_head("instagram", "选择 Instagram 专业账号的登录方式")}
<div class="channel-mode-grid"><div class="channel-mode"><h3>Instagram 登录</h3><p class="hint">不需要关联 Facebook Page</p><form method="post" action="/admin/oauth/instagram/start">{oauth_fields()}<dl class="channel-meta"><dt>Callback URI</dt><dd><code>{html.escape(instagram_callback)}</code></dd>{"<dt>评论权限</dt><dd>instagram_business_manage_comments</dd>" if instagram_comments else ""}</dl><button class="btn-block">继续使用 Instagram 登录</button></form></div>
<div class="channel-mode"><h3>Facebook 登录</h3><p class="hint">适用于已关联 Facebook Page 的专业账号</p><form method="post" action="/admin/oauth/meta/start">{oauth_fields()}<input type="hidden" name="platform" value="instagram"><dl class="channel-meta"><dt>Callback URI</dt><dd><code>{html.escape(meta_callback)}</code></dd>{"<dt>评论权限</dt><dd>pages_read_engagement · instagram_manage_comments</dd>" if instagram_comments else ""}</dl><button class="btn-block">继续使用 Facebook 登录</button></form></div></div>
<details class="advanced-connect"><summary>高级连接：使用已有 Page Token</summary><div class="advanced-body"><form method="post" action="/admin/connect/meta">{common}<input type="hidden" name="platform" value="instagram">{_input("external_account_id", "Instagram Professional Account ID")}{_input("page_id", "Facebook Page ID")}{_input("access_token", "Page Access Token", secret=True)}{_input("app_secret", "Meta App Secret", secret=True)}{_input("app_id", "Meta App ID", required=False)}{_input("app_public_id", "Existing App Public ID", required=False)}{_input("verify_token", "Webhook Verify Token", secret=True)}{instagram_policy_fields}<button class="btn-block">连接 Instagram</button></form></div></details></section>"""
    elif selected_channel == "telegram":
        selected_panel = f"""<section class="channel-setup" id="channel-setup">
{_channel_setup_head("telegram", "连接 Telegram Bot")}
<form class="channel-form" method="post" action="/admin/connect/telegram">{common}{_input("token", "Bot Token", secret=True)}<p class="hint">Token 由 @BotFather 创建 Bot 后提供。</p><button class="btn-block">连接 Telegram</button></form></section>"""
    elif selected_channel == "whatsapp":
        selected_panel = f"""<section class="channel-setup" id="channel-setup">
{_channel_setup_head("whatsapp", "连接 WhatsApp Cloud API 号码")}
<form class="channel-form" method="post" action="/admin/connect/whatsapp">{common}{_input("external_account_id", "Phone Number ID")}{_input("access_token", "Access Token", secret=True)}{_input("app_secret", "Meta App Secret", secret=True)}{_input("app_id", "Meta App ID", required=False)}{_input("app_public_id", "Existing App Public ID", required=False)}{_input("verify_token", "Webhook Verify Token", secret=True)}<button class="btn-block">连接 WhatsApp</button></form></section>"""
    elif selected_channel == "feishu":
        selected_panel = f"""<section class="channel-setup" id="channel-setup">
{_channel_setup_head("feishu", "连接企业自建应用 Bot")}
<form class="channel-form" method="post" action="/admin/connect/feishu">{common}{_input("app_id", "App ID")}{_input("app_secret", "App Secret", secret=True)}{_input("verification_token", "Verification Token", secret=True)}{_input("encrypt_key", "Encrypt Key", secret=True)}<input type="hidden" name="api_base_url" value="{FEISHU_API_BASE_URL}"><input type="hidden" name="group_mode" value="{FEISHU_GROUP_MODE}"><input type="hidden" name="automation_default" value="BOT_DRAFT_ONLY"><button class="btn-block">连接 Feishu</button></form></section>"""
    elif selected_channel == "email":
        selected_panel = f"""<section class="channel-setup" id="channel-setup">
{_channel_setup_head("email", "连接收发邮箱")}
<form class="channel-form" method="post" action="/admin/connect/email">{common}<div class="channel-form-grid"><div>{_input("email_address", "Email Address", input_type="email", autocomplete="email")}</div><div>{_input("from_name", "From Name（可选）", required=False)}</div><div>{_input("username", "Username", autocomplete="username")}</div><div>{_input("password", "Password", secret=True, autocomplete="current-password")}</div><div>{_input("imap_host", "IMAP Host", value="imap.larksuite.com")}</div><div>{_input("imap_port", "IMAP Port", value="993", input_type="number", inputmode="numeric", min=1, max=65535)}</div><div>{_input("smtp_host", "SMTP Host", value="smtp.larksuite.com")}</div><div>{_input("smtp_port", "SMTP Port（留空按加密方式默认）", required=False, input_type="number", inputmode="numeric", min=1, max=65535)}</div><div><label for="f-email-smtp-security">SMTP Security</label><select id="f-email-smtp-security" name="smtp_security" required><option value="ssl" selected>SSL（默认 465）</option><option value="starttls">STARTTLS（默认 587）</option></select></div><div>{_input("mailbox", "Mailbox", value="INBOX")}</div><div class="span-2"><label for="f-email-domain-policy">同域内部邮件</label><select id="f-email-domain-policy" name="internal_domain_policy" required><option value="ignore" selected>忽略（推荐）</option><option value="allow">允许进入处理流程</option></select><p class="hint">默认忽略同域来信，以降低自动回复循环风险。</p></div></div><input type="hidden" name="automation_default" value="BOT_DRAFT_ONLY"><button class="btn-block">连接 Email</button></form></section>"""
    account_card = f"""<section class="card"><h2>平台账号</h2><div class="tablewrap"><table><thead><tr><th>平台</th><th>名称</th><th>状态</th><th>消息通道</th><th>账号自动化策略</th><th>急停</th><th>操作</th></tr></thead><tbody>{account_rows}</tbody></table></div></section>"""
    jobs_card = f"""<section class="card"><h2>Provisioning Jobs</h2><p class="hint">最近 20 条接入任务。</p><div class="tablewrap"><table><thead><tr><th>ID</th><th>平台</th><th>状态</th><th>步骤</th><th>错误</th></tr></thead><tbody>{job_rows}</tbody></table></div></section>"""
    if principal.is_superadmin:
        body = f"""<h1>平台账号</h1><p class="lede">连接渠道、查看账号健康、调整账号级自动化策略与接入任务。</p>
{oauth_banner}{channel_picker}{selected_panel}{account_card}{jobs_card}"""
    else:
        body = f"""<h1>平台账号授权</h1><p class="lede">授权并管理当前 Tenant 的平台账号。</p>
{oauth_banner}{channel_picker}{selected_panel}{account_card}{jobs_card}"""
    response = HTMLResponse(
        _page("平台账号", body, active="accounts", show_users=principal.is_superadmin)
    )
    return _ensure_csrf(response, request, csrf)


@router.get("/system/safety", response_class=HTMLResponse)
async def safety_page(request: Request) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    if not principal.is_superadmin:
        raise HTTPException(status_code=403, detail="superadmin_required")
    csrf = _csrf(request)
    settings = get_settings()
    tenants = sorted(principal.allowed_tenants)
    redis = aioredis.from_url(settings.redis_url)
    try:
        flags = await redis.mget([f"killswitch:global:{tenant}" for tenant in tenants])
    finally:
        await redis.aclose()
    controls = "".join(
        f'<form class="card" method="post" action="/admin/killswitch/toggle">'
        f'<input type="hidden" name="csrf_token" value="{csrf}">'
        '<input type="hidden" name="scope" value="global">'
        f'<input type="hidden" name="tenant_id" value="{html.escape(tenant)}">'
        f'<h2>{html.escape(tenant)}</h2>'
        '<p class="hint">启用后自动回复降级为草稿；人工处理和持久化工作流继续运行。</p>'
        f'<button class="{"btn-ghost" if flags[index] else "btn-danger"}">'
        f'{"解除全局急停" if flags[index] else "启用全局急停"}</button></form>'
        for index, tenant in enumerate(tenants)
    )
    body = f"""<h1>安全控制</h1><p class="lede">集中管理租户级全局急停。账号级控制仍位于平台账号页。</p>
<div class="grid">{controls}</div>"""
    response = HTMLResponse(
        _page("安全控制", body, active="safety", show_users=True)
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
        if not get_settings().automation_default_allowed(account.platform, target):
            raise HTTPException(status_code=422, detail="automation_default_not_allowed")
        previous = account.automation_default
        account.automation_default = target
        if previous != target:
            await session.execute(
                models.AuditLog.__table__.insert().values(
                    tenant_id=account.tenant_id,
                    category="admin_action",
                    actor=principal.actor,
                    action="SET_AUTOMATION_DEFAULT",
                    subject_type="platform_account",
                    subject_id=str(account_id),
                    detail={"from": previous, "to": target, "platform": account.platform},
                )
            )
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
    target = "/admin/system/safety" if scope == "global" else "/admin/integrations/accounts"
    return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)
