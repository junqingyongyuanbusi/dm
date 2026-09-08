"""运营后台控制台：总览 / 对话 / 决策 / 知识库 / 投递 / 账号与急停。

与 admin.py 共享服务端会话与 CSRF；全部查询和写操作按当前 Principal 租户范围过滤。
"""

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import NoReturn
from urllib.parse import quote, urlencode

import redis.asyncio as aioredis
from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import and_, desc, func, or_, select

from social_reply.application.account_management.access import account_read_condition
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
from social_reply.application.account_management.auth import Principal, current_principal
from social_reply.application.account_management.channel_management import (
    ChannelActor,
    ChannelConflictError,
    ChannelManagementError,
    ChannelNotFoundError,
    ChannelPermissionError,
    repair_channel_xchat,
    set_channel_account_automation,
    set_channel_account_kill_switch,
)
from social_reply.application.account_management.human_workflow import (
    HumanWorkflowConflict,
    HumanWorkflowError,
    claim_human_work_item,
    require_work_conversation_tenant,
    resolve_human_work_item,
    resume_bot,
    send_human_reply,
    start_human_reception,
    transfer_human_work_item,
)
from social_reply.application.account_management.jobs import provisioning_job_is_in_flight
from social_reply.application.account_management.oauth.common import notice
from social_reply.application.account_management.reply_prompt_policy import (
    ReplyBusinessPromptConflict,
    ReplyBusinessPromptScopeError,
)
from social_reply.application.account_management.reply_prompt_trial import (
    ReplyBusinessPromptTrialExecutionError,
    ReplyBusinessPromptTrialRateLimited,
    ReplyBusinessPromptTrialUnavailable,
    ReplyBusinessPromptTrialValidationError,
    run_reply_business_prompt_trial,
)
from social_reply.application.account_management.reply_prompt_web import (
    RollbackReplyBusinessPromptCommand,
    SaveReplyBusinessPromptCommand,
    execute_rollback_reply_business_prompt,
    execute_save_reply_business_prompt,
)
from social_reply.application.account_management.saas_ui import (
    escape,
    render_saas_page,
    secondary_action,
    status_badge,
)
from social_reply.application.account_management.system_user_management import (
    SystemUserAuthenticationError,
    SystemUserValidationError,
    require_bootstrap_reauthentication,
)
from social_reply.application.account_management.ui_i18n import translate
from social_reply.application.account_management.xchat_activation import XChatActivationError
from social_reply.application.knowledge.authorization import KnowledgeAuthorizationError
from social_reply.application.knowledge.commands import (
    KnowledgeApplicationError,
    KnowledgeConflictError,
    KnowledgeNotFoundError,
)
from social_reply.application.message_delivery.contracts import (
    build_direct_reply_destination,
)
from social_reply.application.message_delivery.intents import (
    OutboxIdempotencyConflict,
    OutboxIntentError,
)
from social_reply.application.message_delivery.recovery import (
    DeliveryRecoveryConflict,
    DeliveryRecoveryNotFound,
    DeliveryRecoveryValidationError,
    retry_failed_outbox,
)
from social_reply.application.reply_review.queries import reviewable_draft_condition
from social_reply.application.reply_review.service import (
    DraftReviewConflict,
    DraftReviewNotFound,
    DraftReviewValidationError,
)
from social_reply.application.reply_review.service import (
    approve_draft as approve_draft_review,
)
from social_reply.application.reply_review.service import (
    reject_draft as reject_draft_review,
)
from social_reply.connectors.feishu.contracts import FEISHU_API_BASE_URL, FEISHU_GROUP_MODE
from social_reply.domain.automation.state_machine import (
    AutomationStateEnum,
    can_transition,
)
from social_reply.domain.platform_accounts import capability_text_limit
from social_reply.domain.reply.business_prompt import BusinessPromptValidationError
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.shared.config import DEFAULT_TENANT_ID, get_settings

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


def _channel_management_http_error(exc: ChannelManagementError) -> HTTPException:
    if isinstance(exc, ChannelNotFoundError):
        return HTTPException(status_code=404, detail=exc.code)
    if isinstance(exc, ChannelConflictError):
        return HTTPException(status_code=409, detail=exc.code)
    if isinstance(exc, ChannelPermissionError):
        return HTTPException(status_code=403, detail=exc.code)
    return HTTPException(status_code=422, detail=exc.code)


def _fmt(dt: datetime | None) -> str:
    return dt.strftime("%m-%d %H:%M") if dt else "—"


async def _legacy_tenant_get_redirect(request: Request, suffix: str = "") -> Response:
    principal = await current_principal(request)
    if principal is None:
        return RedirectResponse("/auth/login", status_code=status.HTTP_303_SEE_OTHER)
    if principal.must_change_password:
        return RedirectResponse(
            "/auth/change-password",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    principal.require_tenant_admin()
    tenant_id = principal.tenant_id or sorted(principal.allowed_tenants)[0]
    if tenant_id != DEFAULT_TENANT_ID:
        raise HTTPException(status_code=404, detail="tenant_workspace_not_found")
    canonical_target = f"/app/t/{quote(tenant_id, safe='')}{suffix}"
    if request.url.query:
        canonical_target = f"{canonical_target}?{request.url.query}"
    return RedirectResponse(
        canonical_target,
        status_code=status.HTTP_303_SEE_OTHER,
    )


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


_REASON_MESSAGE_KEYS = {
    "RISK_WORD": "admin.reason.risk_word",
    "OPENAI": "admin.reason.openai",
    "EMPTY_OR_NON_TEXT": "admin.reason.empty_or_non_text",
    "INSUFFICIENT_KNOWLEDGE": "admin.reason.insufficient_knowledge",
    "LLM_REFUSAL": "admin.reason.llm_refusal",
    "LLM_SCHEMA_FAIL": "admin.reason.llm_schema_fail",
    "LLM_UNAVAILABLE": "admin.reason.llm_unavailable",
    "GUARD_PII_LEAK": "PII Guard",
    "GUARD_TOO_LONG": "admin.reason.guard_too_long",
    "CAPABILITY_NOT_ALLOWED": "admin.reason.capability_not_allowed",
    "CAPABILITY_TEXT_TOO_LONG": "admin.reason.capability_text_too_long",
    "DELIVERY_WINDOW_EXPIRED": "admin.reason.delivery_window_expired",
    "UNSUPPORTED_ATTACHMENT": "admin.reason.unsupported_attachment",
    "AMBIGUOUS_SEND": "admin.reason.ambiguous_send",
}


def _reason_label(code: str | None) -> str:
    if not code:
        return "—"
    message_key = _REASON_MESSAGE_KEYS.get(code)
    if message_key is None:
        return code
    return translate(message_key) if message_key.startswith("admin.") else message_key


def _target_label(target: dict | None) -> str:
    value = dict(target or {})
    kind = str(value.get("kind") or "dm")
    label_keys = {
        "dm": "admin.target.dm",
        "x_chat": "admin.target.x_chat",
        "comment": "admin.target.comment",
        "reply": "admin.target.reply",
        "session_message": "admin.target.session_message",
    }
    label = translate(label_keys[kind]) if kind in label_keys else kind
    return f"{label} · {json.dumps(value, ensure_ascii=False, sort_keys=True)}"


def _attachment_text(attachment: object) -> str:
    if not isinstance(attachment, dict):
        return translate("admin.attachment.default")
    media_type = str(
        attachment.get("type")
        or attachment.get("media_type")
        or attachment.get("mime_type")
        or translate("admin.attachment.default")
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


def _aria_current(is_current: bool) -> str:
    return ' aria-current="page"' if is_current else ""


_CHANNEL_FILTERS = {"all", "dm", "comment"}
_CONVERSATION_CHANNEL_MESSAGE_KEYS = {
    "dm": "admin.channel.dm",
    "comment": "admin.channel.comment",
    "mention": "admin.channel.mention",
}


def _channel_label(channel_type: str) -> str:
    message_key = _CONVERSATION_CHANNEL_MESSAGE_KEYS.get(channel_type)
    return translate(message_key) if message_key is not None else channel_type


def _channel_condition(channel: str):
    if channel == "dm":
        return models.Conversation.channel_type == "dm"
    if channel == "comment":
        return models.Conversation.channel_type.in_(("comment", "mention"))
    return None


def _account_read_scope(
    principal: Principal,
    tenants: frozenset[str],
    tenant_id: str = "",
):
    scope_tenants = (tenant_id,) if tenant_id else tuple(sorted(tenants))
    conditions = tuple(account_read_condition(principal, tenant) for tenant in scope_tenants)
    return or_(*conditions) if conditions else models.PlatformAccount.id.is_(None)


def _scope_inbox_statement(
    statement,
    *,
    tenant_column,
    tenants: frozenset[str],
    tenant_id: str,
    account_id: uuid.UUID | None,
    platform: str,
    channel: str,
    principal: Principal | None = None,
):
    statement = statement.where(
        tenant_column.in_(tenants),
        models.Conversation.tenant_id == tenant_column,
        models.PlatformAccount.tenant_id == tenant_column,
    )
    if principal is not None:
        statement = statement.where(_account_read_scope(principal, tenants, tenant_id))
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
    principal: Principal | None = None,
) -> dict[str, tuple[int, datetime | None]]:
    scope = {
        "tenants": tenants,
        "tenant_id": tenant_id,
        "account_id": account_id,
        "platform": platform,
        "channel": channel,
        "principal": principal,
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
        .where(reviewable_draft_condition())
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
    tenant_options = f'<option value="">{translate("admin.common.all_tenants")}</option>' + "".join(
        f'<option value="{html.escape(value)}"{_selected(tenant_id, value)}>{html.escape(value)}</option>'
        for value in sorted(principal.allowed_tenants)
    )
    account_options = (
        f'<option value="">{translate("admin.common.all_accounts")}</option>'
        + "".join(
            f'<option value="{account.id}"{_selected(account_id, str(account.id))}>{html.escape(account.name)}</option>'
            for account in accounts
            if not tenant_id or account.tenant_id == tenant_id
        )
    )
    platform_options = (
        f'<option value="">{translate("admin.common.all_platforms")}</option>'
        + "".join(
            f'<option value="{value}"{_selected(platform, value)}>{html.escape(value)}</option>'
            for value in _ADMIN_PLATFORMS
        )
    )
    status_values = {
        "human": ("WAITING", "CLAIMED", "RESOLVED", "CANCELLED"),
        "drafts": ("PENDING", "ACCEPTED", "EDITED", "REJECTED"),
        "delivery": ("FAILED", "NEEDS_REVIEW"),
    }[queue]
    status_options = (
        f'<option value="">{translate("admin.common.all_statuses")}</option>'
        + "".join(
            f'<option value="{value}"{_selected(queue_status, value)}>{value}</option>'
            for value in status_values
        )
    )
    return f"""<form class="filters" method="get" action="/admin/inbox">
<input type="hidden" name="queue" value="{queue}">
<input type="hidden" name="channel" value="{channel}">
<div><label for="inbox-tenant">Tenant</label><select id="inbox-tenant" name="tenant_id">{tenant_options}</select></div>
<div><label for="inbox-account">{translate("admin.inbox.account_filter")}</label><select id="inbox-account" name="account_id">{account_options}</select></div>
<div><label for="inbox-platform">{translate("admin.inbox.platform_filter")}</label><select id="inbox-platform" name="platform">{platform_options}</select></div>
<div><label for="inbox-status">{translate("admin.inbox.status_filter")}</label><select id="inbox-status" name="status">{status_options}</select></div>
<div><label for="inbox-reason">{translate("admin.inbox.reason_code")}</label><input id="inbox-reason" name="reason" value="{html.escape(reason, quote=True)}" maxlength="128" placeholder="{translate("admin.common.all_reasons")}"></div>
<button>{translate("admin.common.filter")}</button></form>"""


def _conversation_filter_form(
    *,
    principal: Principal,
    accounts: list[models.PlatformAccount],
    tenant_id: str,
    account_id: str,
    platform: str,
    channel: str,
) -> str:
    tenant_options = f'<option value="">{translate("admin.common.all_tenants")}</option>' + "".join(
        f'<option value="{html.escape(value)}"{_selected(tenant_id, value)}>{html.escape(value)}</option>'
        for value in sorted(principal.allowed_tenants)
    )
    account_options = (
        f'<option value="">{translate("admin.common.all_accounts")}</option>'
        + "".join(
            f'<option value="{account.id}"{_selected(account_id, str(account.id))}>{html.escape(account.name)}</option>'
            for account in accounts
            if not tenant_id or account.tenant_id == tenant_id
        )
    )
    platform_options = (
        f'<option value="">{translate("admin.common.all_platforms")}</option>'
        + "".join(
            f'<option value="{value}"{_selected(platform, value)}>{html.escape(value)}</option>'
            for value in _ADMIN_PLATFORMS
        )
    )
    return f"""<form class="filters" method="get" action="/admin/conversations">
<input type="hidden" name="channel" value="{channel}">
<div><label for="conversation-tenant">Tenant</label><select id="conversation-tenant" name="tenant_id">{tenant_options}</select></div>
<div><label for="conversation-account">{translate("admin.inbox.account_filter")}</label><select id="conversation-account" name="account_id">{account_options}</select></div>
<div><label for="conversation-platform">{translate("admin.inbox.platform_filter")}</label><select id="conversation-platform" name="platform">{platform_options}</select></div>
<button>{translate("admin.common.filter")}</button></form>"""


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
        return translate("admin.time.just_now")
    minutes = seconds // 60
    if minutes < 60:
        return translate("admin.time.minutes", count=minutes)
    hours = minutes // 60
    if hours < 48:
        return translate("admin.time.hours", count=hours)
    return translate("admin.time.days", count=hours // 24)


def _elapsed(started_at: datetime, finished_at: datetime | None) -> str:
    if finished_at is None:
        return "—"
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    if finished_at.tzinfo is None:
        finished_at = finished_at.replace(tzinfo=UTC)
    seconds = max(int((finished_at - started_at).total_seconds()), 0)
    if seconds < 60:
        return translate("admin.time.seconds", count=seconds)
    minutes = seconds // 60
    if minutes < 60:
        return translate("admin.time.minutes", count=minutes)
    hours = minutes // 60
    return translate("admin.time.hours_minutes", hours=hours, minutes=minutes % 60)


async def _load_health_metrics(
    session,
    tenants: frozenset[str],
    now: datetime,
    *,
    principal: Principal | None = None,
) -> list[_HealthMetric]:
    if principal is not None:
        principal.require_superadmin()
    tenant_id = sorted(tenants)[0]
    tenant_root = f"/app/t/{quote(tenant_id, safe='')}"
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
    decision_warning = models.DecisionJob.status.in_(("PENDING", "PROCESSING", "FAILED"))
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
            .where(
                models.PlatformAccount.tenant_id.in_(tenants),
                _account_read_scope(principal, tenants)
                if principal is not None
                else models.PlatformAccount.tenant_id.in_(tenants),
            )
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
                _account_read_scope(principal, tenants)
                if principal is not None
                else models.PlatformAccount.tenant_id.in_(tenants),
            )
        )
    ).one()

    account_action = models.PlatformAccount.status == "DISABLED"
    account_row = (
        await session.execute(
            select(
                func.count().filter(account_action),
                func.min(models.PlatformAccount.created_at).filter(account_action),
            ).where(
                models.PlatformAccount.tenant_id.in_(tenants),
                _account_read_scope(principal, tenants)
                if principal is not None
                else models.PlatformAccount.tenant_id.in_(tenants),
            )
        )
    ).one()

    return [
        _HealthMetric(
            "ingestion",
            translate("admin.health.metric.ingestion"),
            *raw_row,
            f"{tenant_root}/health#ingress",
        ),
        _HealthMetric(
            "decisions",
            translate("admin.health.metric.decisions"),
            *decision_row,
            f"{tenant_root}/inbox?queue=drafts",
        ),
        _HealthMetric(
            "delivery",
            translate("admin.health.metric.delivery"),
            *outbox_row,
            f"{tenant_root}/inbox?queue=delivery",
        ),
        _HealthMetric(
            "provisioning",
            translate("admin.health.metric.provisioning"),
            *provisioning_row,
            f"{tenant_root}/channels",
        ),
        _HealthMetric(
            "sync",
            translate("admin.health.metric.sync"),
            *sync_row,
            f"{tenant_root}/channels",
        ),
        _HealthMetric(
            "accounts",
            translate("admin.health.metric.accounts"),
            int(account_row[0]),
            0,
            account_row[1],
            f"{tenant_root}/channels",
        ),
    ]


@router.get("", response_class=HTMLResponse)
async def overview(request: Request) -> Response:
    principal = await current_principal(request)
    if principal is not None and not principal.must_change_password and principal.is_superadmin:
        return RedirectResponse(
            "/admin/system/overview",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    return await _legacy_tenant_get_redirect(request)

    # Legacy implementation retained below while prior POST adapters are still mounted.
    principal = await _web_principal(request)
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
        health_metrics = await _load_health_metrics(session, tenants, now, principal=principal)

    auto = action_counts.get("auto_reply", 0)
    handled = auto + action_counts.get("draft", 0) + action_counts.get("handoff", 0)
    deflection = f"{auto / handled * 100:.0f}%" if handled else "—"
    sent = outbox_counts.get("SENT", 0)
    failed = outbox_counts.get("FAILED", 0) + outbox_counts.get("NEEDS_REVIEW", 0)
    send_total = sent + failed
    send_rate = f"{sent / send_total * 100:.0f}%" if send_total else "—"

    stats = f"""<div class="stats">
<div class="stat"><div class="num">{msg_today}</div><div class="lbl">{translate("admin.overview.messages_today")}</div></div>
<div class="stat"><div class="num">{deflection}</div><div class="lbl">{translate("admin.overview.automation_rate")}</div></div>
<div class="stat"><div class="num">{conv_total}</div><div class="lbl">{translate("admin.overview.conversations_total")}</div></div>
<div class="stat"><div class="num">{human_active}</div><div class="lbl">{translate("admin.overview.human_active")}</div></div>
<div class="stat"><div class="num">{send_rate}</div><div class="lbl">{translate("admin.overview.delivery_rate")}</div></div>
</div>"""

    total_actions = sum(action_counts.values()) or 1
    tone_map = {"auto_reply": "ok", "draft": "warn", "handoff": "err", "ignore": "neutral"}
    label_map = {
        "auto_reply": translate("status.auto_reply"),
        "draft": translate("status.draft"),
        "handoff": translate("status.handoff"),
        "ignore": translate("status.ignore"),
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
        f"<td>{translate('admin.health.backlog_summary', action_count=metric.action_count, warning_count=metric.warning_count)}</td>"
        f"<td class='muted'>{_health_age(now, metric.oldest_at)}</td>"
        f"<td><a href='{metric.href}'>{translate('admin.common.view')}</a></td></tr>"
        for metric in health_metrics
    )
    health = f"""<section class="card"><h2>{translate("admin.overview.runtime_health")}</h2><p class="hint">{translate("admin.overview.runtime_health_description")}</p>
<div class="tablewrap"><table><thead><tr><th>{translate("admin.overview.stage")}</th><th>{translate("common.status")}</th><th>{translate("admin.overview.backlog")}</th><th>{translate("admin.overview.oldest_wait")}</th><th></th></tr></thead><tbody>{health_rows}</tbody></table></div></section>"""
    recent_rows = (
        "".join(
            f"<tr><td class='muted'>{_fmt(d.created_at)}</td><td>{_pill(d.action)}</td>"
            f"<td class='muted'>{html.escape(d.intent or '—')}</td>"
            f"<td>{html.escape((d.reply_text or '—')[:46])}</td>"
            f"<td><a href='/admin/conversations/{d.conversation_id}'>{translate('admin.overview.open_conversation')}</a></td></tr>"
            for d in recent
        )
        or f"<tr><td colspan='5' class='muted'>{translate('admin.overview.no_decisions')}</td></tr>"
    )
    page_title = translate("admin.overview.title")
    body = f"""<h1>{page_title}</h1><p class="lede">{translate("admin.overview.description")}</p>{stats}{health}
<div class="grid" style="grid-template-columns:1fr 1.4fr">
<section class="card"><h2>{translate("admin.overview.decision_distribution")}</h2><p class="hint">{translate("admin.overview.decision_distribution_description")}</p>{bars}</section>
<section class="card"><h2>{translate("admin.overview.recent_decisions")}</h2><p class="hint">{translate("admin.overview.recent_decisions_description")}</p><div class="tablewrap"><table><thead><tr><th>{translate("common.time")}</th><th>{translate("common.action")}</th><th>{translate("admin.overview.intent")}</th><th>{translate("admin.overview.reply_preview")}</th><th></th></tr></thead><tbody>{recent_rows}</tbody></table></div></section>
</div>"""
    return HTMLResponse(
        _page(
            page_title,
            body,
            active="overview",
            show_users=principal.is_admin,
            principal=principal,
        )
    )


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
        f"{translate('admin.inbox.waiting')} {_health_age(now, decision.created_at)} · "
        f'<a href="/admin/conversations/{conversation.id}">{translate("admin.inbox.review_context")}</a></p>'
    )
    if review_action == "PENDING":
        expected_generation = (
            str(decision.decision_generation) if decision.decision_generation is not None else ""
        )
        controls = f"""<form method="post" action="/admin/decisions/{decision.id}/approve"><input type="hidden" name="csrf_token" value="{csrf}"><input type="hidden" name="expected_generation" value="{expected_generation}"><input type="hidden" name="expected_review_action" value="PENDING"><label for="draft-{decision.id}">{translate("admin.inbox.reply_content")}</label><textarea id="draft-{decision.id}" name="final_reply_text" required maxlength="10000">{html.escape(original_text)}</textarea><button class="btn-sm">{translate("admin.inbox.send")}</button></form>
<form method="post" action="/admin/decisions/{decision.id}/discard"><input type="hidden" name="csrf_token" value="{csrf}"><input type="hidden" name="expected_generation" value="{expected_generation}"><input type="hidden" name="expected_review_action" value="PENDING"><label for="reject-{decision.id}">{translate("admin.inbox.rejection_reason")}</label><input id="reject-{decision.id}" name="review_reason" maxlength="500" required><button class="btn-sm btn-ghost">{translate("admin.inbox.reject")}</button></form>"""
        return f"{heading}{controls}</section>"

    final_text = decision.final_reply_text or "—"
    review_reason = decision.review_reason or "—"
    reviewed_by = decision.reviewed_by or "—"
    return f"""{heading}<dl class="channel-meta"><dt>{translate("admin.inbox.original_draft")}</dt><dd>{html.escape(original_text or "—")}</dd>
<dt>{translate("admin.inbox.final_reply")}</dt><dd>{html.escape(final_text)}</dd><dt>{translate("admin.inbox.reviewer")}</dt><dd>{html.escape(reviewed_by)}</dd>
<dt>{translate("admin.inbox.review_time")}</dt><dd>{_fmt(decision.reviewed_at)}</dd><dt>{translate("admin.inbox.review_duration")}</dt><dd>{_elapsed(decision.created_at, decision.reviewed_at)}</dd>
<dt>{translate("admin.inbox.review_reason")}</dt><dd>{html.escape(review_reason)}</dd></dl></section>"""


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
                    _account_read_scope(principal, principal.allowed_tenants, tenant_id),
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
            principal=principal,
        )
    return JSONResponse({key: value[0] for key, value in summary.items()})


@router.get("/inbox", response_class=HTMLResponse)
async def inbox_page(request: Request) -> Response:
    return await _legacy_tenant_get_redirect(request, "/inbox")

    # Legacy implementation retained below while prior POST adapters are still mounted.
    principal = await _web_principal(request)
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
                    .where(
                        models.PlatformAccount.tenant_id.in_(tenants),
                        _account_read_scope(principal, tenants, tenant_id),
                    )
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
            principal=principal,
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
                statement = statement.where(reviewable_draft_condition())
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
            f'<a class="queue-tab{" active" if queue == key else ""}"{_aria_current(queue == key)} href="/admin/inbox?{urlencode({"queue": key, **shared_query})}">'
            f"<strong>{summary[key][0]}</strong><span>{label} · {translate('admin.inbox.oldest', age=_health_age(now, summary[key][1]))}</span></a>"
            for key, label in (
                ("human", translate("admin.inbox.queue.human")),
                ("drafts", translate("admin.inbox.queue.drafts")),
                ("delivery", translate("admin.inbox.queue.delivery")),
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
        f'<div class="chips" aria-label="{translate("admin.channel.interaction_type")}">'
        + "".join(
            f'<a class="chip{" active" if channel == value else ""}"{_aria_current(channel == value)} href="/admin/inbox?{urlencode({**channel_query, "channel": value})}">{label}</a>'
            for value, label in (
                ("all", translate("common.all")),
                ("dm", translate("admin.channel.dm")),
                ("comment", translate("admin.channel.comments_mentions")),
            )
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
                f"<td><a href='/admin/conversations/{conv.id}'>{html.escape(display or external or translate('common.anonymous_contact'))}</a></td>"
                f"<td>{html.escape(platform_name)} · {html.escape(_channel_label(conv.channel_type))}<br><span class='muted'>{html.escape(account_name)}</span></td>"
                f"<td>{_pill(work.status)}</td><td>{html.escape(_reason_label(work.reason_code))}</td>"
                f"<td class='muted'>{html.escape(work.assigned_actor or str(work.assigned_user_id or translate('admin.common.not_claimed')))}</td>"
                "<td>"
                + (
                    f'<form class="inline" method="post" action="/admin/work-items/{work.id}/claim"><input type="hidden" name="csrf_token" value="{csrf}"><input type="hidden" name="version" value="{work.version}"><button class="btn-sm">{translate("admin.inbox.claim_and_take_over")}</button></form>'
                    if work.status == "WAITING"
                    else ""
                )
                + f"<a href='/admin/conversations/{conv.id}'>{translate('admin.inbox.open')}</a>"
                + "</td></tr>"
                for work, conv, display, external, account_name, platform_name, _automation in items
            )
            or f"<tr><td colspan='7' class='muted'>{translate('admin.inbox.no_human_items')}</td></tr>"
        )
        content = f"<section class='card'><div class='tablewrap'><table><thead><tr><th>{translate('admin.inbox.waiting')}</th><th>{translate('common.contact')}</th><th>{translate('common.channel')}</th><th>{translate('common.status')}</th><th>{translate('admin.common.reason')}</th><th>{translate('admin.common.owner')}</th><th></th></tr></thead><tbody>{rows}</tbody></table></div></section>"
    elif queue == "drafts":
        cards = (
            "".join(
                _draft_review_card(
                    decision=decision,
                    conversation=conv,
                    display_name=display or external or translate("common.anonymous_contact"),
                    platform=platform_name,
                    channel_type=conv.channel_type,
                    account_name=account_name,
                    csrf=csrf,
                    now=now,
                )
                for decision, conv, display, external, account_name, platform_name in items
            )
            or f"<section class='card'><p class='muted'>{translate('admin.inbox.no_drafts')}</p></section>"
        )
        content = cards
    else:
        rows = (
            "".join(
                f"<tr><td class='muted'>{_health_age(now, outbox.created_at)}</td>"
                f"<td><a href='/admin/conversations/{conv.id}'>{html.escape(display or external or translate('common.anonymous_contact'))}</a></td>"
                f"<td>{html.escape(platform_name)} · {html.escape(_channel_label(conv.channel_type))}<br><span class='muted'>{html.escape(account_name)}</span></td>"
                f"<td>{_pill(outbox.status)}</td><td>{html.escape(_reason_label(outbox.last_error_code))}</td>"
                f"<td class='muted'>{html.escape(outbox.last_error_message or '—')}</td>"
                + (
                    f'<td><form class="inline" method="post" action="/admin/delivery/{outbox.id}/retry"><input type="hidden" name="csrf_token" value="{csrf}"><button class="btn-sm btn-ghost">{translate("button.retry")}</button></form></td>'
                    if outbox.status == "FAILED"
                    else f"<td><span class='muted'>{translate('admin.inbox.verify_provider_result')}</span></td>"
                )
                + "</tr>"
                for outbox, conv, display, external, account_name, platform_name in items
            )
            or f"<tr><td colspan='7' class='muted'>{translate('admin.inbox.no_delivery_issues')}</td></tr>"
        )
        content = f"<section class='card'><div class='tablewrap'><table><thead><tr><th>{translate('admin.inbox.waiting')}</th><th>{translate('common.contact')}</th><th>{translate('common.channel')}</th><th>{translate('common.status')}</th><th>{translate('admin.common.error')}</th><th>{translate('common.details')}</th><th></th></tr></thead><tbody>{rows}</tbody></table></div></section>"

    page_title = translate("inbox.title")
    body = f"""<h1>{page_title}</h1><p class="lede">{translate("admin.inbox.description")}</p>{queue_tabs}{channel_tabs}
<div class="toolbar">{filters}</div>{content}"""
    response = HTMLResponse(
        _page(
            page_title,
            body,
            active="inbox",
            refresh_seconds=0 if queue == "drafts" else 20,
            show_users=principal.is_admin,
            principal=principal,
        )
    )
    return _ensure_csrf(response, request, csrf)


# ---------- Conversations ----------


@router.get("/conversations", response_class=HTMLResponse)
async def conversations_page(request: Request) -> Response:
    return await _legacy_tenant_get_redirect(request, "/conversations")

    # Legacy implementation retained below while conversation-detail adapters remain mounted.
    principal = await _web_principal(request)
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
                    .where(
                        models.PlatformAccount.tenant_id.in_(tenants),
                        _account_read_scope(principal, tenants, tenant_id),
                    )
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
                _account_read_scope(principal, tenants, tenant_id),
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
        f'<div class="chips" aria-label="{translate("admin.channel.interaction_type")}">'
        + "".join(
            f'<a class="chip{" active" if channel == value else ""}"{_aria_current(channel == value)} href="/admin/conversations?{urlencode({"channel": value, **shared_query})}">{label}</a>'
            for value, label in (
                ("all", translate("common.all")),
                ("dm", translate("admin.channel.dm")),
                ("comment", translate("admin.channel.comments_mentions")),
            )
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
            f"<td><a href='/admin/conversations/{conv.id}'>{html.escape(display or external or translate('common.anonymous_contact'))}</a></td>"
            f"<td class='muted'>{html.escape(account_name)}</td>"
            f"<td class='muted'>{_fmt(last_at or conv.created_at)}</td></tr>"
            for conv, display, external, account_name, last_at in rows
        )
        or f"<tr><td colspan='4' class='muted'>{translate('admin.conversations.empty')}</td></tr>"
    )
    page_title = translate("conversations.title")
    body = f"""<h1>{page_title}</h1><p class="lede">{translate("admin.conversations.description")}</p>{channel_tabs}
<div class="toolbar">{filters}</div>
<section class="card"><div class="tablewrap"><table><thead><tr><th>{translate("common.channel")}</th><th>{translate("common.contact")}</th><th>{translate("common.account")}</th><th>{translate("admin.conversations.last_active")}</th></tr></thead><tbody>{trs}</tbody></table></div></section>"""
    return HTMLResponse(
        _page(
            page_title,
            body,
            active="conversations",
            show_users=principal.is_admin,
            principal=principal,
        )
    )


_TRANSITION_MESSAGE_KEYS = {
    "HUMAN_ACTIVE": ("admin.conversation.transition.human", "btn-danger"),
    "BOT_ACTIVE": ("admin.conversation.transition.auto", ""),
    "BOT_DRAFT_ONLY": ("admin.conversation.transition.draft", "btn-ghost"),
    "BOT_COOLDOWN": ("admin.conversation.transition.cooldown", "btn-ghost"),
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
    return await _legacy_tenant_get_redirect(
        request,
        f"/conversations/{conversation_id}",
    )

    # Retained temporarily as unreachable reference code while legacy POST adapters are mounted.
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
                    _account_read_scope(principal, principal.allowed_tenants, conv.tenant_id),
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
    sender_labels = {
        "contact": translate("admin.conversation.sender.contact"),
        "agent": translate("admin.conversation.sender.agent"),
        "bot": translate("admin.conversation.sender.bot"),
    }
    bubbles = (
        "".join(
            f"<div class='msg {'in' if m.direction == 'inbound' else 'out'}'>"
            f"{html.escape(m.text or translate('admin.conversation.non_text_message'))}"
            + "".join(
                f"<div class='target-choice'>{html.escape(_attachment_text(attachment))}</div>"
                for attachment in (m.attachments or [])
            )
            + f"<div class='meta'>{sender_labels.get(m.sender_type, sender_labels['bot'])} · {_fmt(m.occurred_at or m.created_at)}</div></div>"
            for m in msgs
        )
        or f"<p class='muted'>{translate('admin.conversation.no_messages')}</p>"
    )
    cur = AutomationStateEnum(cur_state) if cur_state in AutomationStateEnum.__members__ else None
    buttons = "".join(
        f"""<form class="inline" method="post" action="/admin/conversations/{conversation_id}/state">
<input type="hidden" name="csrf_token" value="{csrf}"><input type="hidden" name="target" value="{dst}">
<input type="hidden" name="expect" value="{cur_state}"><button class="btn-sm {cls}">{translate(message_key)}</button></form>"""
        for dst, (message_key, cls) in _TRANSITION_MESSAGE_KEYS.items()
        if principal.is_admin
        and cur is not None
        and can_transition(cur, AutomationStateEnum(dst))
        and get_settings().automation_default_allowed(account.platform, dst)
        and (dst == "HUMAN_ACTIVE" or work_item is None)
    )
    decision_rows = (
        "".join(
            f"<tr><td class='muted'>{_fmt(d.created_at)}</td><td>{_pill(d.action)}</td>"
            f"<td class='muted'>{html.escape(d.intent or '—')}</td>"
            f"<td>{html.escape((d.final_reply_text or d.original_reply_text or d.reply_text or '—')[:60])}</td>"
            f"<td>{html.escape(', '.join(_reason_label(str(code)) for code in (d.reason_codes or [])) or '—')}</td>"
            f"<td class='muted'>{d.confidence if d.confidence is not None else '—'}</td></tr>"
            for d in decisions
        )
        or f"<tr><td colspan='6' class='muted'>{translate('admin.conversation.no_decisions')}</td></tr>"
    )
    who = html.escape(
        (contact.display_name if contact else None)
        or (contact.external_user_id if contact else "")
        or translate("common.anonymous_contact")
    )
    reply_candidates = [message for message in msgs if message.direction == "inbound"]
    reply_heading = (
        translate("admin.conversation.public_reply")
        if conv.channel_type in {"comment", "mention"}
        else translate("admin.conversation.private_reply")
    )
    text_limit = capability_text_limit(account.platform, dict(account.capability or {})) or 2000
    destination_label = "—"
    window_label = translate("admin.conversation.no_provider_deadline")
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
                    translate(
                        "admin.conversation.deadline",
                        time=_fmt(destination.valid_until),
                    )
                    if destination.valid_until > datetime.now(UTC)
                    else translate("admin.reason.delivery_window_expired")
                )
        except ValueError:
            destination_label = translate("admin.conversation.unsupported_direct_reply")

    target_choices = "".join(
        f"""<label class="target-choice"><input type="radio" name="reply_to_message_id" value="{message.id}" {"checked" if message is reply_candidates[-1] else ""} required>
{html.escape(_target_label(message.reply_target))}<br><span class="muted">{html.escape((message.text or translate("admin.conversation.non_text_preview"))[:90])} · {_fmt(message.occurred_at or message.created_at)}</span></label>"""
        for message in reply_candidates
    )
    work_status = work_item.status if work_item is not None else "NONE"
    assigned = (
        work_item.assigned_actor
        or str(work_item.assigned_user_id or translate("admin.common.not_claimed"))
        if work_item is not None
        else "—"
    )
    effective_policy = account.automation_default
    if not get_settings().automation_default_allowed(account.platform, effective_policy):
        effective_policy = "BOT_DRAFT_ONLY"
    policy_label = (
        translate("admin.conversation.policy.auto")
        if effective_policy == "BOT_ACTIVE"
        else translate("admin.conversation.policy.draft")
    )
    work_actions = ""
    if work_item is None and principal.user_id is not None:
        work_actions = f"""<form class="inline" method="post" action="/admin/conversations/{conversation_id}/start-reception"><input type="hidden" name="csrf_token" value="{csrf}"><button class="btn-sm">{translate("conversation.start_reception")}</button></form>"""
    elif work_item is not None and work_item.status == "WAITING":
        work_actions = f"""<form class="inline" method="post" action="/admin/work-items/{work_item.id}/claim"><input type="hidden" name="csrf_token" value="{csrf}"><input type="hidden" name="version" value="{work_item.version}"><button class="btn-sm">{translate("admin.inbox.claim_and_take_over")}</button></form>"""
    if (
        work_item is not None
        and work_item.status == "CLAIMED"
        and (principal.is_admin or work_item.assigned_actor == principal.actor)
    ):
        work_actions += f"""<form class="inline" method="post" action="/admin/work-items/{work_item.id}/resolve"><input type="hidden" name="csrf_token" value="{csrf}"><input type="hidden" name="version" value="{work_item.version}"><button class="btn-sm btn-ghost">{translate("admin.conversation.resolve_and_resume", policy=policy_label)}</button></form>"""
    if (
        principal.is_admin
        and work_item is not None
        and work_item.status == "RESOLVED"
        and cur_state
        in {
            "HANDOFF_PENDING",
            "HUMAN_ACTIVE",
            "BOT_COOLDOWN",
        }
    ):
        resume_auto = (
            f'<button class="btn-sm btn-ghost" name="target" value="BOT_ACTIVE">{translate("admin.conversation.resume_auto")}</button>'
            if get_settings().automation_default_allowed(account.platform, "BOT_ACTIVE")
            else ""
        )
        work_actions += f"""<form class="inline" method="post" action="/admin/conversations/{conversation_id}/resume"><input type="hidden" name="csrf_token" value="{csrf}"><button class="btn-sm" name="target" value="BOT_DRAFT_ONLY">{translate("admin.conversation.resume_draft")}</button>{resume_auto}</form>"""

    work_fields = ""
    if work_item is not None and work_item.status == "CLAIMED":
        work_fields = f'<input type="hidden" name="work_item_id" value="{work_item.id}"><input type="hidden" name="version" value="{work_item.version}">'
    can_handle = (
        work_item is not None
        and work_item.status == "CLAIMED"
        and work_item.assigned_actor == principal.actor
    )
    composer = (
        f"""<section class="card composer"><h2>{reply_heading}</h2>
<dl class="channel-meta"><dt>{translate("admin.conversation.reply_channel")}</dt><dd>{html.escape(destination_label)}</dd><dt>{translate("admin.conversation.delivery_window")}</dt><dd>{html.escape(window_label)}</dd><dt>{translate("admin.conversation.text_limit")}</dt><dd>{translate("admin.conversation.characters", count=text_limit)}</dd></dl>
<form method="post" action="/admin/conversations/{conversation_id}/reply"><input type="hidden" name="csrf_token" value="{csrf}"><input type="hidden" name="idempotency_key" value="{uuid.uuid4()}">{work_fields}
<fieldset><legend>{translate("admin.conversation.reply_target")}</legend>{target_choices}</fieldset><label for="manual-reply">{translate("admin.inbox.reply_content")}</label><textarea id="manual-reply" name="text" required maxlength="{text_limit}"></textarea>
<div class="composer-meta"><span>{translate("admin.conversation.sent_by_admin")}</span><span>{translate("admin.conversation.maximum_characters", count=text_limit)}</span></div><button>{translate("admin.conversation.send_reply")}</button></form></section>"""
        if reply_candidates and can_handle
        else f"<section class='card'><h2>{reply_heading}</h2><div class='banner err'>"
        + (
            translate("admin.conversation.claimed_elsewhere")
            if reply_candidates
            else translate("admin.conversation.no_safe_target")
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
        or f"<tr><td colspan='5' class='muted'>{translate('admin.conversation.no_send_records')}</td></tr>"
    )
    audit_items = (
        "".join(
            f"<li><strong>{html.escape(entry.action)}</strong> · {html.escape(entry.actor)}"
            f"<div class='muted'>{_fmt(entry.created_at)} · {html.escape(json.dumps(entry.detail or {}, ensure_ascii=False, sort_keys=True))}</div></li>"
            for entry in audit_logs
        )
        or f"<li class='muted'>{translate('admin.conversation.no_audit_records')}</li>"
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
    sidebar = f"""<section class="card"><h2>{translate("admin.conversation.handling_status")}</h2><table class="kv"><tbody>
<tr><th>Automation</th><td>{_pill(cur_state)}</td></tr><tr><th>{translate("admin.conversation.human_work_item")}</th><td>{_pill(work_status)}</td></tr>
<tr><th>{translate("admin.conversation.handoff_reason")}</th><td>{html.escape(_reason_label(handoff_reason))}</td></tr><tr><th>{translate("admin.common.owner")}</th><td>{html.escape(assigned)}</td></tr>
<tr><th>{translate("admin.conversation.wait_time")}</th><td>{_health_age(datetime.now(UTC), work_item.created_at) if work_item is not None else "—"}</td></tr>
<tr><th>{translate("admin.conversation.last_human_send")}</th><td>{_fmt(max(human_times, default=None))}</td></tr>
<tr><th>{translate("admin.conversation.last_bot_send")}</th><td>{_fmt(max(bot_times, default=None))}</td></tr>
</tbody></table><div style="margin-top:14px">{work_actions}{buttons}</div></section>
<section class="card"><h2>{translate("admin.conversation.audit_timeline")}</h2><ul class="audit-list">{audit_items}</ul></section>"""
    body = f"""<a class="back" href="/admin/inbox">← {translate("admin.conversation.back_to_inbox")}</a>
<header><h1>{who}</h1><p class="hint">{html.escape(conv.platform)} · {html.escape(_channel_label(conv.channel_type))} · {html.escape(account.name)}</p></header>
<div class="detail-grid"><div class="detail-stack"><section class="card"><h2>{translate("admin.conversation.message_thread")}</h2><div class="thread">{bubbles}</div></section>{composer}</div><aside>{sidebar}</aside></div>
<section class="card"><h2>{translate("admin.conversation.send_status")}</h2><div class="tablewrap"><table><thead><tr><th>{translate("common.time")}</th><th>{translate("common.status")}</th><th>{translate("admin.common.source")}</th><th>{translate("admin.common.content")}</th><th>{translate("admin.conversation.provider_error")}</th></tr></thead><tbody>{outbox_rows}</tbody></table></div></section>
<section class="card"><h2>{translate("admin.conversation.decisions")}</h2><div class="tablewrap"><table><thead><tr><th>{translate("common.time")}</th><th>{translate("common.action")}</th><th>{translate("admin.overview.intent")}</th><th>{translate("admin.common.reply")}</th><th>{translate("admin.common.reason")}</th><th>{translate("admin.conversation.confidence")}</th></tr></thead><tbody>{decision_rows}</tbody></table></div></section>"""
    page_title = translate("admin.conversation.title")
    response = HTMLResponse(
        _page(
            page_title,
            body,
            active="conversations",
            show_users=principal.is_admin,
            principal=principal,
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
            principal=principal,
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
            allow_override=principal.is_admin,
            user_id=principal.user_id,
            principal=principal,
        )
    except HumanWorkflowError as exc:
        raise _workflow_error(exc) from exc
    return RedirectResponse("/admin/inbox?queue=human", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/conversations/{conversation_id}/resume")
async def resume_conversation(request: Request, conversation_id: uuid.UUID) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    principal.require_tenant_admin()
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
            principal=principal,
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
        if work_item_id is None:
            raise HTTPException(status_code=409, detail="human_reception_required")
    try:
        await send_human_reply(
            conversation_id=conversation_id,
            reply_to_message_id=reply_to_message_id,
            text=text,
            idempotency_key=browser_key,
            allowed_tenants=principal.allowed_tenants,
            actor=principal.actor,
            user_id=principal.user_id,
            allow_override=False,
            work_item_id=work_item_id,
            expected_version=expected_version,
            principal=principal,
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


@router.post("/conversations/{conversation_id}/start-reception")
async def start_legacy_human_reception(request: Request, conversation_id: uuid.UUID) -> Response:
    principal = await _web_principal(request, require_admin=False)
    if isinstance(principal, Response):
        return principal
    principal.require_tenant(DEFAULT_TENANT_ID)
    form = await _form(request)
    _require_csrf(request, form)
    try:
        await start_human_reception(conversation_id=conversation_id, principal=principal)
    except (TypeError, ValueError, HumanWorkflowError) as exc:
        raise _workflow_error(exc) from exc
    return RedirectResponse(
        f"/admin/conversations/{conversation_id}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/work-items/{work_item_id}/transfer")
async def transfer_legacy_work_item(request: Request, work_item_id: uuid.UUID) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    principal.require_tenant_admin()
    form = await _form(request)
    _require_csrf(request, form)
    try:
        target_user_id = uuid.UUID(form.get("target_user_id") or "")
        expected_version = _expected_version(form)
        await transfer_human_work_item(
            work_item_id=work_item_id,
            allowed_tenants=principal.allowed_tenants,
            actor=principal.actor,
            user_id=principal.user_id,
            target_user_id=target_user_id,
            expected_version=expected_version,
            principal=principal,
        )
    except (TypeError, ValueError, HumanWorkflowError) as exc:
        raise _workflow_error(exc) from exc
    return RedirectResponse("/admin/inbox?queue=human", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/conversations/{conversation_id}/state")
async def flip_conversation_state(request: Request, conversation_id: uuid.UUID) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    principal.require_tenant_admin()
    form = await _form(request)
    _require_csrf(request, form)
    target, expect = form.get("target", ""), form.get("expect", "")
    if target not in _TRANSITION_MESSAGE_KEYS or expect not in AutomationStateEnum.__members__:
        raise HTTPException(status_code=422, detail="invalid_state_transition")
    if not can_transition(AutomationStateEnum(expect), AutomationStateEnum(target)):
        raise HTTPException(status_code=422, detail="transition_not_allowed")
    try:
        if target == "HUMAN_ACTIVE":
            await start_human_reception(
                conversation_id=conversation_id,
                principal=principal,
                expected_state=expect,
            )
        else:
            await resume_bot(
                conversation_id=conversation_id,
                allowed_tenants=principal.allowed_tenants,
                actor=principal.actor,
                target=target,
                principal=principal,
                expected_state=expect,
            )
    except HumanWorkflowError as exc:
        raise _workflow_error(exc) from exc
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


def _optional_expected_generation(form: dict[str, str]) -> int | None:
    raw_value = form.get("expected_generation")
    if raw_value is None or not raw_value.strip():
        return None
    try:
        expected_generation = int(raw_value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="draft_expected_generation_invalid") from exc
    if expected_generation < 0:
        raise HTTPException(status_code=422, detail="draft_expected_generation_invalid")
    return expected_generation


def _draft_review_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, DraftReviewNotFound):
        return HTTPException(status_code=404, detail=exc.code)
    if isinstance(exc, DraftReviewConflict):
        return HTTPException(status_code=409, detail=exc.code)
    if isinstance(exc, DraftReviewValidationError):
        return HTTPException(status_code=422, detail=exc.code)
    return HTTPException(status_code=500, detail="draft_review_failed")


async def _required_admin_draft_tenant(
    decision_id: uuid.UUID,
    principal: Principal,
) -> str:
    async with get_session_factory()() as session:
        tenant_id = await session.scalar(
            select(models.ReplyDecision.tenant_id).where(
                models.ReplyDecision.id == decision_id,
                models.ReplyDecision.tenant_id == DEFAULT_TENANT_ID,
            )
        )
    if tenant_id is None:
        raise HTTPException(status_code=404, detail="decision_not_found")
    return tenant_id


@router.post("/decisions/{decision_id}/approve")
async def approve_draft(request: Request, decision_id: uuid.UUID) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    required_tenant_id = await _required_admin_draft_tenant(decision_id, principal)
    try:
        await approve_draft_review(
            decision_id=decision_id,
            required_tenant_id=required_tenant_id,
            actor=principal.actor,
            final_reply_text=form.get("final_reply_text"),
            expected_generation=_optional_expected_generation(form),
            expected_review_action=form.get("expected_review_action") or None,
            principal=principal,
        )
    except (DraftReviewNotFound, DraftReviewConflict, DraftReviewValidationError) as exc:
        raise _draft_review_http_error(exc) from exc
    return RedirectResponse("/admin/inbox?queue=drafts", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/decisions/{decision_id}/discard")
async def discard_draft(request: Request, decision_id: uuid.UUID) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    required_tenant_id = await _required_admin_draft_tenant(decision_id, principal)
    try:
        await reject_draft_review(
            decision_id=decision_id,
            required_tenant_id=required_tenant_id,
            actor=principal.actor,
            review_reason=form.get("review_reason", ""),
            expected_generation=_optional_expected_generation(form),
            expected_review_action=form.get("expected_review_action") or None,
            principal=principal,
        )
    except (DraftReviewNotFound, DraftReviewConflict, DraftReviewValidationError) as exc:
        raise _draft_review_http_error(exc) from exc
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
    "added": ("ok", "admin.knowledge.banner.added"),
    "duplicate": ("err", "admin.knowledge.banner.duplicate"),
    "embed_failed": ("err", "admin.knowledge.banner.embed_failed"),
    "status_changed": ("ok", "admin.knowledge.banner.status_changed"),
    "classification_changed": ("ok", "admin.knowledge.banner.classification_changed"),
    "language_confirmed": ("ok", "admin.knowledge.banner.language_confirmed"),
    "deleted": ("ok", "admin.knowledge.banner.deleted"),
    "import_bad_csv": ("err", "admin.knowledge.banner.import_bad_csv"),
    "import_too_large": ("err", "admin.knowledge.banner.import_too_large"),
}

def _legacy_knowledge_location(tenant_id: str, *, notice: str = "", **values: object) -> str:
    query = {
        key: str(value)
        for key, value in {"notice": notice, **values}.items()
        if value not in (None, "")
    }
    location = f"/app/t/{quote(tenant_id, safe='')}/knowledge"
    return f"{location}?{urlencode(query)}" if query else location


def _legacy_knowledge_http_error(exc: KnowledgeApplicationError) -> HTTPException:
    if isinstance(exc, KnowledgeAuthorizationError):
        return HTTPException(status_code=403, detail=exc.code)
    if isinstance(exc, KnowledgeNotFoundError):
        return HTTPException(status_code=404, detail=exc.code)
    if isinstance(exc, KnowledgeConflictError):
        return HTTPException(status_code=409, detail=exc.code)
    return HTTPException(status_code=422, detail=exc.code)


def _require_legacy_knowledge_admin(principal: Principal) -> None:
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail="tenant_admin_required")
    if principal.tenant_id != DEFAULT_TENANT_ID:
        raise HTTPException(status_code=404, detail="tenant_workspace_not_found")


async def _legacy_document_tenant(
    session,
    *,
    principal: Principal,
    document_id: uuid.UUID,
) -> str:
    tenant_id = await session.scalar(
        select(models.KnowledgeDocument.tenant_id).where(
            models.KnowledgeDocument.id == document_id,
            models.KnowledgeDocument.tenant_id == DEFAULT_TENANT_ID,
        )
    )
    if tenant_id is None:
        raise HTTPException(status_code=404, detail="knowledge_document_not_found")
    return tenant_id


def _query_int(request: Request, name: str) -> int:
    try:
        return max(0, int(request.query_params.get(name) or 0))
    except ValueError:
        return 0


def _knowledge_actions(doc: models.KnowledgeDocument, csrf: str) -> str:
    status_target = "draft" if doc.status == "published" else "published"
    status_label = (
        translate("admin.knowledge.unpublish")
        if doc.status == "published"
        else translate("admin.knowledge.publish")
    )
    if doc.status == "draft":
        official_target = "false" if doc.is_official_contact else "true"
        official_label = (
            translate("admin.knowledge.remove_official")
            if doc.is_official_contact
            else translate("admin.knowledge.mark_official")
        )
        classification = (
            f'<form class="inline" method="post" '
            f'action="/admin/knowledge/{doc.id}/official-contact">'
            f'<input type="hidden" name="csrf_token" value="{csrf}">'
            f'<input type="hidden" name="target" value="{official_target}">'
            f'<button class="btn-sm btn-ghost">{official_label}</button></form>'
        )
        if doc.language_verified:
            language_confirmation = (
                f'<span class="muted">{translate("admin.knowledge.english_confirmed")}</span>'
            )
        elif doc.language_detection_status in {"mixed", "non_english"}:
            language_confirmation = f'<span class="muted">{translate("admin.knowledge.needs_english_replacement")}</span>'
        else:
            reason_input = (
                f'<label class="sr-only" for="knowledge-confirmation-{doc.id}">'
                f"{translate('admin.knowledge.confirmation_reason')}</label>"
                f'<input id="knowledge-confirmation-{doc.id}" name="confirmation_reason" '
                f'placeholder="{translate("admin.knowledge.confirmation_reason")}" required>'
                if doc.language_detection_status == "unknown"
                else ""
            )
            language_confirmation = (
                f'<form class="inline" method="post" '
                f'action="/admin/knowledge/{doc.id}/confirm-english">'
                f'<input type="hidden" name="csrf_token" value="{csrf}">'
                f'{reason_input}<button class="btn-sm btn-ghost">{translate("admin.knowledge.confirm_english")}</button></form>'
            )
    else:
        classification = f'<span class="muted">{translate("admin.knowledge.unpublish_before_classification")}</span>'
        language_confirmation = (
            f'<span class="muted">{translate("admin.knowledge.unpublish_before_language")}</span>'
        )
    return f"""<form class="inline" method="post" action="/admin/knowledge/{doc.id}/status"><input type="hidden" name="csrf_token" value="{csrf}"><input type="hidden" name="target" value="{status_target}"><button class="btn-sm btn-ghost">{status_label}</button></form>
{classification}
{language_confirmation}
<form class="inline" method="post" action="/admin/knowledge/{doc.id}/delete"><input type="hidden" name="csrf_token" value="{csrf}"><button class="btn-sm btn-danger" onclick="return confirm('{translate("admin.knowledge.delete_confirm")}')">{translate("admin.knowledge.delete")}</button></form>"""


def _require_knowledge_corpus_mutation_allowed() -> None:
    # Runtime provenance is checked at decision/send time; do not freeze knowledge maintenance.
    return None



@router.get("/content/knowledge", response_class=HTMLResponse)
@router.get("/knowledge", response_class=HTMLResponse)
async def knowledge_page(request: Request, notice: str = "") -> Response:
    return await _legacy_tenant_get_redirect(request, "/knowledge")

    # Legacy implementation retained below for the staged POST compatibility adapters.
    principal = await _web_principal(request)
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail="tenant_admin_required")
    tenant_id = tenant_id_or_default(
        principal,
        (request.query_params.get("tenant_id") or "").strip(),
    )
    query = {
        key: value
        for key, value in {
            "notice": notice,
            "status_filter": request.query_params.get("status_filter", ""),
            "brand_id": request.query_params.get("brand_id", ""),
            "platform": request.query_params.get("platform", ""),
            "category": request.query_params.get("category", ""),
        }.items()
        if value
    }
    location = f"/app/t/{quote(tenant_id, safe='')}/knowledge"
    if query:
        location = f"{location}?{urlencode(query)}"
    return RedirectResponse(location, status_code=status.HTTP_303_SEE_OTHER)
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
        batch_rows = (
            await session.execute(
                select(
                    models.KnowledgeDocument.import_batch_id,
                    models.KnowledgeDocument.source_file,
                    models.KnowledgeDocument.tenant_id,
                    func.count().label("candidate_count"),
                    func.max(models.KnowledgeDocument.created_at).label("imported_at"),
                )
                .where(
                    models.KnowledgeDocument.tenant_id.in_(tenants),
                    models.KnowledgeDocument.import_batch_id.isnot(None),
                    models.KnowledgeDocument.status == "draft",
                    models.KnowledgeDocument.language_detection_status == "english",
                    models.KnowledgeDocument.language_verified.is_(False),
                )
                .group_by(
                    models.KnowledgeDocument.import_batch_id,
                    models.KnowledgeDocument.source_file,
                    models.KnowledgeDocument.tenant_id,
                )
                .order_by(desc(func.max(models.KnowledgeDocument.created_at)))
                .limit(100)
            )
        ).all()
    banner = ""
    if notice == "imported":
        n = _query_int(request, "inserted")
        s = _query_int(request, "skipped")
        b = _query_int(request, "blank")
        imported_batch_id = (request.query_params.get("batch_id") or "").strip()
        banner = f'<div class="banner ok">{translate("admin.knowledge.imported", inserted=n, skipped=s, blank=b, batch_id=html.escape(imported_batch_id or "—"))}</div>'
    elif notice == "bulk_language_confirmed":
        count = _query_int(request, "count")
        banner = f'<div class="banner ok">{translate("admin.knowledge.bulk_language_confirmed", count=count)}</div>'
    elif notice == "bulk_published":
        count = _query_int(request, "count")
        skipped = _query_int(request, "skipped")
        reasons = html.escape((request.query_params.get("skip_reasons") or "").strip())
        banner = f'<div class="banner ok">{translate("admin.knowledge.bulk_published", count=count, skipped=skipped, reasons=reasons or translate("admin.common.none"))}</div>'
    elif notice in _KB_BANNERS:
        tone, message_key = _KB_BANNERS[notice]
        banner = f'<div class="banner {tone}">{translate(message_key)}</div>'
    rows = (
        "".join(
            f"<tr><td><details><summary>{html.escape((d.question or '')[:40])}</summary>"
            f"<pre>{html.escape(d.question or '')}</pre></details></td>"
            f"<td class='muted'><details><summary>{html.escape((d.reply or '')[:48])}</summary>"
            f"<pre>{html.escape(d.reply or '')}</pre></details></td>"
            f"<td class='muted'>{html.escape(d.category or '—')}</td>"
            f"<td>{translate('admin.knowledge.yes') if d.is_official_contact else translate('admin.knowledge.no')}</td>"
            f"<td>{html.escape(d.detected_language)} / {html.escape(d.language_detection_status)}</td>"
            f"<td>{translate('admin.knowledge.english_confirmed') if d.language_verified else translate('admin.knowledge.pending_confirmation')}</td>"
            f"<td>{_pill(d.status)}</td>"
            f"<td>{_knowledge_actions(d, csrf)}</td></tr>"
            for d in docs
        )
        or f"<tr><td colspan='8' class='muted'>{translate('admin.knowledge.empty')}</td></tr>"
    )
    add_form = f"""<details class="collapse"><summary>{translate("admin.knowledge.add_entry")}</summary><div class="inner">
<form method="post" action="/admin/knowledge/add"><input type="hidden" name="csrf_token" value="{csrf}">
{_tenant_input(principal)}{_input("question", translate("admin.knowledge.trigger_question"))}
<label for="f-kb-reply">{translate("admin.knowledge.standard_reply")}</label><textarea id="f-kb-reply" name="reply" required></textarea>
{_input("category", translate("admin.knowledge.category_optional"), required=False)}{_input("brand_id", translate("admin.knowledge.brand_default"), required=False)}
<label><input type="checkbox" name="is_official_contact" value="true"> {translate("admin.knowledge.official_template")}</label>
<p class="hint">{translate("admin.knowledge.draft_notice")}</p>
<button class="btn-block">{translate("admin.knowledge.add_and_embed")}</button></form></div></details>"""
    import_form = f"""<details class="collapse"><summary>{translate("admin.knowledge.import_csv")}</summary><div class="inner">
<form method="post" action="/admin/knowledge/import" enctype="multipart/form-data"><input type="hidden" name="csrf_token" value="{csrf}">
{_tenant_input(principal)}{_input("brand_id", translate("admin.knowledge.brand_default"), required=False)}
<label for="f-kb-csv">{translate("admin.knowledge.csv_file")}</label><input id="f-kb-csv" type="file" name="file" accept=".csv" required>
<p class="hint">{translate("admin.knowledge.csv_hint")}</p>
<button class="btn-block">{translate("admin.knowledge.upload_import")}</button></form></div></details>"""
    bulk_publish_form = f"""<section class="card"><h2>{translate("admin.knowledge.bulk_publish")}</h2>
<form method="post" action="/admin/knowledge/bulk-publish"><input type="hidden" name="csrf_token" value="{csrf}">
{_tenant_input(principal)}
<p class="hint">{translate("admin.knowledge.bulk_publish_hint")}</p>
<button class="btn-block" onclick="return confirm('{translate("admin.knowledge.bulk_publish_confirm")}')">{translate("admin.knowledge.bulk_publish_button")}</button></form></section>"""
    batch_options = "".join(
        f'<option value="{row.import_batch_id}">'
        f"{html.escape(row.tenant_id)} / {html.escape(row.source_file or '—')} / "
        f"{translate('admin.knowledge.entries_count', count=row.candidate_count)} / {_fmt(row.imported_at)}</option>"
        for row in batch_rows
    )
    bulk_confirm_form = f"""<section class="card"><h2>{translate("admin.knowledge.bulk_confirm")}</h2>
<form method="post" action="/admin/knowledge/bulk-confirm-english"><input type="hidden" name="csrf_token" value="{csrf}">
<label for="f-kb-import-batch">{translate("admin.knowledge.import_batch")}</label>
<select id="f-kb-import-batch" name="import_batch_id" required>{batch_options or f'<option value="">{translate("admin.knowledge.no_pending_batch")}</option>'}</select>
<p class="hint">{translate("admin.knowledge.bulk_confirm_hint")}</p>
<button class="btn-block" onclick="return confirm('{translate("admin.knowledge.bulk_confirm_confirm")}')">{translate("admin.knowledge.bulk_confirm_button")}</button></form></section>"""
    page_title = translate("admin.knowledge.title")
    body = f"""<h1>{page_title}</h1><p class="lede">{translate("admin.knowledge.description")}</p>{banner}
{add_form}
{import_form}
{bulk_confirm_form}
{bulk_publish_form}
<section class="card"><h2>{translate("admin.knowledge.template_list")}</h2><p class="hint">{translate("admin.knowledge.template_list_hint", count=len(docs))}</p><div class="tablewrap"><table><thead><tr><th>{translate("admin.common.question")}</th><th>{translate("admin.common.reply")}</th><th>{translate("admin.common.classification")}</th><th>{translate("admin.knowledge.official_contact")}</th><th>{translate("admin.knowledge.language_detection")}</th><th>{translate("admin.knowledge.english_confirmation")}</th><th>{translate("common.status")}</th><th>{translate("admin.common.operation")}</th></tr></thead><tbody>{rows}</tbody></table></div></section>"""
    response = HTMLResponse(
        _page(
            page_title,
            body,
            active="knowledge",
            show_users=principal.is_admin,
            principal=principal,
        )
    )
    return _ensure_csrf(response, request, csrf)


@router.post("/knowledge/add")
async def knowledge_add(request: Request) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    _require_legacy_knowledge_admin(principal)
    tenant_id = (form.get("tenant_id") or "").strip()
    if tenant_id not in principal.allowed_tenants:
        raise HTTPException(status_code=403, detail="tenant_access_denied")
    from social_reply.application.knowledge.commands import (
        CreateKnowledgeDocumentCommand,
        execute_create_knowledge_document,
    )
    from social_reply.application.knowledge.upload import parse_protected_values
    from social_reply.application.reply_decision.runner import _get_embedder

    official_value = form.get("is_official_contact", "")
    if official_value not in {"", "true"}:
        raise HTTPException(status_code=422, detail="invalid_is_official_contact")
    try:
        async with get_session_factory()() as session:
            await execute_create_knowledge_document(
                session,
                CreateKnowledgeDocumentCommand(
                    required_tenant_id=tenant_id,
                    principal=principal,
                    question=form.get("question", ""),
                    reply=form.get("reply", ""),
                    brand_id=form.get("brand_id", "") or "default",
                    platform=form.get("platform") or None,
                    category=form.get("category") or None,
                    is_official_contact=official_value == "true",
                    protected_values=parse_protected_values(form.get("protected_values_json")),
                    source_name="admin-console",
                ),
                embedder=_get_embedder(),
            )
            await session.commit()
    except KnowledgeConflictError as exc:
        if exc.code == "knowledge_document_duplicate":
            return RedirectResponse(
                _legacy_knowledge_location(tenant_id, notice="duplicate"),
                status_code=status.HTTP_303_SEE_OTHER,
            )
        raise _legacy_knowledge_http_error(exc) from exc
    except KnowledgeApplicationError as exc:
        raise _legacy_knowledge_http_error(exc) from exc
    return RedirectResponse(
        _legacy_knowledge_location(tenant_id, notice="created"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/knowledge/import")
async def knowledge_import(request: Request) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    form = await request.form()
    _require_csrf(request, {"csrf_token": str(form.get("csrf_token") or "")})
    _require_legacy_knowledge_admin(principal)
    tenant_id = str(form.get("tenant_id") or "").strip()
    if tenant_id not in principal.allowed_tenants:
        raise HTTPException(status_code=403, detail="tenant_access_denied")
    upload = form.get("file")
    filename = getattr(upload, "filename", None)
    read = getattr(upload, "read", None)
    from social_reply.application.knowledge.commands import (
        ImportKnowledgeBatchCommand,
        execute_import_knowledge_batch,
    )
    from social_reply.application.knowledge.upload import (
        MAX_KNOWLEDGE_UPLOAD_BYTES,
        decode_knowledge_csv_upload,
    )
    from social_reply.application.reply_decision.runner import _get_embedder

    if not callable(read):
        raise HTTPException(status_code=422, detail="knowledge_csv_required")
    raw = await read(MAX_KNOWLEDGE_UPLOAD_BYTES + 1)
    try:
        csv_text = decode_knowledge_csv_upload(raw)
        async with get_session_factory()() as session:
            report = await execute_import_knowledge_batch(
                session,
                ImportKnowledgeBatchCommand(
                    required_tenant_id=tenant_id,
                    principal=principal,
                    csv_text=csv_text,
                    source_name=(str(filename or "").strip() or "import.csv")[:256],
                    brand_id_default=str(form.get("brand_id") or "default"),
                ),
                embedder=_get_embedder(),
            )
            await session.commit()
    except KnowledgeAuthorizationError as exc:
        raise _legacy_knowledge_http_error(exc) from exc
    except (KnowledgeApplicationError, ValueError):
        return RedirectResponse(
            _legacy_knowledge_location(tenant_id, notice="import_bad_csv"),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    except Exception as exc:
        _log_knowledge_exception("Knowledge CSV import failed", exc)
        return RedirectResponse(
            _legacy_knowledge_location(tenant_id, notice="embed_failed"),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    return RedirectResponse(
        _legacy_knowledge_location(
            tenant_id,
            notice="imported",
            inserted=report.inserted,
            skipped=report.skipped,
            blank=report.blank,
            batch_id=report.batch_id,
        ),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/knowledge/{doc_id}/confirm-english")
async def knowledge_confirm_english(request: Request, doc_id: uuid.UUID) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    _require_legacy_knowledge_admin(principal)
    from social_reply.application.knowledge.commands import (
        ConfirmKnowledgeEnglishCommand,
        execute_confirm_knowledge_english,
    )

    try:
        async with get_session_factory()() as session:
            tenant_id = await _legacy_document_tenant(
                session,
                principal=principal,
                document_id=doc_id,
            )
            await execute_confirm_knowledge_english(
                session,
                ConfirmKnowledgeEnglishCommand(
                    required_tenant_id=tenant_id,
                    principal=principal,
                    document_id=doc_id,
                    confirmation_reason=form.get("confirmation_reason", ""),
                ),
            )
            await session.commit()
    except KnowledgeApplicationError as exc:
        raise _legacy_knowledge_http_error(exc) from exc
    return RedirectResponse(
        f"/app/t/{quote(tenant_id, safe='')}/knowledge/documents/{doc_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/knowledge/bulk-confirm-english")
async def knowledge_bulk_confirm_english(request: Request) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    _require_legacy_knowledge_admin(principal)
    raw_batch_id = (form.get("import_batch_id") or "").strip()
    try:
        import_batch_id = uuid.UUID(raw_batch_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid_import_batch_id") from exc
    from social_reply.application.knowledge.commands import (
        ConfirmKnowledgeEnglishBatchCommand,
        execute_confirm_knowledge_english_batch,
    )

    try:
        async with get_session_factory()() as session:
            batch_tenant = await session.scalar(
                select(models.KnowledgeDocument.tenant_id)
                .where(
                    models.KnowledgeDocument.import_batch_id == import_batch_id,
                    models.KnowledgeDocument.tenant_id.in_(principal.allowed_tenants),
                )
                .limit(1)
            )
            if batch_tenant is None:
                raise HTTPException(
                    status_code=404,
                    detail="knowledge_import_batch_not_found",
                )
            confirmed_count = await execute_confirm_knowledge_english_batch(
                session,
                ConfirmKnowledgeEnglishBatchCommand(
                    required_tenant_id=batch_tenant,
                    principal=principal,
                    import_batch_id=import_batch_id,
                ),
            )
            await session.commit()
    except KnowledgeApplicationError as exc:
        raise _legacy_knowledge_http_error(exc) from exc
    return RedirectResponse(
        _legacy_knowledge_location(
            batch_tenant,
            notice="bulk_confirmed",
            count=confirmed_count,
        ),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/knowledge/bulk-publish")
async def knowledge_bulk_publish(request: Request) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    _require_legacy_knowledge_admin(principal)
    _require_knowledge_corpus_mutation_allowed()
    requested_tenant = (form.get("tenant_id") or "").strip()
    if not requested_tenant:
        raise HTTPException(status_code=422, detail="tenant_id_required")
    tenant_id = tenant_id_or_default(principal, requested_tenant)
    from social_reply.application.knowledge.publication import (
        BulkPublishKnowledgeCommand,
        execute_bulk_publish_knowledge,
    )

    try:
        async with get_session_factory()() as session:
            result = await execute_bulk_publish_knowledge(
                session,
                BulkPublishKnowledgeCommand(
                    required_tenant_id=tenant_id,
                    principal=principal,
                ),
            )
            await session.commit()
    except KnowledgeApplicationError as exc:
        raise _legacy_knowledge_http_error(exc) from exc
    return RedirectResponse(
        _legacy_knowledge_location(
            tenant_id,
            notice="bulk_published",
            published=result.published_count,
            skipped=result.skipped_count,
        ),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/knowledge/{doc_id}/status")
async def knowledge_set_status(request: Request, doc_id: uuid.UUID) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    _require_legacy_knowledge_admin(principal)
    _require_knowledge_corpus_mutation_allowed()
    target = (form.get("target") or "").strip()
    if target not in {"draft", "published"}:
        raise HTTPException(status_code=422, detail="invalid_knowledge_status")
    from social_reply.application.knowledge.publication import (
        PublishKnowledgeCommand,
        UnpublishKnowledgeCommand,
        execute_publish_knowledge,
        execute_unpublish_knowledge,
    )

    try:
        async with get_session_factory()() as session:
            tenant_id = await _legacy_document_tenant(
                session,
                principal=principal,
                document_id=doc_id,
            )
            if target == "published":
                await execute_publish_knowledge(
                    session,
                    PublishKnowledgeCommand(
                        required_tenant_id=tenant_id,
                        principal=principal,
                        document_id=doc_id,
                    ),
                )
            else:
                await execute_unpublish_knowledge(
                    session,
                    UnpublishKnowledgeCommand(
                        required_tenant_id=tenant_id,
                        principal=principal,
                        document_id=doc_id,
                    ),
                )
            await session.commit()
    except KnowledgeApplicationError as exc:
        raise _legacy_knowledge_http_error(exc) from exc
    return RedirectResponse(
        f"/app/t/{quote(tenant_id, safe='')}/knowledge/documents/{doc_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/knowledge/{doc_id}/official-contact")
async def knowledge_set_official_contact(request: Request, doc_id: uuid.UUID) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    _require_legacy_knowledge_admin(principal)
    target_value = (form.get("target") or "").strip()
    if target_value not in {"true", "false"}:
        raise HTTPException(status_code=422, detail="invalid_official_contact_target")
    target = target_value == "true"
    from social_reply.application.knowledge.commands import (
        SetKnowledgeOfficialContactCommand,
        execute_set_knowledge_official_contact,
    )

    try:
        async with get_session_factory()() as session:
            tenant_id = await _legacy_document_tenant(
                session,
                principal=principal,
                document_id=doc_id,
            )
            await execute_set_knowledge_official_contact(
                session,
                SetKnowledgeOfficialContactCommand(
                    required_tenant_id=tenant_id,
                    principal=principal,
                    document_id=doc_id,
                    is_official_contact=target,
                ),
            )
            await session.commit()
    except KnowledgeApplicationError as exc:
        raise _legacy_knowledge_http_error(exc) from exc
    return RedirectResponse(
        f"/app/t/{quote(tenant_id, safe='')}/knowledge/documents/{doc_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/knowledge/{doc_id}/delete")
async def knowledge_delete(request: Request, doc_id: uuid.UUID) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    _require_legacy_knowledge_admin(principal)
    from social_reply.application.knowledge.commands import (
        DeleteKnowledgeDraftCommand,
        execute_delete_knowledge_draft,
    )

    try:
        async with get_session_factory()() as session:
            tenant_id = await _legacy_document_tenant(
                session,
                principal=principal,
                document_id=doc_id,
            )
            await execute_delete_knowledge_draft(
                session,
                DeleteKnowledgeDraftCommand(
                    required_tenant_id=tenant_id,
                    principal=principal,
                    document_id=doc_id,
                ),
            )
            await session.commit()
    except KnowledgeApplicationError as exc:
        raise _legacy_knowledge_http_error(exc) from exc
    return RedirectResponse(
        _legacy_knowledge_location(tenant_id, notice="deleted"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ---------- Editable reply business prompt ----------


def _prompt_tenant(principal: Principal, requested: str) -> str:
    return tenant_id_or_default(principal, requested)


def _prompt_location(tenant_id: str, brand_id: str, *, notice: str = "") -> str:
    location = f"/app/t/{quote(tenant_id, safe='')}/agents/{quote(brand_id, safe='')}/instructions"
    return f"{location}?{urlencode({'notice': notice})}" if notice else location


def _prompt_expected_revision(form: dict[str, str]) -> int:
    try:
        value = int(form.get("expected_revision", ""))
    except ValueError as exc:
        raise ReplyBusinessPromptConflict("reply_business_prompt_revision_conflict") from exc
    if value < 0:
        raise ReplyBusinessPromptConflict("reply_business_prompt_revision_conflict")
    return value


@router.get("/content/reply-prompt", response_class=HTMLResponse)
@router.get("/content/brand-voice", response_class=HTMLResponse)
@router.get("/prompt", response_class=HTMLResponse)
async def prompt_page(request: Request, notice: str = "", tenant_id: str = "") -> Response:
    return await _legacy_tenant_get_redirect(request, "/agents/default/instructions")

    # Legacy implementation retained below for the staged POST compatibility adapters.
    principal = await _web_principal(request)
    tenant = _prompt_tenant(principal, tenant_id)
    brand = (request.query_params.get("brand_id") or "default").strip() or "default"
    return RedirectResponse(
        _prompt_location(tenant, brand, notice=notice),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/content/reply-prompt/save")
@router.post("/prompt/save")
async def prompt_save(request: Request) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    if set(form) != {
        "csrf_token",
        "tenant_id",
        "brand_id",
        "expected_revision",
        "content",
        "change_note",
    }:
        raise HTTPException(status_code=422, detail="reply_business_prompt_fields_invalid")
    tenant = _prompt_tenant(principal, form.get("tenant_id", ""))
    brand = (form.get("brand_id") or "default").strip() or "default"
    try:
        expected_revision = _prompt_expected_revision(form)
        async with get_session_factory()() as session:
            await execute_save_reply_business_prompt(
                session,
                SaveReplyBusinessPromptCommand(
                    tenant_id=tenant,
                    brand_id=brand,
                    content=form.get("content", ""),
                    expected_revision=expected_revision,
                    actor=principal.actor,
                    change_note=form.get("change_note"),
                    principal=principal,
                ),
            )
            await session.commit()
    except ReplyBusinessPromptConflict:
        return RedirectResponse(
            _prompt_location(tenant, brand, notice="revision_conflict"),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    except BusinessPromptValidationError:
        return RedirectResponse(
            _prompt_location(tenant, brand, notice="prompt_invalid"),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    except ReplyBusinessPromptScopeError:
        return RedirectResponse(
            _prompt_location(tenant, "default", notice="brand_invalid"),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    return RedirectResponse(
        _prompt_location(tenant, brand, notice="saved"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/content/reply-prompt/versions/{version_id}/rollback")
async def prompt_rollback(version_id: uuid.UUID, request: Request) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    if set(form) != {"csrf_token", "tenant_id", "brand_id", "expected_revision"}:
        raise HTTPException(status_code=422, detail="reply_business_prompt_fields_invalid")
    tenant = _prompt_tenant(principal, form.get("tenant_id", ""))
    brand = (form.get("brand_id") or "default").strip() or "default"
    try:
        expected_revision = _prompt_expected_revision(form)
        async with get_session_factory()() as session:
            await execute_rollback_reply_business_prompt(
                session,
                RollbackReplyBusinessPromptCommand(
                    tenant_id=tenant,
                    brand_id=brand,
                    source_version_id=version_id,
                    expected_revision=expected_revision,
                    actor=principal.actor,
                    principal=principal,
                ),
            )
            await session.commit()
    except ReplyBusinessPromptConflict:
        notice = "revision_conflict"
    except (BusinessPromptValidationError, ReplyBusinessPromptScopeError):
        notice = "brand_invalid"
    else:
        notice = "rolled_back"
    return RedirectResponse(
        _prompt_location(tenant, brand, notice=notice),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/content/reply-prompt/trial")
@router.post("/prompt/trial")
async def prompt_trial(request: Request) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    if set(form) != {"csrf_token", "tenant_id", "brand_id", "text"}:
        raise HTTPException(status_code=422, detail="reply_business_prompt_fields_invalid")
    tenant = _prompt_tenant(principal, form.get("tenant_id", ""))
    brand = (form.get("brand_id") or "default").strip() or "default"
    try:
        await run_reply_business_prompt_trial(
            tenant_id=tenant,
            brand_id=brand,
            input_text=form.get("text", ""),
            actor=principal.actor,
        )
    except ReplyBusinessPromptTrialValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.code) from exc
    except ReplyBusinessPromptTrialRateLimited:
        response = HTMLResponse(
            translate("agent.instructions.trial.rate_limited"),
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        )
        response.headers["Cache-Control"] = "no-store"
        return response
    except (ReplyBusinessPromptTrialUnavailable, ReplyBusinessPromptTrialExecutionError):
        response = HTMLResponse(
            translate("agent.instructions.trial.unavailable"),
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
        response.headers["Cache-Control"] = "no-store"
        return response
    except ReplyBusinessPromptScopeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    response = RedirectResponse(
        _prompt_location(tenant, brand),
        status_code=status.HTTP_303_SEE_OTHER,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


# ---------- System health ----------


@router.get("/system/health", response_class=HTMLResponse)
@router.get("/health", response_class=HTMLResponse)
async def health_page(request: Request) -> Response:
    if request.url.path == "/admin/health":
        return await _legacy_tenant_get_redirect(request, "/health")

    principal = await current_principal(request)
    if principal is None:
        return RedirectResponse("/auth/login", status_code=status.HTTP_303_SEE_OTHER)
    if principal.must_change_password:
        return RedirectResponse(
            "/auth/change-password",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    principal.require_superadmin()

    # The tenant health implementation lives in saas_console. This system view remains
    # available only to the bootstrap superadmin and aggregates the configured tenant set.
    tenants = principal.allowed_tenants
    now = datetime.now(UTC)
    day_ago = now - timedelta(hours=24)
    async with get_session_factory()() as session:
        health_metrics = await _load_health_metrics(session, tenants, now, principal=principal)
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
    action_metric_count = sum(metric.action_count for metric in health_metrics)
    warning_metric_count = sum(metric.warning_count for metric in health_metrics)
    oldest_metric_at = min(
        (metric.oldest_at for metric in health_metrics if metric.oldest_at is not None),
        default=None,
    )
    health_status = "degraded" if action_metric_count else "healthy"
    metric_rows = "".join(
        f'<tr id="{metric.key}"><td><strong>{metric.label}</strong></td>'
        f"<td>{status_badge(health_status if metric.action_count else 'healthy', label=metric.level)}</td>"
        f"<td>{translate('admin.health.backlog_summary', action_count=metric.action_count, warning_count=metric.warning_count)}</td>"
        f"<td class='muted'>{_health_age(now, metric.oldest_at)}</td>"
        f"<td><a href='{metric.href}'>{translate('admin.common.view')}</a></td></tr>"
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
        or f"<tr><td colspan='6' class='muted'>{translate('admin.health.no_delivery_records')}</td></tr>"
    )
    ingress_rows = (
        "".join(
            f"<tr><td>{html.escape(source)}</td><td>{_pill(processing_status)}</td>"
            f"<td class='muted'>{count}</td><td class='muted'>{_fmt(last_at)}</td></tr>"
            for source, processing_status, count, last_at in ingress
        )
        or f"<tr><td colspan='4' class='muted'>{translate('admin.health.no_ingress')}</td></tr>"
    )
    page_title = translate("admin.health.title")
    body = f"""<section class="saas-next-action"><div><div class="saas-eyebrow">{escape(translate("nav.system_health"))}</div>
<h2>{escape(translate("admin.health.core_pipeline"))}</h2>
<p>{escape(translate("admin.health.backlog_summary", action_count=action_metric_count, warning_count=warning_metric_count))}</p></div>
{secondary_action("/admin/system/safety", translate("system.overview.safety"), small=True)}</section>
<div class="saas-status-summary"><span class="saas-status-summary-label">{escape(translate("common.status"))}</span>
<div class="saas-status-summary-items">{status_badge(health_status)}<span class="saas-muted">{escape(translate("admin.overview.oldest_wait"))} · {escape(_health_age(now, oldest_metric_at))}</span></div></div>
<div class="saas-section-header"><div class="saas-section-header-copy"><h2>{escape(translate("admin.health.core_pipeline"))}</h2>
<p>{escape(translate("admin.health.description"))}</p></div></div>
<div class="saas-table-wrap"><table class="saas-table"><thead><tr><th>{escape(translate("admin.overview.stage"))}</th><th>{escape(translate("common.status"))}</th><th>{escape(translate("admin.overview.backlog"))}</th><th>{escape(translate("admin.overview.oldest_wait"))}</th><th></th></tr></thead><tbody>{metric_rows}</tbody></table></div>
<div class="saas-section-header"><div class="saas-section-header-copy"><h2>Outbox</h2><p>{escape(translate("admin.health.outbox_hint"))}</p></div></div>
<div class="saas-table-wrap"><table class="saas-table"><thead><tr><th>{escape(translate("common.time"))}</th><th>{escape(translate("common.status"))}</th><th>{escape(translate("admin.health.destination"))}</th><th>{escape(translate("admin.common.content"))}</th><th>{escape(translate("admin.common.attempts"))}</th><th>{escape(translate("admin.common.error"))}</th></tr></thead><tbody>{rows}</tbody></table></div>
<div class="saas-section-header"><div class="saas-section-header-copy"><h2>{escape(translate("admin.health.ingress"))}</h2><p>{escape(translate("admin.health.last_received"))}</p></div></div>
<div class="saas-table-wrap" id="ingress"><table class="saas-table"><thead><tr><th>{escape(translate("admin.health.ingress_source"))}</th><th>{escape(translate("admin.health.processing_status"))}</th><th>{escape(translate("admin.health.event_count"))}</th><th>{escape(translate("admin.health.last_received"))}</th></tr></thead><tbody>{ingress_rows}</tbody></table></div>"""
    return HTMLResponse(
        render_saas_page(
            principal=principal,
            title=page_title,
            description=translate("admin.health.description"),
            body=body,
            active_navigation="system-health",
            tenant_id=None,
            system_admin=True,
        )
    )


@router.get("/delivery")
async def delivery_page(request: Request) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    return RedirectResponse("/admin/inbox?queue=delivery", status_code=status.HTTP_303_SEE_OTHER)


_LEGACY_DELIVERY_RETRY_REASON = "Legacy admin confirmed failed delivery for retry."
_LEGACY_DELIVERY_VERIFICATION_SOURCE = "ADMIN_OPERATOR_ATTESTED"


def _legacy_delivery_recovery_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, DeliveryRecoveryNotFound):
        return HTTPException(status_code=404, detail=exc.code)
    if isinstance(exc, DeliveryRecoveryConflict):
        return HTTPException(status_code=409, detail=exc.code)
    if isinstance(exc, DeliveryRecoveryValidationError):
        return HTTPException(status_code=422, detail=exc.code)
    return HTTPException(status_code=500, detail="delivery_recovery_failed")


async def _legacy_retry_fence(
    *,
    outbox_id: uuid.UUID,
    allowed_tenants: frozenset[str],
) -> tuple[str, str, int]:
    async with get_session_factory()() as session:
        row = (
            await session.execute(
                select(
                    models.OutboxMessage.tenant_id,
                    models.OutboxMessage.status,
                    models.OutboxMessage.attempt_count,
                ).where(
                    models.OutboxMessage.id == outbox_id,
                    models.OutboxMessage.tenant_id.in_(allowed_tenants),
                )
            )
        ).one_or_none()
        if row is None:
            raise DeliveryRecoveryNotFound("outbox_not_found")
        expected_status = row.status
        expected_attempt_count = row.attempt_count
        if row.status != "FAILED":
            audit_detail = await session.scalar(
                select(models.AuditLog.detail)
                .where(
                    models.AuditLog.tenant_id == row.tenant_id,
                    models.AuditLog.category == "delivery_recovery",
                    models.AuditLog.action == "RETRY_CONFIRMED_FAILURE",
                    models.AuditLog.subject_type == "outbox",
                    models.AuditLog.subject_id == str(outbox_id),
                )
                .order_by(models.AuditLog.created_at.desc())
                .limit(1)
            )
            if isinstance(audit_detail, dict):
                stored_status = audit_detail.get("expected_status")
                stored_attempt_count = audit_detail.get("expected_attempt_count")
                if stored_status == "FAILED" and isinstance(stored_attempt_count, int):
                    expected_status = stored_status
                    expected_attempt_count = stored_attempt_count
        return row.tenant_id, expected_status, expected_attempt_count


@router.post("/delivery/{outbox_id}/retry")
async def delivery_retry(request: Request, outbox_id: uuid.UUID) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    try:
        tenant_id, expected_status, expected_attempt_count = await _legacy_retry_fence(
            outbox_id=outbox_id,
            allowed_tenants=principal.allowed_tenants,
        )
        await retry_failed_outbox(
            outbox_id=outbox_id,
            required_tenant_id=tenant_id,
            actor=principal.actor,
            expected_status=expected_status,
            expected_attempt_count=expected_attempt_count,
            review_reason=_LEGACY_DELIVERY_RETRY_REASON,
            verification_source=_LEGACY_DELIVERY_VERIFICATION_SOURCE,
            principal=principal,
        )
    except (
        DeliveryRecoveryNotFound,
        DeliveryRecoveryConflict,
        DeliveryRecoveryValidationError,
    ) as exc:
        raise _legacy_delivery_recovery_http_error(exc) from exc
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
    "instagram": "admin.accounts.channel_kind.instagram",
    "telegram": "Bot Token",
    "whatsapp": "Cloud API",
    "feishu": "admin.accounts.channel_kind.feishu",
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
    channel_kind = _CHANNEL_KINDS[channel]
    status = translate(channel_kind) if channel_kind.startswith("admin.") else channel_kind
    if not enabled:
        status = translate("admin.accounts.disabled")
    inner = (
        f'{icon}<span class="channel-name">{html.escape(label)}</span>'
        f'<span class="channel-kind">{html.escape(status)}</span>'
    )
    if not enabled:
        return (
            f'<div class="channel-tile disabled" data-channel="{channel}" '
            f'aria-disabled="true" aria-label="{html.escape(translate("admin.accounts.disabled_aria", provider=label))}">{inner}</div>'
        )
    current = ' aria-current="true"' if selected else ""
    return (
        f'<a class="channel-tile" data-channel="{channel}"{current} '
        f'href="/admin/integrations/accounts/new/{channel}" '
        f'aria-label="{html.escape(translate("admin.accounts.connect_aria", provider=label))}">{inner}</a>'
    )


def _channel_setup_head(channel: str, subtitle: str) -> str:
    return (
        '<div class="channel-setup-head">'
        f"{_channel_icon(channel)}<div><h2>{translate('admin.accounts.connect_heading', provider=html.escape(_CHANNEL_LABELS[channel]))}</h2>"
        f"<p>{html.escape(subtitle)}</p></div></div>"
    )


@router.get("/integrations/accounts/new/{provider}", response_class=HTMLResponse)
@router.get("/integrations/accounts", response_class=HTMLResponse)
@router.get("/accounts", response_class=HTMLResponse)
async def accounts_page(request: Request) -> Response:
    return await _legacy_tenant_get_redirect(request, "/channels")

    # Legacy implementation retained below for the staged POST compatibility adapters.
    principal = await _web_principal(request)
    tenant_id = principal.tenant_id or sorted(principal.allowed_tenants)[0]
    redirect_target = f"/app/t/{tenant_id}/channels"
    if request.query_params:
        redirect_target = f"{redirect_target}?{request.query_params}"
    return RedirectResponse(
        redirect_target,
        status_code=status.HTTP_303_SEE_OTHER,
    )
    oauth_banner = ""
    if request.query_params.get("provider") == "x":
        oauth_status = request.query_params.get("status")
        if oauth_status == "connected":
            oauth_banner = (
                f'<div class="banner ok">{translate("admin.accounts.oauth.connected")}</div>'
            )
        elif oauth_status == "processing":
            oauth_banner = (
                f'<div class="banner info">{translate("admin.accounts.oauth.processing")}</div>'
            )
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
                f'<div class="banner err">{translate("admin.accounts.oauth.error")}'
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
                    .where(
                        models.PlatformAccount.tenant_id.in_(tenants),
                        _account_read_scope(principal, tenants),
                    )
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
            f'<span class="pill err">{translate("admin.accounts.kill_switch.active")}</span>'
            if stopped
            else f'<span class="pill ok">{translate("admin.accounts.kill_switch.normal")}</span>'
        )
        ks_btn = (
            translate("admin.accounts.kill_switch.disable")
            if stopped
            else translate("admin.accounts.kill_switch.enable")
        )
        ks_cls = "btn-ghost" if stopped else "btn-danger"
        auto_target = "BOT_DRAFT_ONLY" if a.automation_default == "BOT_ACTIVE" else "BOT_ACTIVE"
        auto_label = (
            translate("admin.accounts.automation.draft")
            if a.automation_default == "BOT_ACTIVE"
            else translate("admin.accounts.automation.auto")
        )
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
                xchat_form = f"""<form class="inline" method="post" action="/admin/accounts/{a.id}/xchat"><input type="hidden" name="expected_config_version" value="{a.config_version}"><label class="sr-only" for="xchat-pin-{a.id}">XChat PIN</label><input id="xchat-pin-{a.id}" type="password" name="xchat_pin" inputmode="numeric" pattern="[0-9]{{4}}" maxlength="4" placeholder="XChat PIN" required><input type="hidden" name="csrf_token" value="{csrf}"><button class="btn-sm btn-ghost">{translate("admin.accounts.restore_xchat")}</button></form>"""
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
                probe_result = (
                    f'<span class="pill ok">{translate("admin.accounts.probe.passed")}</span>'
                )
            elif probe_status == "UNKNOWN":
                probe_result = (
                    f'<span class="pill neutral">{translate("admin.accounts.probe.unknown")}</span>'
                )
            else:
                probe_result = (
                    f'<span class="pill err">{translate("admin.accounts.probe.error")}</span>'
                )
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
                f"<div>{translate('admin.accounts.probe.label')} {probe_result}</div>"
                f"<div class='muted'>Mailbox {html.escape(mailbox)}</div>"
                f"<div class='muted'>Security {html.escape(smtp_security)}</div>"
                f"<div class='muted'>{translate('admin.accounts.probe.checked_at')} {html.escape(checked_at)}</div>"
                f"<div class='muted'>{translate('admin.common.error')} {html.escape(error_code)}</div>"
                f"<div class='muted'>{translate('admin.accounts.probe.note')}</div>"
            )
        account_rows += (
            f"<tr><td>{html.escape(a.platform)}</td><td>{html.escape(a.name)}</td>"
            f"<td>{_pill(a.status)}</td><td>{channel_status}</td>"
            f"<td>{_pill(a.automation_default)}</td><td>{ks_pill}</td>"
            f"""<td>{automation_form}
<form class="inline" method="post" action="/admin/killswitch/toggle"><input type="hidden" name="csrf_token" value="{csrf}"><input type="hidden" name="scope" value="account"><input type="hidden" name="account_id" value="{a.id}"><input type="hidden" name="tenant_id" value="{html.escape(a.tenant_id)}"><button class="btn-sm {ks_cls}">{ks_btn}</button></form>{xchat_form}</td></tr>"""
        )
    account_rows = (
        account_rows
        or f"<tr><td colspan='7' class='muted'>{translate('admin.accounts.empty')}</td></tr>"
    )

    job_rows = (
        "".join(
            f"<tr><td><a href='/admin/integrations/provisioning-jobs/{row.id}'><code>{str(row.id)[:8]}</code></a></td>"
            f"<td>{html.escape(row.platform)}</td>"
            f"<td>{_pill('PROCESSING' if row.status == 'FAILED' and provisioning_job_is_in_flight(row) else row.status)}</td>"
            f"<td class='muted'>{html.escape(row.current_step)}</td>"
            f"<td class='muted'>{html.escape(row.last_error_code or '—')}</td></tr>"
            for row in jobs
        )
        or f"<tr><td colspan='5' class='muted'>{translate('admin.accounts.no_jobs')}</td></tr>"
    )
    common = (
        f'<input type="hidden" name="csrf_token" value="{csrf}">'
        + _tenant_input(principal)
        + _input("brand_id", "Brand", required=True, value="default")
        + _input("name", translate("admin.accounts.display_name_optional"), required=False)
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
            translate("admin.accounts.xchat_pin_optional"),
            secret=True,
            required=False,
        )
        if settings.xchat_enabled
        else ""
    )
    xchat_manual_input = (
        _input(
            "xchat_pin",
            translate("admin.accounts.xchat_pin_optional"),
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
    requested_channel = request.path_params.get("provider") or request.query_params.get(
        "connect", ""
    )
    if request.path_params.get("provider") and requested_channel not in _CHANNEL_LABELS:
        raise HTTPException(status_code=404, detail="integration_provider_not_found")
    selected_channel = (
        requested_channel
        if requested_channel in _CHANNEL_LABELS and channel_enabled[requested_channel]
        else ""
    )
    channel_notice = ""
    if requested_channel in _CHANNEL_LABELS and not channel_enabled[requested_channel]:
        channel_notice = (
            f'<div class="banner info">{translate("admin.accounts.channel_disabled_notice")}</div>'
        )
    channel_tiles = "".join(
        _channel_tile(
            channel,
            enabled=channel_enabled[channel],
            selected=channel == selected_channel,
        )
        for channel in _CHANNEL_LABELS
    )
    channel_picker = f"""<section class="channel-section" aria-labelledby="add-channel-title">
<div class="channel-heading"><div><h2 id="add-channel-title">{translate("admin.accounts.add_channel")}</h2><p>{translate("admin.accounts.choose_platform")}</p></div><span class="muted">{translate("admin.accounts.platform_count", count=len(_CHANNEL_LABELS))}</span></div>
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
{_channel_setup_head("x", translate("admin.accounts.x.subtitle"))}
<form class="channel-form" method="post" action="/admin/oauth/x/start">{oauth_fields()}{xchat_oauth_input}
<dl class="channel-meta"><dt>Callback URI</dt><dd><code>{html.escape(x_callback)}</code></dd><dt>{translate("admin.accounts.authorization_scope")}</dt><dd>Read and write{"" if not (settings.x_legacy_dm_enabled or settings.xchat_enabled) else " and Direct message"}</dd></dl>
<button class="btn-block">{translate("admin.accounts.continue_x")}</button></form>
<details class="advanced-connect"><summary>{translate("admin.accounts.advanced_token")}</summary><div class="advanced-body"><form method="post" action="/admin/connect/x">{common}{_input("consumer_key", "Consumer Key", secret=True)}{_input("consumer_secret", "Consumer Secret", secret=True)}{_input("access_token", "Access Token", secret=True)}{_input("access_token_secret", "Access Token Secret", secret=True)}<input type="hidden" name="environment" value="oauth">{xchat_manual_input}<button class="btn-block">{translate("admin.accounts.connect_x")}</button></form></div></details></section>"""
    elif selected_channel == "facebook":
        selected_panel = f"""<section class="channel-setup" id="channel-setup">
{_channel_setup_head("facebook", translate("admin.accounts.facebook.subtitle"))}
<form class="channel-form" method="post" action="/admin/oauth/meta/start">{oauth_fields()}<input type="hidden" name="platform" value="facebook">
<dl class="channel-meta"><dt>Callback URI</dt><dd><code>{html.escape(meta_callback)}</code></dd><dt>{translate("admin.accounts.permissions")}</dt><dd>pages_show_list · pages_messaging · pages_manage_metadata{" · pages_read_engagement · pages_read_user_content · pages_manage_engagement" if facebook_comments else ""}</dd></dl>
<button class="btn-block">{translate("admin.accounts.continue_facebook")}</button></form>
<details class="advanced-connect"><summary>{translate("admin.accounts.advanced_page_token")}</summary><div class="advanced-body"><form method="post" action="/admin/connect/meta">{common}<input type="hidden" name="platform" value="facebook">{_input("external_account_id", "Facebook Page ID")}{_input("access_token", "Page Access Token", secret=True)}{_input("app_secret", "Meta App Secret", secret=True)}{_input("app_id", "Meta App ID", required=False)}{_input("app_public_id", "Existing App Public ID", required=False)}{_input("verify_token", "Webhook Verify Token", secret=True)}{facebook_policy_fields}<button class="btn-block">{translate("admin.accounts.connect_facebook")}</button></form></div></details></section>"""
    elif selected_channel == "instagram":
        selected_panel = f"""<section class="channel-setup" id="channel-setup">
{_channel_setup_head("instagram", translate("admin.accounts.instagram.subtitle"))}
<div class="channel-mode-grid"><div class="channel-mode"><h3>{translate("admin.accounts.instagram.login")}</h3><p class="hint">{translate("admin.accounts.instagram.no_page")}</p><form method="post" action="/admin/oauth/instagram/start">{oauth_fields()}<dl class="channel-meta"><dt>Callback URI</dt><dd><code>{html.escape(instagram_callback)}</code></dd>{f"<dt>{translate('admin.accounts.comment_permission')}</dt><dd>instagram_business_manage_comments</dd>" if instagram_comments else ""}</dl><button class="btn-block">{translate("admin.accounts.continue_instagram")}</button></form></div>
<div class="channel-mode"><h3>{translate("admin.accounts.facebook.login")}</h3><p class="hint">{translate("admin.accounts.instagram.facebook_page")}</p><form method="post" action="/admin/oauth/meta/start">{oauth_fields()}<input type="hidden" name="platform" value="instagram"><dl class="channel-meta"><dt>Callback URI</dt><dd><code>{html.escape(meta_callback)}</code></dd>{f"<dt>{translate('admin.accounts.comment_permission')}</dt><dd>pages_read_engagement · instagram_manage_comments</dd>" if instagram_comments else ""}</dl><button class="btn-block">{translate("admin.accounts.continue_facebook")}</button></form></div></div>
<details class="advanced-connect"><summary>{translate("admin.accounts.advanced_page_token")}</summary><div class="advanced-body"><form method="post" action="/admin/connect/meta">{common}<input type="hidden" name="platform" value="instagram">{_input("external_account_id", "Instagram Professional Account ID")}{_input("page_id", "Facebook Page ID")}{_input("access_token", "Page Access Token", secret=True)}{_input("app_secret", "Meta App Secret", secret=True)}{_input("app_id", "Meta App ID", required=False)}{_input("app_public_id", "Existing App Public ID", required=False)}{_input("verify_token", "Webhook Verify Token", secret=True)}{instagram_policy_fields}<button class="btn-block">{translate("admin.accounts.connect_instagram")}</button></form></div></details></section>"""
    elif selected_channel == "telegram":
        selected_panel = f"""<section class="channel-setup" id="channel-setup">
{_channel_setup_head("telegram", translate("admin.accounts.telegram.subtitle"))}
<form class="channel-form" method="post" action="/admin/connect/telegram">{common}{_input("token", "Bot Token", secret=True)}<p class="hint">{translate("admin.accounts.telegram.token_hint")}</p><button class="btn-block">{translate("admin.accounts.connect_telegram")}</button></form></section>"""
    elif selected_channel == "whatsapp":
        selected_panel = f"""<section class="channel-setup" id="channel-setup">
{_channel_setup_head("whatsapp", translate("admin.accounts.whatsapp.subtitle"))}
<form class="channel-form" method="post" action="/admin/connect/whatsapp">{common}{_input("external_account_id", "Phone Number ID")}{_input("access_token", "Access Token", secret=True)}{_input("app_secret", "Meta App Secret", secret=True)}{_input("app_id", "Meta App ID", required=False)}{_input("app_public_id", "Existing App Public ID", required=False)}{_input("verify_token", "Webhook Verify Token", secret=True)}<button class="btn-block">{translate("admin.accounts.connect_whatsapp")}</button></form></section>"""
    elif selected_channel == "feishu":
        selected_panel = f"""<section class="channel-setup" id="channel-setup">
{_channel_setup_head("feishu", translate("admin.accounts.feishu.subtitle"))}
<form class="channel-form" method="post" action="/admin/connect/feishu">{common}{_input("app_id", "App ID")}{_input("app_secret", "App Secret", secret=True)}{_input("verification_token", "Verification Token", secret=True)}{_input("encrypt_key", "Encrypt Key", secret=True)}<input type="hidden" name="api_base_url" value="{FEISHU_API_BASE_URL}"><input type="hidden" name="group_mode" value="{FEISHU_GROUP_MODE}"><input type="hidden" name="automation_default" value="BOT_DRAFT_ONLY"><button class="btn-block">{translate("admin.accounts.connect_feishu")}</button></form></section>"""
    elif selected_channel == "email":
        selected_panel = f"""<section class="channel-setup" id="channel-setup">
{_channel_setup_head("email", translate("admin.accounts.email.subtitle"))}
<form class="channel-form" method="post" action="/admin/connect/email">{common}<div class="channel-form-grid"><div>{_input("email_address", "Email Address", input_type="email", autocomplete="email")}</div><div>{_input("from_name", translate("admin.accounts.from_name_optional"), required=False)}</div><div>{_input("username", "Username", autocomplete="username")}</div><div>{_input("password", "Password", secret=True, autocomplete="current-password")}</div><div>{_input("imap_host", "IMAP Host", value="imap.larksuite.com")}</div><div>{_input("imap_port", "IMAP Port", value="993", input_type="number", inputmode="numeric", min=1, max=65535)}</div><div>{_input("smtp_host", "SMTP Host", value="smtp.larksuite.com")}</div><div>{_input("smtp_port", translate("admin.accounts.smtp_port_optional"), required=False, input_type="number", inputmode="numeric", min=1, max=65535)}</div><div><label for="f-email-smtp-security">SMTP Security</label><select id="f-email-smtp-security" name="smtp_security" required><option value="ssl" selected>{translate("admin.accounts.ssl_default")}</option><option value="starttls">{translate("admin.accounts.starttls_default")}</option></select></div><div>{_input("mailbox", "Mailbox", value="INBOX")}</div><div class="span-2"><label for="f-email-domain-policy">{translate("admin.accounts.internal_domain")}</label><select id="f-email-domain-policy" name="internal_domain_policy" required><option value="ignore" selected>{translate("admin.accounts.internal_ignore")}</option><option value="allow">{translate("admin.accounts.internal_allow")}</option></select><p class="hint">{translate("admin.accounts.internal_hint")}</p></div></div><input type="hidden" name="automation_default" value="BOT_DRAFT_ONLY"><button class="btn-block">{translate("admin.accounts.connect_email")}</button></form></section>"""
    account_card = f"""<section class="card"><h2>{translate("admin.accounts.title")}</h2><div class="tablewrap"><table><thead><tr><th>{translate("common.platform")}</th><th>{translate("admin.common.name")}</th><th>{translate("common.status")}</th><th>{translate("admin.accounts.message_channel")}</th><th>{translate("admin.accounts.automation_policy")}</th><th>{translate("admin.accounts.kill_switch")}</th><th>{translate("admin.common.operation")}</th></tr></thead><tbody>{account_rows}</tbody></table></div></section>"""
    jobs_card = f"""<section class="card"><h2>{translate("admin.accounts.jobs")}</h2><p class="hint">{translate("admin.accounts.jobs_hint")}</p><div class="tablewrap"><table><thead><tr><th>ID</th><th>{translate("common.platform")}</th><th>{translate("common.status")}</th><th>{translate("admin.common.step")}</th><th>{translate("admin.common.error")}</th></tr></thead><tbody>{job_rows}</tbody></table></div></section>"""
    if principal.is_superadmin:
        page_heading = translate("admin.accounts.title")
        page_description = translate("admin.accounts.description")
        body = f"""<h1>{page_heading}</h1><p class="lede">{page_description}</p>
{oauth_banner}{channel_picker}{selected_panel}{account_card}{jobs_card}"""
    else:
        page_heading = translate("admin.accounts.authorization_title")
        page_description = translate("admin.accounts.authorization_description")
        body = f"""<h1>{page_heading}</h1><p class="lede">{page_description}</p>
{oauth_banner}{channel_picker}{selected_panel}{account_card}{jobs_card}"""
    page_title = translate("admin.accounts.title")
    response = HTMLResponse(
        _page(
            page_title,
            body,
            active="accounts",
            show_users=principal.is_admin,
            principal=principal,
        )
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
        f'<form class="saas-card saas-danger-action" method="post" action="/admin/killswitch/toggle">'
        f'<input type="hidden" name="csrf_token" value="{csrf}">'
        '<input type="hidden" name="scope" value="global">'
        f'<input type="hidden" name="tenant_id" value="{html.escape(tenant)}">'
        f'<input type="hidden" name="enabled" value="{"false" if flags[index] else "true"}">'
        f'<div class="saas-card-header"><div><h2>{html.escape(tenant)}</h2>'
        f"<p>{translate('admin.safety.tenant_hint')}</p></div>"
        f"{status_badge('enabled' if flags[index] else 'disabled')}</div>"
        '<div class="saas-card-body">'
        f'<p class="saas-danger-action-impact">{escape(translate("admin.safety.description"))}</p>'
        f'<label class="saas-field" for="f-safety-password-{index}"><span>{translate("admin.users.bootstrap_password")}</span>'
        f'<input id="f-safety-password-{index}" name="bootstrap_password" type="password" '
        'autocomplete="current-password" required></label>'
        f'<button class="saas-button {"danger" if not flags[index] else ""}" type="submit">'
        f"{translate('admin.safety.disable_global') if flags[index] else translate('admin.safety.enable_global')}</button></div></form>"
        for index, tenant in enumerate(tenants)
    )
    page_title = translate("admin.safety.title")
    body = f"""<section class="saas-next-action"><div><div class="saas-eyebrow">{escape(translate("nav.security_controls"))}</div>
<h2>{escape(page_title)}</h2><p>{escape(translate("admin.safety.description"))}</p></div>
{secondary_action("/admin/system/health", translate("nav.system_health"), small=True)}</section>
<div class="saas-section-header"><div class="saas-section-header-copy"><h2>{escape(translate("system.overview.global_kill_switch"))}</h2>
<p>{escape(translate("admin.safety.tenant_hint"))}</p></div></div><div class="saas-grid two">{controls}</div>"""
    response = HTMLResponse(
        render_saas_page(
            principal=principal,
            title=page_title,
            description=translate("admin.safety.description"),
            body=body,
            active_navigation="system-safety",
            tenant_id=None,
            system_admin=True,
        )
    )
    return _ensure_csrf(response, request, csrf)


@router.post("/accounts/{account_id}/xchat")
async def enable_account_xchat(request: Request, account_id: uuid.UUID) -> Response:
    principal = await _web_principal(request, require_admin=False)
    if isinstance(principal, Response):
        return principal
    if not get_settings().xchat_enabled:
        raise HTTPException(status_code=503, detail="xchat_disabled")
    form = await _form(request)
    _require_csrf(request, form)
    async with get_session_factory()() as session:
        account = await session.scalar(
            select(models.PlatformAccount).where(
                models.PlatformAccount.id == account_id,
                or_(
                    _account_read_scope(principal, principal.allowed_tenants),
                    select(models.AccountReauthorizationGrant.id).where(
                        models.AccountReauthorizationGrant.platform_account_id == models.PlatformAccount.id,
                        models.AccountReauthorizationGrant.tenant_id == models.PlatformAccount.tenant_id,
                        models.AccountReauthorizationGrant.user_id == principal.user_id,
                        models.AccountReauthorizationGrant.active.is_(True),
                    ).exists() if principal.user_id is not None else False,
                ),
            )
        )
    if (
        account is None
        or account.tenant_id not in principal.allowed_tenants
        or account.platform != "x"
    ):
        raise HTTPException(status_code=404, detail="x_account_not_found")
    try:
        expected_config_version = int(form.get("expected_config_version") or "")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid_integer:expected_config_version") from exc
    if expected_config_version < 1:
        raise HTTPException(status_code=422, detail="invalid_integer:expected_config_version")
    pin = (form.get("xchat_pin") or "").strip()
    if len(pin) != 4 or not pin.isdigit():
        raise HTTPException(status_code=422, detail="invalid_xchat_pin")
    try:
        await repair_channel_xchat(
            tenant_id=account.tenant_id,
            account_id=account_id,
            actor=ChannelActor(
                actor=principal.actor,
                role="ADMIN" if principal.is_workspace_admin else "USER",
                user_id=principal.user_id,
                session_id=principal.session_id,
            ),
            pin=pin,
            expected_config_version=expected_config_version,
        )
    except ChannelManagementError as exc:
        raise _channel_management_http_error(exc) from exc
    except XChatActivationError as exc:
        logger.warning("xchat activation failed account=%s code=%s", account_id, exc.code)
        return notice(
            translate("oauth.xchat.activation_failed_title"),
            f"{exc.operator_message} ({exc.code})",
            status_code=exc.status_code,
        )
    except Exception as exc:  # noqa: BLE001 - platform boundary; never echo the PIN
        logger.exception(
            "unexpected xchat activation failure account=%s type=%s",
            account_id,
            type(exc).__name__,
        )
        return notice(
            translate("oauth.xchat.activation_failed_title"),
            translate("oauth.xchat.activation_failed"),
            status_code=500,
        )
    return RedirectResponse(
        f"/app/t/{account.tenant_id}/channels/accounts/{account_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


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
        account = await session.scalar(
            select(models.PlatformAccount).where(
                models.PlatformAccount.id == account_id,
                _account_read_scope(principal, principal.allowed_tenants),
            )
        )
    if account is None or account.tenant_id not in principal.allowed_tenants:
        raise HTTPException(status_code=404, detail="account_not_found")
    try:
        await set_channel_account_automation(
            tenant_id=account.tenant_id,
            account_id=account_id,
            actor=ChannelActor(
                actor=principal.actor,
                role="ADMIN",
                user_id=principal.user_id,
                session_id=principal.session_id,
            ),
            target=target,  # type: ignore[arg-type]
            expected_config_version=account.config_version,
        )
    except ChannelManagementError as exc:
        raise _channel_management_http_error(exc) from exc
    return RedirectResponse(
        f"/app/t/{account.tenant_id}/channels/accounts/{account_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


async def _acquire_global_kill_switch_lock(redis, tenant_id: str) -> tuple[str, str]:
    lock_key = f"killswitch:global-mutation-lock:{tenant_id}"
    lock_token = uuid.uuid4().hex
    acquired = await redis.set(lock_key, lock_token, nx=True, ex=30)
    if not acquired:
        raise HTTPException(status_code=409, detail="global_killswitch_change_in_progress")
    return lock_key, lock_token


async def _release_global_kill_switch_lock(redis, lock_key: str, lock_token: str) -> None:
    await redis.eval(
        """
        if redis.call('get', KEYS[1]) == ARGV[1] then
            return redis.call('del', KEYS[1])
        end
        return 0
        """,
        1,
        lock_key,
        lock_token,
    )


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
        try:
            await require_bootstrap_reauthentication(form.get("bootstrap_password", ""))
        except SystemUserAuthenticationError as exc:
            raise HTTPException(status_code=401, detail=exc.code) from exc
        except SystemUserValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.code) from exc
        enabled_value = form.get("enabled")
        if enabled_value not in {"true", "false"}:
            raise HTTPException(status_code=422, detail="invalid_killswitch_enabled")
        target_enabled = enabled_value == "true"
        key = f"killswitch:global:{tenant_id}"
    elif scope == "account":
        principal.require_tenant_admin()
        account_id = form.get("account_id", "")
        try:
            parsed_account_id = uuid.UUID(account_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid_account_id") from exc
        async with get_session_factory()() as session:
            account = await session.scalar(
                select(models.PlatformAccount).where(
                    models.PlatformAccount.id == parsed_account_id,
                    _account_read_scope(principal, principal.allowed_tenants, tenant_id),
                )
            )
        if account is None or account.tenant_id != tenant_id:
            raise HTTPException(status_code=404, detail="account_not_found")
        redis = aioredis.from_url(settings.redis_url)
        try:
            current_enabled = bool(
                await redis.exists(f"killswitch:account:{tenant_id}:{account_id}")
            )
        finally:
            await redis.aclose()
        enabled_value = form.get("enabled")
        if enabled_value not in {None, "true", "false"}:
            raise HTTPException(status_code=422, detail="invalid_killswitch_enabled")
        enabled = enabled_value == "true" if enabled_value is not None else not current_enabled
        try:
            await set_channel_account_kill_switch(
                tenant_id=tenant_id,
                account_id=parsed_account_id,
                actor=ChannelActor(
                    actor=principal.actor,
                    role="ADMIN",
                    user_id=principal.user_id,
                    session_id=principal.session_id,
                ),
                enabled=enabled,
            )
        except ChannelManagementError as exc:
            raise _channel_management_http_error(exc) from exc
        return RedirectResponse(
            f"/app/t/{tenant_id}/channels/accounts/{account_id}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    else:
        raise HTTPException(status_code=422, detail="invalid_killswitch_scope")
    redis = aioredis.from_url(settings.redis_url)
    lock_key = ""
    lock_token = ""
    try:
        lock_key, lock_token = await _acquire_global_kill_switch_lock(redis, tenant_id)
        previous_enabled = await redis.get(key) is not None
        if target_enabled:
            await redis.set(key, "1")
        else:
            await redis.delete(key)
        try:
            async with get_session_factory()() as session, session.begin():
                session.add(
                    models.AuditLog(
                        tenant_id=tenant_id,
                        category="global_safety",
                        actor=principal.actor,
                        action="SET_GLOBAL_KILL_SWITCH",
                        subject_type="tenant",
                        subject_id=tenant_id,
                        detail={
                            "enabled": target_enabled,
                            "previous_enabled": previous_enabled,
                            "actor_session_id": str(principal.session_id),
                        },
                    )
                )
        except Exception as audit_error:
            lock_still_owned = await redis.get(lock_key) == lock_token
            if not lock_still_owned:
                logger.critical(
                    "global kill switch outcome unknown after lock loss tenant=%s audit_error=%s",
                    tenant_id,
                    type(audit_error).__name__,
                )
                try:
                    async with get_session_factory()() as session, session.begin():
                        session.add(
                            models.AuditLog(
                                tenant_id=tenant_id,
                                category="global_safety",
                                actor=principal.actor,
                                action="GLOBAL_KILL_SWITCH_OUTCOME_UNKNOWN",
                                subject_type="tenant",
                                subject_id=tenant_id,
                                detail={
                                    "attempted_enabled": target_enabled,
                                    "previous_enabled": previous_enabled,
                                    "outcome": "mutation_lock_lost",
                                    "actor_session_id": str(principal.session_id),
                                },
                            )
                        )
                except Exception:  # noqa: BLE001 - critical log is the final durable fallback
                    logger.exception(
                        "could not persist lock-loss outcome audit tenant=%s",
                        tenant_id,
                    )
                raise HTTPException(
                    status_code=503,
                    detail="global_killswitch_outcome_unknown",
                ) from audit_error
            try:
                if previous_enabled:
                    await redis.set(key, "1")
                else:
                    await redis.delete(key)
            except Exception as compensation_error:
                logger.critical(
                    "global kill switch outcome unknown tenant=%s audit_error=%s compensation_error=%s",
                    tenant_id,
                    type(audit_error).__name__,
                    type(compensation_error).__name__,
                )
                try:
                    async with get_session_factory()() as session, session.begin():
                        session.add(
                            models.AuditLog(
                                tenant_id=tenant_id,
                                category="global_safety",
                                actor=principal.actor,
                                action="GLOBAL_KILL_SWITCH_OUTCOME_UNKNOWN",
                                subject_type="tenant",
                                subject_id=tenant_id,
                                detail={
                                    "attempted_enabled": target_enabled,
                                    "previous_enabled": previous_enabled,
                                    "actor_session_id": str(principal.session_id),
                                },
                            )
                        )
                except Exception:  # noqa: BLE001 - critical log is the final durable fallback
                    logger.exception(
                        "could not persist global kill switch outcome-unknown audit tenant=%s",
                        tenant_id,
                    )
                raise HTTPException(
                    status_code=503,
                    detail="global_killswitch_outcome_unknown",
                ) from audit_error
            raise HTTPException(
                status_code=503,
                detail="global_killswitch_audit_failed_rolled_back",
            ) from audit_error
    finally:
        if lock_key and lock_token:
            try:
                await _release_global_kill_switch_lock(redis, lock_key, lock_token)
            except Exception:  # noqa: BLE001 - lock TTL bounds recovery if release fails
                logger.exception("could not release global kill switch lock tenant=%s", tenant_id)
        await redis.aclose()
    target = "/admin/system/safety" if scope == "global" else "/admin/integrations/accounts"
    return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)
