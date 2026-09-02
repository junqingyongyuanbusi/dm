import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlencode, urlsplit

import redis.asyncio as aioredis
from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from markupsafe import Markup
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import aliased

from social_reply.application.account_management.admin import (
    _csrf,
    _ensure_csrf,
    _form,
    _require_csrf,
    _secure_cookie,
)
from social_reply.application.account_management.admin_console import (
    _health_age,
    _load_health_metrics,
    _raw_action_condition,
    _raw_warning_condition,
)
from social_reply.application.account_management.agent_control_plane import (
    AgentControlPlaneConflict,
    AgentControlPlaneValidationError,
    create_agent,
    normalize_agent_slug,
)
from social_reply.application.account_management.auth import Principal, current_principal
from social_reply.application.account_management.channel_management import (
    ChannelActor,
    ChannelConflictError,
    ChannelManagementError,
    ChannelNotFoundError,
    ChannelPermissionError,
    ChannelValidationError,
    assign_channel_account_owner,
    build_provisioning_command,
    rename_channel_account,
    repair_channel_xchat,
    retry_channel_job,
    set_channel_account_automation,
    set_channel_account_kill_switch,
    set_channel_account_status,
    submit_channel_provisioning,
)
from social_reply.application.account_management.feishu_handoff_service import (
    FeishuHandoffConflict,
    FeishuHandoffError,
    FeishuHandoffNotFound,
    load_feishu_handoff_snapshot,
    save_feishu_handoff_config,
    send_feishu_handoff_test_card,
    set_feishu_handoff_operator_status,
    upsert_feishu_handoff_operator,
)
from social_reply.application.account_management.human_workflow import (
    HumanWorkflowError,
    claim_human_work_item,
    resolve_human_work_item,
    send_human_reply,
)
from social_reply.application.account_management.jobs import (
    provisioning_job_is_in_flight,
    public_job,
    requires_secret_resubmission,
)
from social_reply.application.account_management.meta_credentials import (
    facebook_app_credentials,
    instagram_app_credentials,
)
from social_reply.application.account_management.reply_prompt_policy import (
    ReplyBusinessPromptConflict,
    ReplyBusinessPromptScopeError,
)
from social_reply.application.account_management.reply_prompt_trial import (
    ReplyBusinessPromptTrialExecutionError,
    ReplyBusinessPromptTrialRateLimited,
    ReplyBusinessPromptTrialResult,
    ReplyBusinessPromptTrialUnavailable,
    ReplyBusinessPromptTrialValidationError,
    run_reply_business_prompt_trial,
)
from social_reply.application.account_management.reply_prompt_web import (
    DeployAgentVersionCommand,
    ReplyBusinessPromptEditorView,
    RollbackReplyBusinessPromptCommand,
    SaveReplyBusinessPromptCommand,
    execute_deploy_agent_version,
    execute_rollback_reply_business_prompt,
    execute_save_reply_business_prompt,
    load_reply_business_prompt_editor_view,
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
from social_reply.application.account_management.templating import (
    render_template,
    trusted_html,
)
from social_reply.application.account_management.ui_i18n import (
    reset_request_location,
    set_request_location,
    translate,
)
from social_reply.application.account_management.x_app import x_app_credentials
from social_reply.application.account_management.xchat_activation import XChatActivationError
from social_reply.application.knowledge.commands import (
    ConfirmKnowledgeEnglishBatchCommand,
    ConfirmKnowledgeEnglishCommand,
    CreateKnowledgeDocumentCommand,
    DeleteKnowledgeDraftCommand,
    ImportKnowledgeBatchCommand,
    KnowledgeApplicationError,
    KnowledgeConflictError,
    KnowledgeNotFoundError,
    SetKnowledgeOfficialContactCommand,
    execute_confirm_knowledge_english,
    execute_confirm_knowledge_english_batch,
    execute_create_knowledge_document,
    execute_delete_knowledge_draft,
    execute_import_knowledge_batch,
    execute_set_knowledge_official_contact,
)
from social_reply.application.knowledge.publication import (
    BulkPublishKnowledgeCommand,
    PublishKnowledgeCommand,
    UnpublishKnowledgeCommand,
    execute_bulk_publish_knowledge,
    execute_publish_knowledge,
    execute_unpublish_knowledge,
)
from social_reply.application.knowledge.queries import (
    GetKnowledgeDocumentQuery,
    ListKnowledgeDocumentsQuery,
    SearchPublishedKnowledgeQuery,
    execute_get_knowledge_document,
    execute_list_knowledge_documents,
    execute_search_published_knowledge,
    knowledge_review_condition,
    load_knowledge_filter_values,
)
from social_reply.application.knowledge.upload import (
    MAX_KNOWLEDGE_UPLOAD_BYTES,
    decode_knowledge_csv_upload,
    parse_protected_values,
)
from social_reply.application.message_delivery.recovery import (
    DeliveryRecoveryConflict,
    DeliveryRecoveryNotFound,
    DeliveryRecoveryValidationError,
    resolve_needs_review_outbox,
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
from social_reply.domain.reply.business_prompt import (
    BUSINESS_PROMPT_CHANGE_NOTE_MAX_CHARS,
    BUSINESS_PROMPT_MAX_CHARS,
    BusinessPromptValidationError,
)
from social_reply.domain.reply.guard import has_contact_like
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.shared.config import DEFAULT_TENANT_ID, get_settings

router = APIRouter(tags=["saas-console"])
logger = logging.getLogger(__name__)

SYSTEM_AUDIT_CATEGORIES = frozenset(
    {
        "authentication",
        "session_management",
        "user_management",
        "role_change",
        "global_safety",
        "security_configuration",
    }
)
_SYSTEM_AUDIT_CATEGORY_ALIASES = {
    "admin_auth": "authentication",
}
_SYSTEM_AUDIT_SUBJECT_TYPES = frozenset(
    {"admin_user", "admin_session", "bootstrap_admin", "tenant", "system_security"}
)
_SYSTEM_AUDIT_SECRET_KEY_PARTS = (
    "password",
    "secret",
    "token",
    "hash",
    "credential",
    "private_key",
)
_SYSTEM_AUDIT_SAFE_DETAIL_KEYS = frozenset(
    {
        "username",
        "role",
        "previous_role",
        "status",
        "previous_status",
        "first_login",
        "must_change_password",
        "sessions_revoked",
        "revoked_session_count",
        "enabled",
        "previous_enabled",
        "attempted_enabled",
        "outcome",
        "error_code",
    }
)


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
    draft_text: str | None = None
    decision_generation: int | None = None
    review_action: str | None = None
    expected_status: str | None = None
    expected_attempt_count: int | None = None
    delivery_error_code: str | None = None


@dataclass(frozen=True)
class AgentCardView:
    name: str
    agent_id: str
    scope_label: str
    prompt_text: str
    status_html: Markup
    mode_label: str
    mode_html: Markup
    channels_label: str
    active_accounts: int
    account_count: int
    knowledge_label: str
    published_count: int
    release_label: str
    release_text: str
    readiness_label: str
    readiness_percent: int
    next_step_label: str
    next_step: str
    open_href: str
    open_label: str


@dataclass(frozen=True)
class AgentControlPlaneView:
    name: str
    status: str
    version_revision: int
    deployed_version_revision: int | None


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
    if tenant_id != DEFAULT_TENANT_ID:
        raise HTTPException(status_code=404, detail="tenant_workspace_not_found")
    principal.require_tenant(tenant_id)
    return principal


async def _require_tenant_admin_principal(
    request: Request,
    tenant_id: str,
) -> Principal | Response:
    principal = await _require_tenant_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    principal.require_tenant_admin()
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
        reviewable_draft_condition(),
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
    if not principal.is_admin:
        return sorted(account_brands) if account_brands else [DEFAULT_TENANT_ID]
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
    control_plane_brands = set(
        (
            await session.execute(
                select(models.Agent.legacy_brand_id).where(
                    models.Agent.tenant_id == tenant_id,
                    models.Agent.status == "active",
                )
            )
        ).scalars()
    )
    return sorted(
        account_brands
        | prompt_brands
        | knowledge_brands
        | control_plane_brands
        | {DEFAULT_TENANT_ID}
    )


async def _load_agent_control_plane_views(
    session,
    tenant_id: str,
    agent_ids: list[str],
) -> dict[str, AgentControlPlaneView]:
    if not agent_ids:
        return {}
    agents = (
        (
            await session.execute(
                select(models.Agent).where(
                    models.Agent.tenant_id == tenant_id,
                    models.Agent.legacy_brand_id.in_(agent_ids),
                )
            )
        )
        .scalars()
        .all()
    )
    if not agents:
        return {}
    agent_record_ids = [agent.id for agent in agents]
    versions = (
        (
            await session.execute(
                select(models.AgentVersion)
                .where(
                    models.AgentVersion.tenant_id == tenant_id,
                    models.AgentVersion.agent_id.in_(agent_record_ids),
                )
                .order_by(models.AgentVersion.revision.desc())
            )
        )
        .scalars()
        .all()
    )
    deployments = (
        (
            await session.execute(
                select(models.AgentDeployment)
                .where(
                    models.AgentDeployment.tenant_id == tenant_id,
                    models.AgentDeployment.agent_id.in_(agent_record_ids),
                    models.AgentDeployment.environment == "production",
                )
                .order_by(models.AgentDeployment.revision.desc())
            )
        )
        .scalars()
        .all()
    )
    latest_version_by_agent: dict[uuid.UUID, models.AgentVersion] = {}
    version_by_id: dict[uuid.UUID, models.AgentVersion] = {}
    for version in versions:
        version_by_id[version.id] = version
        latest_version_by_agent.setdefault(version.agent_id, version)
    latest_deployment_by_agent: dict[uuid.UUID, models.AgentDeployment] = {}
    for deployment in deployments:
        latest_deployment_by_agent.setdefault(deployment.agent_id, deployment)

    views: dict[str, AgentControlPlaneView] = {}
    for agent in agents:
        latest_version = latest_version_by_agent.get(agent.id)
        if latest_version is None:
            continue
        latest_deployment = latest_deployment_by_agent.get(agent.id)
        deployed_version = (
            version_by_id.get(latest_deployment.agent_version_id)
            if latest_deployment is not None
            else None
        )
        views[agent.legacy_brand_id] = AgentControlPlaneView(
            name=agent.name,
            status=agent.status,
            version_revision=latest_version.revision,
            deployed_version_revision=(
                deployed_version.revision if deployed_version is not None else None
            ),
        )
    return views


def _display_agent_name(agent_id: str) -> str:
    if agent_id == "default":
        return translate("agent.default_name")
    normalized = agent_id.replace("_", " ").replace("-", " ").strip()
    return f"{normalized.title()} Agent"


async def _load_agent_display_name(session, tenant_id: str, agent_id: str) -> str:
    identity = (
        await session.execute(
            select(models.Agent.name, models.Agent.created_by).where(
                models.Agent.tenant_id == tenant_id,
                models.Agent.legacy_brand_id == agent_id,
                models.Agent.status == "active",
            )
        )
    ).one_or_none()
    if identity is not None and identity.created_by.startswith("user:"):
        return identity.name
    return _display_agent_name(agent_id)


def _agent_lifecycle_context(
    tenant_id: str,
    agent_id: str,
    *,
    current_stage: str = "",
) -> dict[str, object]:
    agent_root = _agent_root(tenant_id, agent_id)
    return {
        "lifecycle_eyebrow": translate("agent.lifecycle.eyebrow"),
        "lifecycle_title": translate("agent.lifecycle.title"),
        "lifecycle_description": translate("agent.lifecycle.description"),
        "lifecycle_stages": (
            {
                "key": "train",
                "label": translate("agent.lifecycle.train"),
                "description": translate("agent.lifecycle.train_description"),
                "href": f"{agent_root}/instructions",
                "current": current_stage == "train",
            },
            {
                "key": "test",
                "label": translate("agent.lifecycle.test"),
                "description": translate("agent.lifecycle.test_description"),
                "href": f"{agent_root}/test",
                "current": current_stage == "test",
            },
            {
                "key": "deploy",
                "label": translate("agent.lifecycle.deploy"),
                "description": translate("agent.lifecycle.deploy_description"),
                "href": f"{agent_root}/channels",
                "current": current_stage == "deploy",
            },
            {
                "key": "analyze",
                "label": translate("agent.lifecycle.analyze"),
                "description": translate("agent.lifecycle.analyze_description"),
                "href": f"{agent_root}/activity",
                "current": current_stage == "analyze",
            },
        ),
    }


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
    workbench: bool = False,
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
            workbench=workbench,
        )
    )


@router.get("/app", response_class=HTMLResponse)
async def tenant_selector(request: Request) -> Response:
    principal = await _require_web_principal(request)
    if isinstance(principal, Response):
        return principal
    target_tenant = (
        principal.tenant_id
        if principal.tenant_id in principal.allowed_tenants
        else min(principal.allowed_tenants, default="")
    )
    if not target_tenant:
        raise HTTPException(status_code=403, detail="tenant_access_denied")
    if target_tenant != DEFAULT_TENANT_ID:
        raise HTTPException(status_code=404, detail="tenant_workspace_not_found")
    return RedirectResponse(
        _tenant_root(target_tenant),
        status_code=status.HTTP_303_SEE_OTHER,
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
                    translate("home.metric.human"),
                    detail=translate(
                        "home.oldest_wait",
                        age=format_age(inbox_summary.oldest_human_at),
                    ),
                ),
                metric_card(
                    inbox_summary.draft_count,
                    translate("home.metric.drafts"),
                    detail=translate(
                        "home.oldest_wait",
                        age=format_age(inbox_summary.oldest_draft_at),
                    ),
                ),
                metric_card(
                    inbox_summary.delivery_count,
                    translate("home.metric.delivery"),
                    detail=translate(
                        "home.oldest_wait",
                        age=format_age(inbox_summary.oldest_delivery_at),
                    ),
                ),
            )
        )
        attention_description = translate("home.attention_admin_description")
    else:
        attention_cards = "".join(
            (
                metric_card(
                    int(account_count or 0),
                    translate("home.metric.my_accounts"),
                    detail=translate(
                        "home.accounts_available",
                        count=int(active_account_count or 0),
                    ),
                ),
                metric_card(
                    inbox_summary.human_count,
                    translate("home.metric.human"),
                    detail=translate(
                        "home.oldest_wait",
                        age=format_age(inbox_summary.oldest_human_at),
                    ),
                ),
                metric_card(
                    int(message_count or 0),
                    translate("home.metric.today_messages"),
                    detail=translate("home.own_accounts_only"),
                ),
            )
        )
        attention_description = translate("home.attention_user_description")
    readiness_rows = "".join(
        (
            _progress_row(
                True,
                translate("home.agent_scope_count", count=len(agent_ids)),
            ),
            _progress_row(
                bool(published_knowledge_count),
                translate(
                    "home.knowledge_published_count",
                    count=int(published_knowledge_count or 0),
                ),
            ),
            _progress_row(
                int(active_account_count or 0) == int(account_count or 0) and bool(account_count),
                translate(
                    "home.channel_availability",
                    active=int(active_account_count or 0),
                    total=int(account_count or 0),
                ),
                warning=bool(account_count),
            ),
            _progress_row(True, translate("home.safety_path")),
        )
    )
    lifecycle_agent_id = agent_ids[0] if agent_ids else DEFAULT_TENANT_ID
    body = render_template(
        "tenant/home.html",
        **_agent_lifecycle_context(tenant_id, lifecycle_agent_id),
        next_action_html=trusted_html(next_action),
        attention_title=translate("home.attention_title"),
        attention_description=attention_description,
        attention_cards_html=trusted_html(attention_cards),
        readiness_title=translate("home.readiness_title"),
        readiness_description=translate("home.readiness_description"),
        view_agents_action_html=trusted_html(
            secondary_action(
                f"{_tenant_root(tenant_id)}/agents",
                translate("home.view_agents"),
                small=True,
            )
        ),
        readiness_rows_html=trusted_html(readiness_rows),
        today_title=translate("home.today_overview"),
        today_description=translate("home.today_overview_description"),
        today_messages_html=trusted_html(
            metric_card(int(message_count or 0), translate("home.metric.today_messages"))
        ),
        published_knowledge_html=trusted_html(
            metric_card(
                int(published_knowledge_count or 0),
                translate("home.metric.published_knowledge"),
            )
        ),
        recent_activity_title=translate("home.recent_activity"),
        recent_activity_description=translate("home.recent_activity_description"),
        view_all_action_html=trusted_html(
            secondary_action(
                (
                    f"{_tenant_root(tenant_id)}/audit"
                    if principal.is_admin
                    else f"{_tenant_root(tenant_id)}/activity"
                ),
                translate("home.view_all"),
                small=True,
            )
        ),
        recent_activity_rows=tuple(
            {
                "time": format_datetime(audit.created_at),
                "actor": audit.actor,
                "action": audit.action,
                "subject_type": audit.subject_type,
            }
            for audit in recent_audits
        ),
        recent_activity_empty_html=trusted_html(
            empty_state(
                translate("home.empty_activity_title"),
                translate("home.empty_activity_description"),
            )
        ),
        time_label=translate("common.time"),
        action_label=translate("common.action"),
        resource_label=translate("common.resource"),
    )
    return _render_page(
        principal=principal,
        tenant_id=tenant_id,
        title=translate("home.title"),
        description=translate(
            "home.description",
            tenant_id=tenant_id,
            count=inbox_summary.total,
        ),
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
        title = translate("next.delivery_title")
        description = translate(
            "next.delivery_description",
            count=summary.delivery_count,
            age=format_age(summary.oldest_delivery_at),
        )
        href = f"{inbox_href}?queue=delivery"
        action_label = translate("next.delivery_action")
    elif summary.human_count:
        title = translate("next.human_title")
        description = translate(
            "next.human_description",
            count=summary.human_count,
            age=format_age(summary.oldest_human_at),
        )
        href = f"{inbox_href}?queue=human"
        action_label = translate("next.human_action")
    elif summary.draft_count:
        title = translate("next.draft_title")
        description = translate(
            "next.draft_description",
            count=summary.draft_count,
            age=format_age(summary.oldest_draft_at),
        )
        href = f"{inbox_href}?queue=drafts"
        action_label = translate("next.draft_action")
    elif not account_count:
        if principal.is_admin:
            title = translate("next.admin_account_title")
            description = translate("next.admin_account_description")
            href = f"{_tenant_root(tenant_id)}/agents/default/channels"
            action_label = translate("next.admin_account_action")
        else:
            title = translate("next.user_account_title")
            description = translate("next.user_account_description")
            href = f"{_tenant_root(tenant_id)}/channels"
            action_label = translate("next.user_account_action")
    else:
        title = translate("next.clear_title")
        description = translate("next.clear_description")
        href = f"{_tenant_root(tenant_id)}/agents"
        action_label = translate("next.clear_action")
    return (
        '<section class="saas-next-action"><div><div class="saas-eyebrow">'
        f"{escape(translate('next.eyebrow'))}</div>"
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
        control_plane_views = await _load_agent_control_plane_views(
            session,
            tenant_id,
            agent_ids,
        )
        cards: list[AgentCardView] = []
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
                _build_agent_card_view(
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    accounts=accounts,
                    published_count=int(published_count or 0),
                    prompt=prompt,
                    control_plane=control_plane_views.get(agent_id),
                )
            )
    lifecycle_agent_id = agent_ids[0] if agent_ids else DEFAULT_TENANT_ID
    body = render_template(
        "tenant/agent_list.html",
        **_agent_lifecycle_context(tenant_id, lifecycle_agent_id),
        list_summary=translate("agent.list_summary", count=len(agent_ids)),
        scope_description=translate("agent.list_scope_description"),
        cards=cards,
    )
    return _render_page(
        principal=principal,
        tenant_id=tenant_id,
        title=translate("nav.agents"),
        description=translate("agent.list_description"),
        body=body,
        active_navigation="agents",
        inbox_count=inbox_summary.total,
        primary_action_html=(
            primary_action(
                f"{_tenant_root(tenant_id)}/agents/new",
                translate("agent.create.action"),
            )
            if principal.is_admin
            else ""
        ),
    )


async def _agent_create_page_response(
    request: Request,
    principal: Principal,
    *,
    tenant_id: str,
    name: str = "",
    slug: str = "",
    description: str = "",
    error_key: str = "",
    response_status: int = status.HTTP_200_OK,
) -> Response:
    async with get_session_factory()() as session:
        inbox_summary = await _load_inbox_summary(session, principal, tenant_id)
    csrf_token = _csrf(request)
    body = render_template(
        "tenant/agent_create.html",
        form_action=f"{_tenant_root(tenant_id)}/agents",
        cancel_href=f"{_tenant_root(tenant_id)}/agents",
        csrf_token=csrf_token,
        name=name,
        slug=slug,
        description=description,
        error_message=(translate(error_key) if error_key else ""),
        eyebrow=translate("agent.create.eyebrow"),
        form_title=translate("agent.create.form_title"),
        form_description=translate("agent.create.form_description"),
        name_label=translate("agent.create.name_label"),
        name_placeholder=translate("agent.create.name_placeholder"),
        slug_label=translate("agent.create.slug_label"),
        slug_placeholder=translate("agent.create.slug_placeholder"),
        slug_help=translate("agent.create.slug_help"),
        description_label=translate("agent.create.description_label"),
        description_placeholder=translate("agent.create.description_placeholder"),
        description_help=translate("agent.create.description_help"),
        cancel_label=translate("common.cancel"),
        submit_label=translate("agent.create.submit"),
        next_title=translate("agent.create.next_title"),
        next_description=translate("agent.create.next_description"),
        steps=(
            {
                "number": "01",
                "title": translate("agent.create.step_identity"),
                "description": translate("agent.create.step_identity_description"),
            },
            {
                "number": "02",
                "title": translate("agent.create.step_instructions"),
                "description": translate("agent.create.step_instructions_description"),
            },
            {
                "number": "03",
                "title": translate("agent.create.step_deploy"),
                "description": translate("agent.create.step_deploy_description"),
            },
        ),
        safety_title=translate("agent.create.safety_title"),
        safety_description=translate("agent.create.safety_description"),
    )
    response = _render_page(
        principal=principal,
        tenant_id=tenant_id,
        title=translate("agent.create.title"),
        description=translate("agent.create.page_description"),
        body=body,
        active_navigation="agents",
        inbox_count=inbox_summary.total,
        breadcrumbs=(
            (translate("nav.agents"), f"{_tenant_root(tenant_id)}/agents"),
            (translate("agent.create.title"), None),
        ),
    )
    response.status_code = response_status
    response.headers["Cache-Control"] = "no-store"
    return _ensure_csrf(response, request, csrf_token)


@router.get("/app/t/{tenant_id}/agents/new", response_class=HTMLResponse)
async def new_agent_page(request: Request, tenant_id: str) -> Response:
    principal = await _require_tenant_admin_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    return await _agent_create_page_response(request, principal, tenant_id=tenant_id)


@router.post("/app/t/{tenant_id}/agents")
async def create_tenant_agent(request: Request, tenant_id: str) -> Response:
    principal = await _require_tenant_admin_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    if set(form) != {"csrf_token", "name", "slug", "description"}:
        raise HTTPException(status_code=422, detail="agent_create_fields_invalid")
    name = form.get("name", "")
    slug = form.get("slug", "")
    description = form.get("description", "")
    try:
        async with get_session_factory()() as session:
            agent = await create_agent(
                session,
                tenant_id=tenant_id,
                slug=slug,
                name=name,
                description=description,
                actor=principal.actor,
            )
            await session.commit()
    except AgentControlPlaneConflict:
        return await _agent_create_page_response(
            request,
            principal,
            tenant_id=tenant_id,
            name=name,
            slug=slug,
            description=description,
            error_key="agent.create.error_conflict",
            response_status=status.HTTP_409_CONFLICT,
        )
    except AgentControlPlaneValidationError:
        return await _agent_create_page_response(
            request,
            principal,
            tenant_id=tenant_id,
            name=name,
            slug=slug,
            description=description,
            error_key="agent.create.error_invalid",
            response_status=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    return RedirectResponse(
        _prompt_canonical_location(
            tenant_id,
            agent.legacy_brand_id,
            notice="agent_created",
        ),
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _build_agent_card_view(
    *,
    tenant_id: str,
    agent_id: str,
    accounts: list[models.PlatformAccount],
    published_count: int,
    prompt: models.ReplyBusinessPrompt | None,
    control_plane: AgentControlPlaneView | None = None,
) -> AgentCardView:
    active_accounts = [account for account in accounts if account.status == "active"]
    modes = {account.automation_default for account in accounts}
    if not accounts:
        automation_mode = "unconfigured"
        status = "unconfigured"
        next_step = translate("agent.connect_first_channel")
    elif len(modes) > 1:
        automation_mode = "mixed"
        status = "degraded"
        next_step = translate("agent.confirm_automation_mode")
    else:
        automation_mode = next(iter(modes))
        status = "healthy" if len(active_accounts) == len(accounts) else "degraded"
        next_step = (
            translate("agent.check_unavailable_channels")
            if status == "degraded"
            else translate("agent.check_recent_activity")
        )
    prompt_text = (
        translate("agent.prompt_version", revision=prompt.revision)
        if prompt
        else translate("agent.default_prompt")
    )
    readiness_checks = (
        bool(prompt),
        bool(published_count),
        bool(accounts) and len(active_accounts) == len(accounts),
        (
            control_plane is None
            or control_plane.deployed_version_revision
            == control_plane.version_revision
        ),
    )
    readiness_percent = round(sum(readiness_checks) / len(readiness_checks) * 100)
    if control_plane is None:
        release_text = translate("agent.card.release_legacy")
    elif control_plane.deployed_version_revision is None:
        release_text = translate(
            "agent.card.release_not_deployed",
            version=control_plane.version_revision,
        )
    else:
        release_text = translate(
            "agent.card.release_deployed",
            version=control_plane.version_revision,
            deployed=control_plane.deployed_version_revision,
        )
    return AgentCardView(
        name=control_plane.name if control_plane is not None else _display_agent_name(agent_id),
        agent_id=agent_id,
        scope_label=translate("agent.scope"),
        prompt_text=prompt_text,
        status_html=trusted_html(status_badge(status)),
        mode_label=translate("agent.card.mode"),
        mode_html=trusted_html(status_badge(automation_mode)),
        channels_label=translate("agent.card.channels"),
        active_accounts=len(active_accounts),
        account_count=len(accounts),
        knowledge_label=translate("agent.card.knowledge"),
        published_count=published_count,
        release_label=translate("agent.card.release"),
        release_text=release_text,
        readiness_label=translate("agent.card.readiness"),
        readiness_percent=readiness_percent,
        next_step_label=translate("agent.next_step"),
        next_step=next_step,
        open_href=f"{_agent_root(tenant_id, agent_id)}/overview",
        open_label=translate("agent.open_short"),
    )


def _render_agent_card(
    *,
    tenant_id: str,
    agent_id: str,
    accounts: list[models.PlatformAccount],
    published_count: int,
    prompt: models.ReplyBusinessPrompt | None,
    control_plane: AgentControlPlaneView | None = None,
) -> str:
    """Render one card for compatibility with focused view tests and callers."""
    return render_template(
        "components/agent_card.html",
        card=_build_agent_card_view(
            tenant_id=tenant_id,
            agent_id=agent_id,
            accounts=accounts,
            published_count=published_count,
            prompt=prompt,
            control_plane=control_plane,
        ),
    )


_AGENT_SECTIONS = {
    "overview",
    "instructions",
    "model",
    "channels",
    "knowledge",
    "flow",
    "activity",
}

_PROMPT_NOTICE_KEYS = {
    "agent_created": ("success", "agent.create.success"),
    "saved": ("success", "admin.prompt.banner.saved"),
    "deployed": ("success", "admin.prompt.banner.deployed"),
    "rolled_back": ("success", "admin.prompt.banner.rolled_back"),
    "deployment_conflict": ("danger", "admin.prompt.banner.deployment_conflict"),
    "deployment_requires_channel": (
        "warning",
        "admin.prompt.banner.deployment_requires_channel",
    ),
    "revision_conflict": ("danger", "admin.prompt.banner.revision_conflict"),
    "prompt_invalid": ("danger", "admin.prompt.banner.prompt_invalid"),
}


def _agent_section_tabs(tenant_id: str, agent_id: str, section: str) -> str:
    agent_base = _agent_root(tenant_id, agent_id)
    return tabs(
        (
            ("overview", f"{agent_base}/overview", translate("agent.tab.overview")),
            (
                "instructions",
                f"{agent_base}/instructions",
                translate("agent.tab.instructions"),
            ),
            ("model", f"{agent_base}/model", translate("agent.tab.model")),
            ("channels", f"{agent_base}/channels", translate("agent.tab.channels")),
            (
                "knowledge",
                f"{agent_base}/knowledge",
                translate("agent.tab.knowledge"),
            ),
            ("test", f"{agent_base}/test", translate("agent.tab.test")),
            ("flow", f"{agent_base}/flow", translate("agent.tab.flow")),
            ("activity", f"{agent_base}/activity", translate("agent.tab.activity")),
        ),
        section,
    )


def _prompt_expected_revision(form: dict[str, str]) -> int:
    try:
        expected_revision = int(form.get("expected_revision", ""))
    except ValueError as exc:
        raise ReplyBusinessPromptConflict("reply_business_prompt_revision_conflict") from exc
    if expected_revision < 0:
        raise ReplyBusinessPromptConflict("reply_business_prompt_revision_conflict")
    return expected_revision


def _deployment_expected_revision(form: dict[str, str]) -> int:
    try:
        expected_revision = int(form.get("expected_deployment_revision", ""))
    except ValueError as exc:
        raise AgentControlPlaneConflict("agent_deployment_revision_conflict") from exc
    if expected_revision < 0:
        raise AgentControlPlaneConflict("agent_deployment_revision_conflict")
    return expected_revision


def _prompt_canonical_location(
    tenant_id: str,
    agent_id: str,
    *,
    notice: str = "",
) -> str:
    location = f"{_agent_root(tenant_id, agent_id)}/instructions"
    if notice:
        return f"{location}?{urlencode({'notice': notice})}"
    return location


def _render_user_agent_behavior_summary(
    *,
    accounts: list[models.PlatformAccount],
    published_knowledge_count: int,
) -> str:
    automation_modes = {account.automation_default for account in accounts}
    if not automation_modes:
        automation_summary = translate("agent.instructions.summary.unconfigured")
    elif automation_modes == {"BOT_DRAFT_ONLY"}:
        automation_summary = translate("agent.instructions.summary.draft_only")
    elif automation_modes == {"BOT_ACTIVE"}:
        automation_summary = translate("agent.instructions.summary.active")
    else:
        automation_summary = translate("agent.instructions.summary.mixed")
    fact_source_summary = translate(
        "agent.instructions.summary.fact_source",
        count=published_knowledge_count,
    )
    return f"""<div class="saas-grid three" style="margin-top:0">
<section class="saas-card"><div class="saas-card-header"><div><h2>{escape(translate("agent.instructions.summary.mode_title"))}</h2></div></div>
<div class="saas-card-body"><p>{escape(automation_summary)}</p></div></section>
<section class="saas-card"><div class="saas-card-header"><div><h2>{escape(translate("agent.instructions.summary.fact_title"))}</h2></div></div>
<div class="saas-card-body"><p>{escape(fact_source_summary)}</p></div></section>
<section class="saas-card"><div class="saas-card-header"><div><h2>{escape(translate("agent.instructions.summary.handoff_title"))}</h2></div></div>
<div class="saas-card-body"><p>{escape(translate("agent.instructions.summary.handoff"))}</p></div></section>
</div><section class="saas-alert" style="margin-top:18px">{escape(translate("agent.instructions.summary.read_only"))}</section>"""


def _render_reply_prompt_trial_result(
    result: ReplyBusinessPromptTrialResult | None,
    *,
    trial_failed: bool,
) -> str:
    if trial_failed:
        return (
            '<section class="saas-alert danger" style="margin-top:16px">'
            f"{escape(translate('admin.prompt.trial_failed'))}</section>"
        )
    if result is None:
        return ""
    metadata = definition_list(
        (
            (translate("common.action"), result.action),
            (translate("admin.overview.intent"), result.intent or "—"),
            (translate("admin.prompt.risk"), result.risk_level),
            (translate("admin.conversation.confidence"), f"{result.confidence:.2f}"),
            (
                translate("admin.prompt.reason_codes"),
                ", ".join(result.reason_codes) or "—",
            ),
            (translate("agent.instructions.trial.duration"), f"{result.duration_ms} ms"),
        )
    )
    reply_html = (
        f'<div class="saas-alert" style="margin-top:12px">{escape(result.reply_text)}</div>'
        if result.reply_text
        else f'<p class="saas-muted">{escape(translate("admin.prompt.trial_no_reply"))}</p>'
    )
    return (
        '<section class="saas-alert" style="margin-top:16px">'
        f"{escape(translate('admin.prompt.trial_result_notice'))}</section>"
        f"{metadata}{reply_html}"
    )


def _render_admin_reply_prompt_editor(
    editor_view: ReplyBusinessPromptEditorView,
    *,
    csrf_token: str,
    notice: str = "",
    trial_result: ReplyBusinessPromptTrialResult | None = None,
    trial_failed: bool = False,
) -> str:
    canonical_root = f"{_agent_root(editor_view.tenant_id, editor_view.brand_id)}/instructions"
    notice_html = ""
    notice_presentation = _PROMPT_NOTICE_KEYS.get(notice)
    if notice_presentation is not None:
        tone, message_key = notice_presentation
        notice_html = (
            f'<section class="saas-alert {tone}">{escape(translate(message_key))}</section>'
        )
    feature_enabled = get_settings().reply_business_prompt_enabled
    feature_status = (
        translate("admin.prompt.feature_enabled")
        if feature_enabled
        else translate("admin.prompt.feature_disabled")
    )
    metadata = definition_list(
        (
            ("Tenant", editor_view.tenant_id),
            ("Brand / Agent", editor_view.brand_id),
            (translate("admin.prompt.draft_revision"), editor_view.current_revision),
            (translate("agent.instructions.content_hash"), editor_view.content_hash),
            (translate("agent.instructions.last_updated"), format_datetime(editor_view.updated_at)),
            (translate("agent.instructions.updated_by"), editor_view.updated_by),
        )
    )
    has_unpublished_changes = (
        editor_view.latest_agent_version_id is not None
        and editor_view.latest_agent_version_id != editor_view.deployed_agent_version_id
    )
    if not editor_view.has_channel:
        release_tone = "degraded"
        release_status = translate("admin.prompt.release_connect_channel")
    elif editor_view.deployed_agent_version_id is None:
        release_tone = "degraded"
        release_status = translate("admin.prompt.release_not_deployed")
    elif has_unpublished_changes:
        release_tone = "degraded"
        release_status = translate("admin.prompt.release_changes_pending")
    else:
        release_tone = "active"
        release_status = translate("admin.prompt.release_current")
    release_action = ""
    if (
        editor_view.has_channel
        and editor_view.latest_agent_version_id is not None
        and has_unpublished_changes
    ):
        release_action = f"""<form method="post" action="{escape(canonical_root)}/releases/{editor_view.latest_agent_version_id}/deploy">
<input type="hidden" name="csrf_token" value="{escape(csrf_token)}">
<input type="hidden" name="expected_deployment_revision" value="{editor_view.deployment_revision}">
<button class="saas-button primary" type="submit">{escape(translate("admin.prompt.deploy_latest"))}</button></form>"""
    release_metadata = definition_list(
        (
            (
                translate("admin.prompt.latest_draft"),
                (
                    f"Agent v{editor_view.latest_agent_revision}"
                    if editor_view.latest_agent_revision is not None
                    else "—"
                ),
            ),
            (
                translate("admin.prompt.production_release"),
                (
                    f"Agent v{editor_view.deployed_agent_revision}"
                    if editor_view.deployed_agent_revision is not None
                    else translate("admin.prompt.release_none")
                ),
            ),
            (translate("admin.prompt.deployment_revision"), editor_view.deployment_revision),
        )
    )
    version_rows: list[str] = []
    for version in editor_view.versions:
        version_badges = " ".join(
            badge
            for badge in (
                (
                    status_badge("draft", label=translate("admin.prompt.latest_draft_badge"))
                    if version.is_active
                    else ""
                ),
                (
                    status_badge("active", label=translate("admin.prompt.production_badge"))
                    if version.is_deployed
                    else ""
                ),
            )
            if badge
        )
        rollback_form = ""
        if not version.is_active:
            rollback_form = f"""<form method="post" action="{escape(canonical_root)}/versions/{version.id}/rollback">
<input type="hidden" name="csrf_token" value="{escape(csrf_token)}">
<input type="hidden" name="expected_revision" value="{editor_view.current_revision}">
<button class="saas-button small" type="submit">{escape(translate("admin.prompt.restore_version"))}</button></form>"""
        deploy_form = ""
        if (
            editor_view.has_channel
            and version.agent_version_id is not None
            and not version.is_deployed
        ):
            deploy_form = f"""<form method="post" action="{escape(canonical_root)}/releases/{version.agent_version_id}/deploy">
<input type="hidden" name="csrf_token" value="{escape(csrf_token)}">
<input type="hidden" name="expected_deployment_revision" value="{editor_view.deployment_revision}">
<button class="saas-button small" type="submit">{escape(translate("admin.prompt.deploy_version"))}</button></form>"""
        operations = f'<div class="saas-action-row">{deploy_form}{rollback_form}</div>'
        version_rows.append(
            f"<tr><td>r{version.revision} {version_badges}<br>"
            f'<span class="saas-muted">Agent v{version.agent_revision or "—"}</span></td>'
            f"<td><details><summary>{escape(translate('admin.prompt.view_content'))}</summary>"
            f"<pre>{escape(version.content)}</pre></details></td>"
            f"<td>{escape(version.change_note or '—')}</td>"
            f'<td>{escape(version.created_by)}<br><span class="saas-muted">'
            f"{escape(format_datetime(version.created_at, include_year=True))} · "
            f"{escape(version.content_hash)}</span></td><td>{operations}</td></tr>"
        )
    version_history = "".join(version_rows) or (
        f'<tr><td colspan="5" class="saas-muted">'
        f"{escape(translate('admin.prompt.no_versions'))}</td></tr>"
    )
    return f"""{notice_html}
<section class="saas-alert {"success" if feature_enabled else "warning"}">{escape(feature_status)}</section>
<section class="saas-card" style="margin-top:18px"><div class="saas-card-header"><div><h2>{escape(translate("admin.prompt.release_title"))}</h2>
<p>{escape(translate("admin.prompt.release_description"))}</p></div>{status_badge(release_tone, label=release_status)}</div>
<div class="saas-card-body"><div class="saas-grid two" style="margin-top:0"><div>{release_metadata}</div>
<div><p class="saas-muted">{escape(translate("admin.prompt.release_safety"))}</p>{release_action}</div></div></div></section>
<div class="saas-grid two">
<section class="saas-card"><div class="saas-card-header"><div><h2>{escape(translate("admin.prompt.current_prompt"))}</h2>
<p>{escape(translate("admin.prompt.edit_hint", count=BUSINESS_PROMPT_MAX_CHARS))}</p></div></div>
<div class="saas-card-body"><form method="post" action="{escape(canonical_root)}/save">
<input type="hidden" name="csrf_token" value="{escape(csrf_token)}">
<input type="hidden" name="expected_revision" value="{editor_view.current_revision}">
<label class="saas-field"><span>{escape(translate("admin.prompt.instructions"))}</span>
<textarea name="content" maxlength="{BUSINESS_PROMPT_MAX_CHARS}" required style="min-height:300px">{escape(editor_view.current_content)}</textarea></label>
<label class="saas-field"><span>{escape(translate("admin.prompt.change_note"))}</span>
<input name="change_note" maxlength="{BUSINESS_PROMPT_CHANGE_NOTE_MAX_CHARS}" placeholder="{escape(translate("admin.prompt.change_note_placeholder"))}"></label>
<button class="saas-button primary" type="submit">{escape(translate("admin.prompt.save_version"))}</button></form></div></section>
<aside class="saas-card"><div class="saas-card-header"><div><h2>{escape(translate("agent.instructions.security_metadata"))}</h2></div></div>
<div class="saas-card-body">{metadata}<div class="saas-alert warning" style="margin-top:16px">{escape(translate("agent.instructions.safety_notice"))}</div></div></aside>
</div>
<section class="saas-card" id="prompt-trial"><div class="saas-card-header"><div><h2>{escape(translate("admin.prompt.trial"))}</h2>
<p>{escape(translate("admin.prompt.trial_hint"))}</p></div></div><div class="saas-card-body">
<form method="post" action="{escape(canonical_root)}/trial">
<input type="hidden" name="csrf_token" value="{escape(csrf_token)}">
<label class="saas-field"><span>{escape(translate("admin.prompt.test_message"))}</span>
<textarea name="text" maxlength="4000" required></textarea></label>
<button class="saas-button primary" type="submit">{escape(translate("admin.prompt.trial"))}</button></form>
{_render_reply_prompt_trial_result(trial_result, trial_failed=trial_failed)}</div></section>
<section class="saas-card"><div class="saas-card-header"><div><h2>{escape(translate("admin.prompt.version_history"))}</h2>
<p>{escape(translate("admin.prompt.version_history_hint"))}</p></div></div><div class="saas-table-wrap"><table class="saas-table"><thead><tr>
<th>{escape(translate("admin.prompt.version"))}</th><th>{escape(translate("admin.common.content"))}</th><th>{escape(translate("admin.prompt.note"))}</th><th>{escape(translate("admin.prompt.audit"))}</th><th>{escape(translate("admin.common.operation"))}</th>
</tr></thead><tbody>{version_history}</tbody></table></div></section>"""


async def _agent_instructions_page_response(
    request: Request,
    principal: Principal,
    *,
    tenant_id: str,
    agent_id: str,
    notice: str = "",
    trial_result: ReplyBusinessPromptTrialResult | None = None,
    trial_failed: bool = False,
) -> Response:
    async with get_session_factory()() as session:
        agent_ids = await _load_agent_ids(session, principal, tenant_id)
        if agent_id not in agent_ids:
            raise HTTPException(status_code=404, detail="agent_not_found")
        agent_display_name = await _load_agent_display_name(session, tenant_id, agent_id)
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
        if principal.is_admin:
            editor_view = await load_reply_business_prompt_editor_view(
                session,
                tenant_id=tenant_id,
                brand_id=agent_id,
            )
            section_body = _render_admin_reply_prompt_editor(
                editor_view,
                csrf_token=_csrf(request),
                notice=notice,
                trial_result=trial_result,
                trial_failed=trial_failed,
            )
        else:
            published_knowledge_count = int(
                await session.scalar(
                    select(func.count()).where(
                        models.KnowledgeDocument.tenant_id == tenant_id,
                        models.KnowledgeDocument.brand_id == agent_id,
                        models.KnowledgeDocument.status == "published",
                    )
                )
                or 0
            )
            section_body = _render_user_agent_behavior_summary(
                accounts=accounts,
                published_knowledge_count=published_knowledge_count,
            )

    automation_modes = {account.automation_default for account in accounts}
    agent_mode = (
        next(iter(automation_modes))
        if len(automation_modes) == 1
        else ("mixed" if automation_modes else "unconfigured")
    )
    response = _render_page(
        principal=principal,
        tenant_id=tenant_id,
        title=agent_display_name,
        description=translate(
            "agent.detail_description",
            agent_id=agent_id,
            mode=_plain_status_label(agent_mode),
            count=len(accounts),
        ),
        body=(f"{_agent_section_tabs(tenant_id, agent_id, 'instructions')}{section_body}"),
        active_navigation="agents",
        inbox_count=inbox_summary.total,
        breadcrumbs=(
            (translate("nav.agents"), f"{_tenant_root(tenant_id)}/agents"),
            (agent_display_name, None),
        ),
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/app/t/{tenant_id}/knowledge/documents")
async def create_tenant_knowledge_document(request: Request, tenant_id: str) -> Response:
    principal = await _require_tenant_admin_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    allowed_fields = {
        "csrf_token",
        "question",
        "reply",
        "brand_id",
        "platform",
        "category",
        "is_official_contact",
        "protected_values_json",
    }
    if set(form) - allowed_fields:
        raise HTTPException(status_code=422, detail="knowledge_document_fields_invalid")
    official_value = form.get("is_official_contact", "")
    if official_value not in {"", "true"}:
        raise HTTPException(status_code=422, detail="invalid_is_official_contact")
    try:
        protected_values = parse_protected_values(form.get("protected_values_json"))
        from social_reply.application.reply_decision.runner import _get_embedder

        async with get_session_factory()() as session:
            await execute_create_knowledge_document(
                session,
                CreateKnowledgeDocumentCommand(
                    required_tenant_id=tenant_id,
                    actor=principal.actor,
                    question=form.get("question", ""),
                    reply=form.get("reply", ""),
                    brand_id=form.get("brand_id", "") or "default",
                    platform=form.get("platform") or None,
                    category=form.get("category") or None,
                    is_official_contact=official_value == "true",
                    protected_values=protected_values,
                ),
                embedder=_get_embedder(),
            )
            await session.commit()
    except ValueError as exc:
        if isinstance(exc, KnowledgeApplicationError):
            raise _knowledge_http_error(exc) from exc
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RedirectResponse(
        _knowledge_location(tenant_id, notice="created"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/app/t/{tenant_id}/knowledge/import")
async def import_tenant_knowledge_batch(request: Request, tenant_id: str) -> Response:
    principal = await _require_tenant_admin_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    form = await request.form()
    _require_csrf(request, {"csrf_token": str(form.get("csrf_token") or "")})
    if set(form) - {"csrf_token", "brand_id", "file"}:
        raise HTTPException(status_code=422, detail="knowledge_import_fields_invalid")
    upload = form.get("file")
    read_upload = getattr(upload, "read", None)
    if not callable(read_upload):
        raise HTTPException(status_code=422, detail="knowledge_csv_required")
    raw = await read_upload(MAX_KNOWLEDGE_UPLOAD_BYTES + 1)
    source_name = str(getattr(upload, "filename", "") or "import.csv")[:256]
    try:
        csv_text = decode_knowledge_csv_upload(raw)
        from social_reply.application.reply_decision.runner import _get_embedder

        async with get_session_factory()() as session:
            report = await execute_import_knowledge_batch(
                session,
                ImportKnowledgeBatchCommand(
                    required_tenant_id=tenant_id,
                    actor=principal.actor,
                    csv_text=csv_text,
                    source_name=source_name,
                    brand_id_default=str(form.get("brand_id") or "default"),
                ),
                embedder=_get_embedder(),
            )
            await session.commit()
    except ValueError as exc:
        if isinstance(exc, KnowledgeApplicationError):
            raise _knowledge_http_error(exc) from exc
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RedirectResponse(
        _knowledge_location(
            tenant_id,
            notice="imported",
            inserted=report.inserted,
            skipped=report.skipped,
            blank=report.blank,
            batch_id=report.batch_id,
        ),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/app/t/{tenant_id}/knowledge/bulk-confirm-english")
async def confirm_tenant_knowledge_batch(request: Request, tenant_id: str) -> Response:
    principal = await _require_tenant_admin_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    try:
        import_batch_id = uuid.UUID(form.get("import_batch_id", ""))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid_import_batch_id") from exc
    try:
        async with get_session_factory()() as session:
            confirmed_count = await execute_confirm_knowledge_english_batch(
                session,
                ConfirmKnowledgeEnglishBatchCommand(
                    required_tenant_id=tenant_id,
                    actor=principal.actor,
                    import_batch_id=import_batch_id,
                ),
            )
            await session.commit()
    except KnowledgeApplicationError as exc:
        raise _knowledge_http_error(exc) from exc
    return RedirectResponse(
        _knowledge_location(tenant_id, notice="bulk_confirmed", count=confirmed_count),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/app/t/{tenant_id}/knowledge/bulk-publish")
async def publish_tenant_knowledge_batch(request: Request, tenant_id: str) -> Response:
    principal = await _require_tenant_admin_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    try:
        async with get_session_factory()() as session:
            result = await execute_bulk_publish_knowledge(
                session,
                BulkPublishKnowledgeCommand(
                    required_tenant_id=tenant_id,
                    actor=principal.actor,
                ),
            )
            await session.commit()
    except KnowledgeApplicationError as exc:
        raise _knowledge_http_error(exc) from exc
    return RedirectResponse(
        _knowledge_location(
            tenant_id,
            notice="bulk_published",
            published=result.published_count,
            skipped=result.skipped_count,
        ),
        status_code=status.HTTP_303_SEE_OTHER,
    )


async def _execute_tenant_knowledge_document_command(
    request: Request,
    tenant_id: str,
    document_id: uuid.UUID,
    operation: str,
) -> Response:
    principal = await _require_tenant_admin_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    try:
        async with get_session_factory()() as session:
            if operation == "confirm":
                await execute_confirm_knowledge_english(
                    session,
                    ConfirmKnowledgeEnglishCommand(
                        required_tenant_id=tenant_id,
                        actor=principal.actor,
                        document_id=document_id,
                        confirmation_reason=form.get("confirmation_reason", ""),
                    ),
                )
            elif operation == "official":
                target = form.get("target", "")
                if target not in {"true", "false"}:
                    raise HTTPException(
                        status_code=422,
                        detail="invalid_official_contact_target",
                    )
                await execute_set_knowledge_official_contact(
                    session,
                    SetKnowledgeOfficialContactCommand(
                        required_tenant_id=tenant_id,
                        actor=principal.actor,
                        document_id=document_id,
                        is_official_contact=target == "true",
                    ),
                )
            elif operation == "publish":
                await execute_publish_knowledge(
                    session,
                    PublishKnowledgeCommand(
                        required_tenant_id=tenant_id,
                        actor=principal.actor,
                        document_id=document_id,
                    ),
                )
            elif operation == "unpublish":
                await execute_unpublish_knowledge(
                    session,
                    UnpublishKnowledgeCommand(
                        required_tenant_id=tenant_id,
                        actor=principal.actor,
                        document_id=document_id,
                    ),
                )
            elif operation == "delete":
                await execute_delete_knowledge_draft(
                    session,
                    DeleteKnowledgeDraftCommand(
                        required_tenant_id=tenant_id,
                        actor=principal.actor,
                        document_id=document_id,
                    ),
                )
            else:
                raise HTTPException(status_code=404, detail="knowledge_operation_not_found")
            await session.commit()
    except KnowledgeApplicationError as exc:
        raise _knowledge_http_error(exc) from exc
    target = (
        _knowledge_location(tenant_id, notice="deleted")
        if operation == "delete"
        else f"{_tenant_root(tenant_id)}/knowledge/documents/{document_id}"
    )
    return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/app/t/{tenant_id}/knowledge/documents/{document_id}/confirm-english")
async def confirm_tenant_knowledge_english(
    request: Request, tenant_id: str, document_id: uuid.UUID
) -> Response:
    return await _execute_tenant_knowledge_document_command(
        request, tenant_id, document_id, "confirm"
    )


@router.post("/app/t/{tenant_id}/knowledge/documents/{document_id}/official-contact")
async def classify_tenant_knowledge_document(
    request: Request, tenant_id: str, document_id: uuid.UUID
) -> Response:
    return await _execute_tenant_knowledge_document_command(
        request, tenant_id, document_id, "official"
    )


@router.post("/app/t/{tenant_id}/knowledge/documents/{document_id}/publish")
async def publish_tenant_knowledge_document(
    request: Request, tenant_id: str, document_id: uuid.UUID
) -> Response:
    return await _execute_tenant_knowledge_document_command(
        request, tenant_id, document_id, "publish"
    )


@router.post("/app/t/{tenant_id}/knowledge/documents/{document_id}/unpublish")
async def unpublish_tenant_knowledge_document(
    request: Request, tenant_id: str, document_id: uuid.UUID
) -> Response:
    return await _execute_tenant_knowledge_document_command(
        request, tenant_id, document_id, "unpublish"
    )


@router.post("/app/t/{tenant_id}/knowledge/documents/{document_id}/delete")
async def delete_tenant_knowledge_document(
    request: Request, tenant_id: str, document_id: uuid.UUID
) -> Response:
    return await _execute_tenant_knowledge_document_command(
        request, tenant_id, document_id, "delete"
    )


@router.get(
    "/app/t/{tenant_id}/agents/{agent_id}/instructions",
    response_class=HTMLResponse,
)
async def agent_instructions_page(
    request: Request,
    tenant_id: str,
    agent_id: str,
    notice: str = "",
) -> Response:
    principal = await _require_tenant_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    return await _agent_instructions_page_response(
        request,
        principal,
        tenant_id=tenant_id,
        agent_id=agent_id,
        notice=notice,
    )


@router.post("/app/t/{tenant_id}/agents/{agent_id}/instructions/save")
async def save_agent_instructions(
    request: Request,
    tenant_id: str,
    agent_id: str,
) -> Response:
    principal = await _require_tenant_admin_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    async with get_session_factory()() as authorization_session:
        if agent_id not in await _load_agent_ids(
            authorization_session,
            principal,
            tenant_id,
        ):
            raise HTTPException(status_code=404, detail="agent_not_found")
    form = await _form(request)
    _require_csrf(request, form)
    if set(form) != {
        "csrf_token",
        "expected_revision",
        "content",
        "change_note",
    }:
        raise HTTPException(status_code=422, detail="reply_business_prompt_fields_invalid")
    try:
        command = SaveReplyBusinessPromptCommand(
            tenant_id=tenant_id,
            brand_id=agent_id,
            content=form.get("content", ""),
            expected_revision=_prompt_expected_revision(form),
            actor=principal.actor,
            change_note=form.get("change_note"),
        )
        async with get_session_factory()() as session:
            await execute_save_reply_business_prompt(session, command)
            await session.commit()
    except ReplyBusinessPromptConflict:
        notice = "revision_conflict"
    except BusinessPromptValidationError:
        notice = "prompt_invalid"
    except ReplyBusinessPromptScopeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    else:
        notice = "saved"
    return RedirectResponse(
        _prompt_canonical_location(tenant_id, agent_id, notice=notice),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post(
    "/app/t/{tenant_id}/agents/{agent_id}/instructions/releases/{agent_version_id}/deploy"
)
async def deploy_agent_instructions(
    request: Request,
    tenant_id: str,
    agent_id: str,
    agent_version_id: uuid.UUID,
) -> Response:
    principal = await _require_tenant_admin_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    async with get_session_factory()() as authorization_session:
        if agent_id not in await _load_agent_ids(
            authorization_session,
            principal,
            tenant_id,
        ):
            raise HTTPException(status_code=404, detail="agent_not_found")
    form = await _form(request)
    _require_csrf(request, form)
    if set(form) != {"csrf_token", "expected_deployment_revision"}:
        raise HTTPException(status_code=422, detail="agent_deployment_fields_invalid")
    try:
        command = DeployAgentVersionCommand(
            tenant_id=tenant_id,
            brand_id=agent_id,
            agent_version_id=agent_version_id,
            expected_deployment_revision=_deployment_expected_revision(form),
            actor=principal.actor,
        )
        async with get_session_factory()() as session:
            await execute_deploy_agent_version(session, command)
            await session.commit()
    except AgentControlPlaneConflict:
        notice = "deployment_conflict"
    except AgentControlPlaneValidationError as exc:
        if str(exc) == "agent_channel_scope_not_found":
            notice = "deployment_requires_channel"
        else:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    else:
        notice = "deployed"
    return RedirectResponse(
        _prompt_canonical_location(tenant_id, agent_id, notice=notice),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/app/t/{tenant_id}/agents/{agent_id}/instructions/versions/{version_id}/rollback")
async def rollback_agent_instructions(
    request: Request,
    tenant_id: str,
    agent_id: str,
    version_id: uuid.UUID,
) -> Response:
    principal = await _require_tenant_admin_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    async with get_session_factory()() as authorization_session:
        if agent_id not in await _load_agent_ids(
            authorization_session,
            principal,
            tenant_id,
        ):
            raise HTTPException(status_code=404, detail="agent_not_found")
    form = await _form(request)
    _require_csrf(request, form)
    if set(form) != {"csrf_token", "expected_revision"}:
        raise HTTPException(status_code=422, detail="reply_business_prompt_fields_invalid")
    try:
        command = RollbackReplyBusinessPromptCommand(
            tenant_id=tenant_id,
            brand_id=agent_id,
            source_version_id=version_id,
            expected_revision=_prompt_expected_revision(form),
            actor=principal.actor,
        )
        async with get_session_factory()() as session:
            await execute_rollback_reply_business_prompt(session, command)
            await session.commit()
    except ReplyBusinessPromptConflict:
        notice = "revision_conflict"
    except BusinessPromptValidationError:
        notice = "prompt_invalid"
    except ReplyBusinessPromptScopeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    else:
        notice = "rolled_back"
    return RedirectResponse(
        _prompt_canonical_location(tenant_id, agent_id, notice=notice),
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _no_store_error_response(*, status_code: int, message_key: str) -> HTMLResponse:
    response = HTMLResponse(
        f'<section class="saas-alert danger">{escape(translate(message_key))}</section>',
        status_code=status_code,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/app/t/{tenant_id}/agents/{agent_id}/instructions/trial")
async def trial_agent_instructions(
    request: Request,
    tenant_id: str,
    agent_id: str,
) -> Response:
    principal = await _require_tenant_admin_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    async with get_session_factory()() as authorization_session:
        if agent_id not in await _load_agent_ids(
            authorization_session,
            principal,
            tenant_id,
        ):
            raise HTTPException(status_code=404, detail="agent_not_found")
    form = await _form(request)
    _require_csrf(request, form)
    if set(form) != {"csrf_token", "text"}:
        raise HTTPException(status_code=422, detail="reply_business_prompt_fields_invalid")
    try:
        trial_result = await run_reply_business_prompt_trial(
            tenant_id=tenant_id,
            brand_id=agent_id,
            input_text=form.get("text", ""),
            actor=principal.actor,
        )
    except ReplyBusinessPromptTrialValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.code) from exc
    except ReplyBusinessPromptTrialRateLimited:
        return _no_store_error_response(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            message_key="agent.instructions.trial.rate_limited",
        )
    except ReplyBusinessPromptTrialUnavailable:
        return _no_store_error_response(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            message_key="agent.instructions.trial.unavailable",
        )
    except ReplyBusinessPromptScopeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ReplyBusinessPromptTrialExecutionError:
        return await _agent_instructions_page_response(
            request,
            principal,
            tenant_id=tenant_id,
            agent_id=agent_id,
            trial_failed=True,
        )
    return await _agent_instructions_page_response(
        request,
        principal,
        tenant_id=tenant_id,
        agent_id=agent_id,
        trial_result=trial_result,
    )


def _render_agent_test_workspace(
    *,
    tenant_id: str,
    agent_id: str,
    can_run: bool,
    csrf_token: str,
    accounts: list[models.PlatformAccount],
    prompt_pointer: models.ReplyBusinessPrompt | None,
    published_knowledge_count: int,
    trial_result: ReplyBusinessPromptTrialResult | None = None,
    error_message_key: str = "",
) -> tuple[str, str]:
    active_account_count = sum(account.status == "active" for account in accounts)
    automation_modes = {account.automation_default for account in accounts}
    agent_mode = (
        next(iter(automation_modes))
        if len(automation_modes) == 1
        else ("mixed" if automation_modes else "unconfigured")
    )
    return (
        render_template(
            "tenant/agent_test.html",
            **_agent_lifecycle_context(tenant_id, agent_id, current_stage="test"),
            playground_title=translate("agent.test.title"),
            playground_description=translate("agent.test.description"),
            sandbox_label=translate("agent.test.sandbox"),
            can_run=can_run,
            test_action=f"{_agent_root(tenant_id, agent_id)}/test",
            csrf_token=csrf_token,
            message_label=translate("admin.prompt.test_message"),
            message_placeholder=translate("agent.test.message_placeholder"),
            input_text="",
            isolation_notice=translate("agent.test.isolation_notice"),
            run_label=translate("agent.test.run"),
            read_only_notice=translate("agent.test.read_only"),
            error_message=(translate(error_message_key) if error_message_key else ""),
            result=trial_result,
            result_eyebrow=translate("agent.test.result_eyebrow"),
            result_title=translate("agent.test.result_title"),
            completed_label=translate("agent.test.completed"),
            action_label=translate("common.action"),
            intent_label=translate("admin.overview.intent"),
            risk_label=translate("admin.prompt.risk"),
            confidence_label=translate("admin.conversation.confidence"),
            duration_label=translate("agent.instructions.trial.duration"),
            reason_codes_label=translate("admin.prompt.reason_codes"),
            reply_label=translate("agent.test.reply"),
            no_reply_label=translate("admin.prompt.trial_no_reply"),
            context_title=translate("agent.test.context_title"),
            context_description=translate("agent.test.context_description"),
            prompt_label=translate("agent.test.prompt_version"),
            prompt_version=(
                f"v{prompt_pointer.revision}"
                if prompt_pointer
                else translate("agent.overview.code_default")
            ),
            knowledge_label=translate("agent.card.knowledge"),
            published_knowledge_count=published_knowledge_count,
            channels_label=translate("agent.card.channels"),
            active_account_count=active_account_count,
            account_count=len(accounts),
            mode_label=translate("agent.card.mode"),
            mode=_plain_status_label(agent_mode),
            guardrails_title=translate("agent.test.guardrails_title"),
            guardrails=(
                translate("agent.test.guardrail.pii"),
                translate("agent.test.guardrail.isolation"),
                translate("agent.test.guardrail.rate_limit"),
                translate("agent.test.guardrail.active_version"),
            ),
        ),
        agent_mode,
    )


async def _agent_test_page_response(
    request: Request,
    principal: Principal,
    *,
    tenant_id: str,
    agent_id: str,
    trial_result: ReplyBusinessPromptTrialResult | None = None,
    error_message_key: str = "",
    status_code: int = status.HTTP_200_OK,
) -> Response:
    async with get_session_factory()() as session:
        if agent_id not in await _load_agent_ids(session, principal, tenant_id):
            raise HTTPException(status_code=404, detail="agent_not_found")
        agent_display_name = await _load_agent_display_name(session, tenant_id, agent_id)
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
        published_knowledge_count = int(
            await session.scalar(
                select(func.count()).where(
                    models.KnowledgeDocument.tenant_id == tenant_id,
                    models.KnowledgeDocument.brand_id == agent_id,
                    models.KnowledgeDocument.status == "published",
                )
            )
            or 0
        )

    test_body, agent_mode = _render_agent_test_workspace(
        tenant_id=tenant_id,
        agent_id=agent_id,
        can_run=principal.is_admin,
        csrf_token=_csrf(request),
        accounts=accounts,
        prompt_pointer=prompt_pointer,
        published_knowledge_count=published_knowledge_count,
        trial_result=trial_result,
        error_message_key=error_message_key,
    )
    response = _render_page(
        principal=principal,
        tenant_id=tenant_id,
        title=agent_display_name,
        description=translate(
            "agent.detail_description",
            agent_id=agent_id,
            mode=_plain_status_label(agent_mode),
            count=len(accounts),
        ),
        body=f"{_agent_section_tabs(tenant_id, agent_id, 'test')}{test_body}",
        active_navigation="agents",
        inbox_count=inbox_summary.total,
        breadcrumbs=(
            (translate("nav.agents"), f"{_tenant_root(tenant_id)}/agents"),
            (agent_display_name, None),
        ),
    )
    response.status_code = status_code
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get(
    "/app/t/{tenant_id}/agents/{agent_id}/test",
    response_class=HTMLResponse,
)
async def agent_test_page(
    request: Request,
    tenant_id: str,
    agent_id: str,
) -> Response:
    principal = await _require_tenant_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    return await _agent_test_page_response(
        request,
        principal,
        tenant_id=tenant_id,
        agent_id=agent_id,
    )


@router.post("/app/t/{tenant_id}/agents/{agent_id}/test")
async def run_agent_test(
    request: Request,
    tenant_id: str,
    agent_id: str,
) -> Response:
    principal = await _require_tenant_admin_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    async with get_session_factory()() as authorization_session:
        if agent_id not in await _load_agent_ids(
            authorization_session,
            principal,
            tenant_id,
        ):
            raise HTTPException(status_code=404, detail="agent_not_found")
    form = await _form(request)
    _require_csrf(request, form)
    if set(form) != {"csrf_token", "text"}:
        raise HTTPException(status_code=422, detail="agent_test_fields_invalid")
    try:
        trial_result = await run_reply_business_prompt_trial(
            tenant_id=tenant_id,
            brand_id=agent_id,
            input_text=form.get("text", ""),
            actor=principal.actor,
        )
    except ReplyBusinessPromptTrialValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.code) from exc
    except ReplyBusinessPromptTrialRateLimited:
        return await _agent_test_page_response(
            request,
            principal,
            tenant_id=tenant_id,
            agent_id=agent_id,
            error_message_key="agent.instructions.trial.rate_limited",
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        )
    except ReplyBusinessPromptTrialUnavailable:
        return await _agent_test_page_response(
            request,
            principal,
            tenant_id=tenant_id,
            agent_id=agent_id,
            error_message_key="agent.instructions.trial.unavailable",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    except ReplyBusinessPromptScopeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ReplyBusinessPromptTrialExecutionError:
        return await _agent_test_page_response(
            request,
            principal,
            tenant_id=tenant_id,
            agent_id=agent_id,
            error_message_key="admin.prompt.trial_failed",
            status_code=status.HTTP_502_BAD_GATEWAY,
        )
    return await _agent_test_page_response(
        request,
        principal,
        tenant_id=tenant_id,
        agent_id=agent_id,
        trial_result=trial_result,
    )


@router.get(
    "/app/t/{tenant_id}/agents/{agent_id}",
    response_class=HTMLResponse,
)
async def agent_detail_redirect(
    request: Request,
    tenant_id: str,
    agent_id: str,
) -> Response:
    principal = await _require_tenant_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
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
        agent_display_name = await _load_agent_display_name(session, tenant_id, agent_id)
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
        control_plane = (
            await _load_agent_control_plane_views(session, tenant_id, [agent_id])
        ).get(agent_id)
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
    section_tabs = _agent_section_tabs(tenant_id, agent_id, section)
    section_body = _render_agent_section(
        section=section,
        tenant_id=tenant_id,
        agent_id=agent_id,
        accounts=accounts,
        prompt_pointer=prompt_pointer,
        prompt_version=prompt_version,
        knowledge_counts=knowledge_counts,
        recent_audits=recent_audits,
        control_plane=control_plane,
        is_admin=principal.is_admin,
    )
    modes = {account.automation_default for account in accounts}
    agent_mode = next(iter(modes)) if len(modes) == 1 else ("mixed" if modes else "unconfigured")
    return _render_page(
        principal=principal,
        tenant_id=tenant_id,
        title=agent_display_name,
        description=translate(
            "agent.detail_description",
            agent_id=agent_id,
            mode=_plain_status_label(agent_mode),
            count=len(accounts),
        ),
        body=f"{section_tabs}{section_body}",
        active_navigation="agents",
        inbox_count=inbox_summary.total,
        primary_action_html=(
            primary_action(
                f"{agent_base}/test",
                translate("agent.run_experiment"),
            )
            if principal.is_admin
            else ""
        ),
        breadcrumbs=(
            (translate("nav.agents"), f"{_tenant_root(tenant_id)}/agents"),
            (agent_display_name, None),
        ),
    )


def _plain_status_label(status: str) -> str:
    labels = {
        "BOT_ACTIVE": translate("agent.status.auto_reply"),
        "BOT_DRAFT_ONLY": translate("agent.status.draft_only"),
        "mixed": translate("agent.status.mixed"),
        "unconfigured": translate("agent.status.unconfigured"),
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
    control_plane: AgentControlPlaneView | None,
    is_admin: bool,
) -> str:
    if section == "overview":
        return _render_agent_overview(
            tenant_id=tenant_id,
            agent_id=agent_id,
            accounts=accounts,
            prompt_pointer=prompt_pointer,
            control_plane=control_plane,
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
            agent_id=agent_id,
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
    control_plane: AgentControlPlaneView | None = None,
    knowledge_counts: dict[str, int],
    is_admin: bool,
) -> str:
    active_accounts = [account for account in accounts if account.status == "active"]
    deployed_revision = (
        control_plane.deployed_version_revision if control_plane is not None else None
    )
    latest_revision = control_plane.version_revision if control_plane is not None else None
    release_is_current = (
        deployed_revision is not None and deployed_revision == latest_revision
    )
    if not accounts:
        next_title = translate("agent.connect_first_channel")
        next_description = translate("agent.overview.framework_description")
        next_href = (
            "/admin/integrations/accounts" if is_admin else f"{_tenant_root(tenant_id)}/channels"
        )
        action_label = (
            translate("agent.overview.connect_channel")
            if is_admin
            else translate("agent.overview.authorize_account")
        )
    elif len(active_accounts) != len(accounts):
        next_title = translate("agent.check_unavailable_channels")
        next_description = translate(
            "agent.overview.unavailable_description",
            count=len(accounts) - len(active_accounts),
        )
        next_href = (
            "/admin/integrations/accounts" if is_admin else f"{_tenant_root(tenant_id)}/channels"
        )
        action_label = translate("agent.overview.view_accounts")
    elif not prompt_pointer:
        next_title = translate("agent.overview.add_instructions")
        next_description = translate("agent.overview.instructions_description")
        next_href = (
            f"{_agent_root(tenant_id, agent_id)}/instructions"
            if is_admin
            else f"{_agent_root(tenant_id, agent_id)}/instructions"
        )
        action_label = (
            translate("agent.overview.edit_instructions")
            if is_admin
            else translate("agent.overview.view_instructions")
        )
    elif not release_is_current:
        next_title = translate("agent.overview.deploy_agent")
        next_description = translate("agent.overview.deploy_agent_description")
        next_href = f"{_agent_root(tenant_id, agent_id)}/instructions"
        action_label = translate("agent.overview.review_release")
    elif not knowledge_counts.get("published", 0):
        next_title = translate("agent.overview.publish_knowledge")
        next_description = (
            translate("agent.overview.knowledge_admin_description")
            if is_admin
            else translate("agent.overview.knowledge_user_description")
        )
        next_href = (
            f"{_tenant_root(tenant_id)}/knowledge"
            if is_admin
            else f"{_tenant_root(tenant_id)}/knowledge-query"
        )
        action_label = (
            translate("agent.overview.open_knowledge")
            if is_admin
            else translate("agent.overview.query_knowledge")
        )
    else:
        next_title = translate("agent.check_recent_activity")
        next_description = translate("agent.overview.activity_description")
        next_href = f"{_agent_root(tenant_id, agent_id)}/activity"
        action_label = translate("agent.overview.view_activity")
    readiness = "".join(
        (
            _progress_row(
                bool(prompt_pointer),
                translate("agent.overview.instructions_ready"),
            ),
            _progress_row(
                release_is_current,
                translate("agent.overview.release_ready"),
                warning=deployed_revision is not None,
            ),
            _progress_row(
                bool(knowledge_counts.get("published")),
                translate("agent.overview.knowledge_ready"),
            ),
            _progress_row(
                bool(accounts) and len(active_accounts) == len(accounts),
                translate(
                    "agent.overview.channel_count",
                    active=len(active_accounts),
                    total=len(accounts),
                ),
                warning=bool(accounts),
            ),
            _progress_row(True, translate("agent.overview.safety_ready")),
        )
    )
    return f"""<div class="saas-grid two" style="margin-top:0">
<section class="saas-next-action"><div><div class="saas-eyebrow">{escape(translate("next.eyebrow"))}</div>
<h2>{escape(next_title)}</h2><p>{escape(next_description)}</p></div>
{primary_action(next_href, action_label)}</section>
<section class="saas-card"><div class="saas-card-header"><div><h2>{escape(translate("agent.overview.runtime_summary"))}</h2>
<p>{escape(translate("agent.overview.runtime_summary_description"))}</p></div></div><div class="saas-card-body">
{definition_list(((translate("agent.overview.channel_accounts"), len(accounts)), (translate("home.metric.published_knowledge"), knowledge_counts.get("published", 0)), (translate("admin.prompt.latest_draft"), f"Agent v{latest_revision}" if latest_revision is not None else "—"), (translate("admin.prompt.production_release"), f"Agent v{deployed_revision}" if deployed_revision is not None else translate("admin.prompt.release_none"))))}
</div></section></div>
<div class="saas-section-title"><div><h2>{escape(translate("agent.overview.readiness"))}</h2><p>{escape(translate("agent.overview.readiness_description"))}</p></div></div>
<section class="saas-card"><div class="saas-card-body"><ul class="saas-progress-list">{readiness}</ul></div></section>"""


def _render_agent_instructions(
    *,
    tenant_id: str,
    agent_id: str,
    prompt_pointer: models.ReplyBusinessPrompt | None,
    prompt_version: models.ReplyBusinessPromptVersion | None,
    is_admin: bool,
) -> str:
    if not is_admin:
        return _render_user_agent_behavior_summary(
            accounts=[],
            published_knowledge_count=0,
        )
    content = (
        prompt_version.content
        if prompt_version
        else translate("agent.instructions.current_default")
    )
    metadata = definition_list(
        (
            ("Tenant", tenant_id),
            ("Brand / Agent", agent_id),
            (
                translate("agent.instructions.active_version"),
                f"v{prompt_pointer.revision}"
                if prompt_pointer
                else translate("agent.overview.code_default"),
            ),
            (
                translate("agent.instructions.content_hash"),
                prompt_pointer.content_hash[:12] if prompt_pointer else "—",
            ),
            (
                translate("agent.instructions.last_updated"),
                format_datetime(prompt_pointer.updated_at) if prompt_pointer else "—",
            ),
            (
                translate("agent.instructions.updated_by"),
                prompt_pointer.updated_by if prompt_pointer else "system",
            ),
        )
    )
    edit_action = (
        secondary_action(
            f"{_agent_root(tenant_id, agent_id)}/instructions",
            translate("agent.instructions.open_editor"),
            small=True,
        )
        if is_admin
        else ""
    )
    return f"""<div class="saas-grid" style="grid-template-columns:220px minmax(0,1fr) 280px;margin-top:0">
<aside class="saas-card"><div class="saas-card-header"><div><h2>{escape(translate("agent.instructions.version"))}</h2>
<p>{escape(translate("agent.instructions.version_description"))}</p></div></div><div class="saas-card-body">
{status_badge("active" if prompt_pointer else "unconfigured", label=translate("agent.instructions.active_badge", revision=prompt_pointer.revision) if prompt_pointer else translate("agent.overview.code_default"))}
</div></aside>
<section class="saas-card"><div class="saas-card-header"><div><h2>{escape(translate("agent.instructions.business_title"))}</h2>
<p>{escape(translate("agent.instructions.business_description"))}</p></div>
{edit_action}</div>
<div class="saas-card-body"><div class="saas-alert">{escape(content)}</div></div></section>
<aside class="saas-card"><div class="saas-card-header"><div><h2>{escape(translate("agent.instructions.security_metadata"))}</h2></div></div>
<div class="saas-card-body">{metadata}<div class="saas-alert warning" style="margin-top:16px">
{escape(translate("agent.instructions.safety_notice"))}</div></div></aside>
</div>"""


def _render_agent_model() -> str:
    settings = get_settings()
    provider_label = settings.llm_provider
    model_label = settings.openai_model if settings.llm_provider == "openai" else "stub"
    grounding_model = settings.openai_grounding_model or model_label
    return f"""<div class="saas-grid two" style="margin-top:0">
<section class="saas-card"><div class="saas-card-header"><div><h2>{escape(translate("agent.model.primary_title"))}</h2>
<p>{escape(translate("agent.model.primary_description"))}</p></div>{status_badge("active")}</div>
<div class="saas-card-body">{definition_list((("Provider", provider_label), ("Model", model_label), (translate("agent.model.usage"), translate("agent.model.primary_usage")), (translate("agent.model.config_source"), "Railway / Settings")))}</div></section>
<section class="saas-card"><div class="saas-card-header"><div><h2>{escape(translate("agent.model.grounding_title"))}</h2>
<p>{escape(translate("agent.model.grounding_description"))}</p></div>{status_badge("active")}</div>
<div class="saas-card-body">{definition_list((("Model", grounding_model), (translate("agent.model.usage"), translate("agent.model.grounding_usage")), ("Fallback", model_label), (translate("agent.model.key_status"), translate("agent.model.configured") if settings.openai_api_key.get_secret_value() else translate("agent.model.not_configured"))))}</div></section>
</div>
<section class="saas-alert warning" style="margin-top:18px">{escape(translate("agent.model.notice"))}</section>"""


def _render_agent_channels(
    accounts: list[models.PlatformAccount],
    *,
    tenant_id: str,
    agent_id: str,
    is_admin: bool,
) -> str:
    account_href = (
        f"{_tenant_root(tenant_id)}/channels?{urlencode({'brand_id': agent_id})}"
        if is_admin
        else f"{_tenant_root(tenant_id)}/channels"
    )
    account_action_label = (
        translate("agent.channels.manage_accounts")
        if is_admin
        else translate("agent.channels.manage_mine")
    )
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
            translate("agent.channels.empty_title"),
            translate("agent.channels.empty_description"),
            action_html=primary_action(
                account_href,
                translate("agent.channels.connect_first")
                if is_admin
                else translate("agent.channels.authorize_mine"),
            ),
        )
    return (
        '<div class="saas-table-wrap"><table class="saas-table"><thead><tr>'
        f"<th>{escape(translate('common.account'))}</th>"
        f"<th>{escape(translate('agent.channels.connection_status'))}</th>"
        f"<th>{escape(translate('agent.channels.automation_mode'))}</th>"
        f"<th>{escape(translate('agent.channels.config_version'))}</th><th></th>"
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
    knowledge_action_label = (
        translate("agent.overview.open_knowledge")
        if is_admin
        else translate("agent.overview.query_knowledge")
    )
    body = f"""<div class="saas-grid three" style="margin-top:0">
{metric_card(published_count, translate("home.metric.published_knowledge"))}
{metric_card(draft_count, translate("agent.knowledge.drafts"))}
{metric_card(published_count + draft_count, translate("agent.knowledge.total"))}
</div>
<section class="saas-card" style="margin-top:18px"><div class="saas-card-header"><div>
<h2>{escape(translate("agent.knowledge.binding_title"))}</h2><p>{escape(translate("agent.knowledge.binding_description"))}</p></div>
{secondary_action(knowledge_href, knowledge_action_label, small=True)}</div>
<div class="saas-card-body"><p>{escape(translate("agent.knowledge.layer_description"))}</p></div></section>"""
    return body


def _render_agent_flow() -> str:
    stages = (
        ("1", translate("agent.flow.stage.rules"), translate("agent.flow.stage.rules_detail")),
        (
            "2",
            translate("agent.flow.stage.retrieval"),
            translate("agent.flow.stage.retrieval_detail"),
        ),
        (
            "3",
            translate("agent.flow.stage.decision"),
            translate("agent.flow.stage.decision_detail"),
        ),
        ("4", translate("agent.flow.stage.guard"), translate("agent.flow.stage.guard_detail")),
        ("5", translate("agent.flow.stage.outbox"), translate("agent.flow.stage.outbox_detail")),
    )
    rows = "".join(
        '<li class="saas-timeline-item"><span class="saas-timeline-marker success">'
        f'{escape(marker)}</span><div><div class="saas-timeline-title">{escape(title)}</div>'
        f'<div class="saas-timeline-detail">{escape(detail)}</div></div></li>'
        for marker, title, detail in stages
    )
    return f"""<section class="saas-card"><div class="saas-card-header"><div><h2>{escape(translate("agent.flow.title"))}</h2>
<p>{escape(translate("agent.flow.description"))}</p></div>{status_badge("active", label=translate("agent.flow.controlled"))}</div>
<div class="saas-card-body"><ol class="saas-timeline">{rows}</ol></div></section>
<section class="saas-alert warning" style="margin-top:18px">{escape(translate("agent.flow.notice"))}</section>"""


def _render_agent_activity(audits: list[models.AuditLog]) -> str:
    rows = "".join(
        f"<tr><td>{format_datetime(audit.created_at)}</td>"
        f"<td>{escape(audit.actor)}</td><td>{escape(audit.action)}</td>"
        f"<td>{escape(audit.subject_type)}</td></tr>"
        for audit in audits
    )
    if not rows:
        return empty_state(
            translate("agent.activity.empty_title"),
            translate("agent.activity.empty_description"),
        )
    return (
        '<div class="saas-table-wrap"><table class="saas-table"><thead><tr>'
        f"<th>{escape(translate('common.time'))}</th><th>Actor</th>"
        f"<th>{escape(translate('common.action'))}</th>"
        f"<th>{escape(translate('common.resource'))}</th>"
        f"</tr></thead><tbody>{rows}</tbody></table></div>"
    )


def _render_inbox_workspace(
    *,
    queue_tabs: str,
    item_list: str,
    thread: str,
    action_panel: str,
    item_count: int = 0,
) -> str:
    return render_template(
        "tenant/inbox.html",
        queue_label=translate("inbox.queue_label"),
        title=translate("inbox.title"),
        item_count_label=translate("inbox.item_count", count=item_count),
        search_label=translate("inbox.search_label"),
        search_placeholder=translate("inbox.search_placeholder"),
        queue_tabs_html=trusted_html(queue_tabs),
        item_list_html=trusted_html(item_list),
        search_empty_title=translate("inbox.search_empty_title"),
        search_empty_description=translate("inbox.search_empty_description"),
        workspace_label=translate("conversations.workspace_label"),
        thread_html=trusted_html(thread),
        current_action_label=translate("inbox.current_action"),
        action_panel_html=trusted_html(action_panel) if action_panel else "",
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
            None,
        )
        messages: list[models.Message] = []
        if selected_item and selected_item.queue != "delivery":
            newest_messages = list(
                (
                    await session.execute(
                        select(models.Message)
                        .join(
                            models.Conversation,
                            models.Message.conversation_id == models.Conversation.id,
                        )
                        .where(
                            models.Message.conversation_id == selected_item.conversation_id,
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
        suggested_item=items[0] if items else None,
    )
    action_panel = _render_inbox_action_panel(
        tenant_id,
        selected_item,
        csrf_token=_csrf(request),
    )
    body = _render_inbox_workspace(
        queue_tabs=queue_tabs,
        item_list=item_list,
        thread=thread,
        action_panel=action_panel,
        item_count=len(items),
    )
    return _render_page(
        principal=principal,
        tenant_id=tenant_id,
        title=translate("inbox.title"),
        description=(
            translate("inbox.admin_description")
            if principal.is_admin
            else translate("inbox.user_description")
        ),
        body=body,
        active_navigation="inbox",
        inbox_count=inbox_summary.total,
        workbench=True,
    )


def _required_draft_generation(form: dict[str, str]) -> int:
    raw_value = form.get("expected_generation", "").strip()
    try:
        expected_generation = int(raw_value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="draft_expected_generation_invalid") from exc
    if expected_generation < 0:
        raise HTTPException(status_code=422, detail="draft_expected_generation_invalid")
    return expected_generation


def _required_draft_review_action(form: dict[str, str]) -> str:
    expected_review_action = form.get("expected_review_action", "").strip()
    if not expected_review_action:
        raise HTTPException(status_code=422, detail="draft_expected_review_action_invalid")
    return expected_review_action


def _tenant_draft_review_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, DraftReviewNotFound):
        return HTTPException(status_code=404, detail=exc.code)
    if isinstance(exc, DraftReviewConflict):
        return HTTPException(status_code=409, detail=exc.code)
    if isinstance(exc, DraftReviewValidationError):
        return HTTPException(status_code=422, detail=exc.code)
    return HTTPException(status_code=500, detail="draft_review_failed")


@router.post("/app/t/{tenant_id}/decisions/{decision_id}/approve")
async def tenant_approve_draft(
    request: Request,
    tenant_id: str,
    decision_id: uuid.UUID,
) -> Response:
    principal = await _require_tenant_admin_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    try:
        await approve_draft_review(
            decision_id=decision_id,
            required_tenant_id=tenant_id,
            actor=principal.actor,
            final_reply_text=form.get("final_reply_text"),
            expected_generation=_required_draft_generation(form),
            expected_review_action=_required_draft_review_action(form),
        )
    except (DraftReviewNotFound, DraftReviewConflict, DraftReviewValidationError) as exc:
        raise _tenant_draft_review_http_error(exc) from exc
    return RedirectResponse(
        f"{_tenant_root(tenant_id)}/inbox?queue=drafts",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/app/t/{tenant_id}/decisions/{decision_id}/discard")
async def tenant_discard_draft(
    request: Request,
    tenant_id: str,
    decision_id: uuid.UUID,
) -> Response:
    principal = await _require_tenant_admin_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    try:
        await reject_draft_review(
            decision_id=decision_id,
            required_tenant_id=tenant_id,
            actor=principal.actor,
            review_reason=form.get("review_reason", ""),
            expected_generation=_required_draft_generation(form),
            expected_review_action=_required_draft_review_action(form),
        )
    except (DraftReviewNotFound, DraftReviewConflict, DraftReviewValidationError) as exc:
        raise _tenant_draft_review_http_error(exc) from exc
    return RedirectResponse(
        f"{_tenant_root(tenant_id)}/inbox?queue=drafts",
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _required_delivery_attempt_count(form: dict[str, str]) -> int:
    raw_value = form.get("expected_attempt_count", "").strip()
    try:
        expected_attempt_count = int(raw_value)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail="delivery_expected_attempt_count_invalid",
        ) from exc
    if expected_attempt_count < 0:
        raise HTTPException(
            status_code=422,
            detail="delivery_expected_attempt_count_invalid",
        )
    return expected_attempt_count


def _required_delivery_status(form: dict[str, str]) -> str:
    expected_status = form.get("expected_status", "").strip()
    if not expected_status:
        raise HTTPException(status_code=422, detail="delivery_expected_status_invalid")
    return expected_status


def _delivery_recovery_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, DeliveryRecoveryNotFound):
        return HTTPException(status_code=404, detail=exc.code)
    if isinstance(exc, DeliveryRecoveryConflict):
        return HTTPException(status_code=409, detail=exc.code)
    if isinstance(exc, DeliveryRecoveryValidationError):
        return HTTPException(status_code=422, detail=exc.code)
    return HTTPException(status_code=500, detail="delivery_recovery_failed")


@router.post("/app/t/{tenant_id}/delivery/{outbox_id}/retry")
async def tenant_retry_failed_delivery(
    request: Request,
    tenant_id: str,
    outbox_id: uuid.UUID,
) -> Response:
    principal = await _require_tenant_admin_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    try:
        await retry_failed_outbox(
            outbox_id=outbox_id,
            required_tenant_id=tenant_id,
            actor=principal.actor,
            expected_status=_required_delivery_status(form),
            expected_attempt_count=_required_delivery_attempt_count(form),
            review_reason=form.get("review_reason", ""),
            verification_source=form.get("verification_source", ""),
        )
    except (
        DeliveryRecoveryNotFound,
        DeliveryRecoveryConflict,
        DeliveryRecoveryValidationError,
    ) as exc:
        raise _delivery_recovery_http_error(exc) from exc
    return RedirectResponse(
        f"{_tenant_root(tenant_id)}/inbox?queue=delivery",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/app/t/{tenant_id}/delivery/{outbox_id}/resolve")
async def tenant_resolve_reviewed_delivery(
    request: Request,
    tenant_id: str,
    outbox_id: uuid.UUID,
) -> Response:
    principal = await _require_tenant_admin_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    try:
        await resolve_needs_review_outbox(
            outbox_id=outbox_id,
            required_tenant_id=tenant_id,
            actor=principal.actor,
            expected_status=_required_delivery_status(form),
            expected_attempt_count=_required_delivery_attempt_count(form),
            review_reason=form.get("review_reason", ""),
            verification_source=form.get("verification_source", ""),
            resolution=form.get("resolution", ""),
            provider_message_id=form.get("provider_message_id"),
        )
    except (
        DeliveryRecoveryNotFound,
        DeliveryRecoveryConflict,
        DeliveryRecoveryValidationError,
    ) as exc:
        raise _delivery_recovery_http_error(exc) from exc
    return RedirectResponse(
        f"{_tenant_root(tenant_id)}/inbox?queue=delivery",
        status_code=status.HTTP_303_SEE_OTHER,
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
                .join(
                    models.Contact,
                    and_(
                        models.Conversation.contact_id == models.Contact.id,
                        models.Contact.tenant_id == tenant_id,
                        models.Contact.platform_account_id
                        == models.Conversation.platform_account_id,
                    ),
                )
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
                title=contact.display_name or translate("common.anonymous_contact"),
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
                .join(
                    models.Contact,
                    and_(
                        models.Conversation.contact_id == models.Contact.id,
                        models.Contact.tenant_id == tenant_id,
                        models.Contact.platform_account_id
                        == models.Conversation.platform_account_id,
                    ),
                )
                .join(
                    models.PlatformAccount,
                    models.Conversation.platform_account_id == models.PlatformAccount.id,
                )
                .where(
                    models.ReplyDecision.tenant_id == tenant_id,
                    models.Conversation.tenant_id == tenant_id,
                    models.PlatformAccount.tenant_id == tenant_id,
                    _account_scope_condition(principal, tenant_id),
                    reviewable_draft_condition(),
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
                title=contact.display_name or translate("common.anonymous_contact"),
                platform=conversation.platform,
                channel_type=conversation.channel_type,
                account_name=account.name,
                status=decision.review_action or "PENDING",
                reason=", ".join(decision.reason_codes or []) or translate("status.needs_review"),
                created_at=decision.created_at,
                draft_text=(
                    (decision.original_reply_text or "").strip()
                    or (decision.reply_text or "").strip()
                ),
                decision_generation=decision.decision_generation,
                review_action=decision.review_action or "PENDING",
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
            .join(
                models.Contact,
                and_(
                    models.Conversation.contact_id == models.Contact.id,
                    models.Contact.tenant_id == tenant_id,
                    models.Contact.platform_account_id == models.Conversation.platform_account_id,
                ),
            )
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
            title=contact.display_name or translate("common.anonymous_contact"),
            platform=conversation.platform,
            channel_type=conversation.channel_type,
            account_name=account.name,
            status=outbox.status,
            reason=outbox.last_error_code or translate("status.needs_review"),
            created_at=outbox.created_at,
            expected_status=outbox.status,
            expected_attempt_count=outbox.attempt_count,
            delivery_error_code=outbox.last_error_code,
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
            ("human", translate("inbox.queue.human"), summary.human_count),
            ("drafts", translate("inbox.queue.drafts"), summary.draft_count),
            ("delivery", translate("inbox.queue.delivery"), summary.delivery_count),
        )
        if include_admin_queues
        else (("human", translate("inbox.queue.human"), summary.human_count),)
    )
    links = "".join(
        f'<a class="saas-queue-tab{" active" if key == active_queue else ""}" '
        f'href="{root}?queue={key}"'
        f"{" aria-current='page'" if key == active_queue else ''}>"
        f"{escape(label)} {count}</a>"
        for key, label, count in queue_data
    )
    return (
        f'<nav class="saas-queue-tabs" aria-label="{escape(translate("inbox.queue_label"))}">'
        f"{links}</nav>"
    )


def _render_inbox_item_list(
    tenant_id: str,
    queue: str,
    items: list[InboxItem],
    selected_item: InboxItem | None,
) -> str:
    if not items:
        return (
            f'<div class="saas-empty"><h2>{escape(translate("inbox.queue_empty_title"))}</h2>'
            f"<p>{escape(translate('inbox.queue_empty_description'))}</p></div>"
        )
    root = f"{_tenant_root(tenant_id)}/inbox?queue={queue}"
    rendered_items: list[str] = []
    for item in items:
        is_selected = selected_item is not None and item.item_id == selected_item.item_id
        filter_text = f"{item.title} {item.account_name} {item.platform}"
        rendered_items.append(
            f'<a class="saas-work-item{" active" if is_selected else ""}" '
            f'href="{root}&amp;item_id={item.item_id}" '
            f'data-filter-text="{escape(filter_text)}"'
            f"{" aria-current='true'" if is_selected else ''}>"
            f'<div class="saas-work-item-title"><span>{escape(item.title)}</span>'
            f"<span>{format_age(item.created_at)}</span></div>"
            f"<p>{escape(item.platform)} · {escape(item.channel_type)} · "
            f"{escape(item.account_name)}</p>"
            f'<div class="saas-work-item-meta"><span>{status_badge(item.status)}</span>'
            f"<span>{escape(item.reason)}</span></div></a>"
        )
    return "".join(rendered_items)


def _render_conversation_thread(
    tenant_id: str,
    messages: list[models.Message],
    selected_item: InboxItem | None,
    *,
    suggested_item: InboxItem | None = None,
) -> str:
    if selected_item is None:
        action_html = ""
        if suggested_item is not None:
            action_html = primary_action(
                f"{_tenant_root(tenant_id)}/inbox?queue={suggested_item.queue}"
                f"&item_id={suggested_item.item_id}",
                translate("inbox.open_oldest"),
            )
        return empty_state(
            translate("inbox.select_title"),
            translate("inbox.select_description"),
            action_html=action_html,
        )
    message_rows = "".join(
        '<article class="saas-message '
        f'{"outbound" if message.direction == "outbound" else "inbound"}">'
        f'<div class="saas-message-bubble">{escape(message.text or translate("common.non_text_message"))}</div>'
        f'<div class="saas-message-meta">{escape(message.sender_type)} · '
        f"{format_datetime(message.occurred_at or message.created_at)}</div></article>"
        for message in messages
    )
    if not message_rows:
        message_rows = (
            f'<div class="saas-empty"><p>{escape(translate("inbox.no_messages"))}</p></div>'
        )
    return f"""<div class="saas-thread">
<div class="saas-inbox-column-header"><strong>{escape(selected_item.title)}</strong>
<div class="saas-muted">{escape(selected_item.platform)} · {escape(selected_item.channel_type)}</div></div>
<div class="saas-thread-messages">{message_rows}</div>
<div class="saas-composer"><textarea disabled placeholder="{escape(translate("inbox.composer_placeholder"))}"></textarea>
<div style="display:flex;justify-content:space-between;align-items:center;margin-top:8px">
<span class="saas-muted">{escape(translate("inbox.reply_safety"))}</span>
{secondary_action(f"{_tenant_root(tenant_id)}/conversations/{selected_item.conversation_id}", translate("inbox.open_full"), small=True)}</div></div>
</div>"""


def _delivery_verification_source_options() -> str:
    options = (
        ("PROVIDER_DASHBOARD", translate("inbox.delivery.source.provider_dashboard")),
        ("PROVIDER_API", translate("inbox.delivery.source.provider_api")),
        (
            "CUSTOMER_CONFIRMATION",
            translate("inbox.delivery.source.customer_confirmation"),
        ),
        (
            "ADMIN_OPERATOR_ATTESTED",
            translate("inbox.delivery.source.admin_attested"),
        ),
        (
            "SUPERVISOR_OVERRIDE",
            translate("inbox.delivery.source.supervisor_override"),
        ),
    )
    return "".join(
        f'<option value="{source}">{escape(label)}</option>' for source, label in options
    )


def _delivery_fence_fields(selected_item: InboxItem) -> str:
    expected_status = selected_item.expected_status or selected_item.status
    expected_attempt_count = (
        selected_item.expected_attempt_count
        if selected_item.expected_attempt_count is not None
        else 0
    )
    return (
        f'<input type="hidden" name="expected_status" value="{escape(expected_status)}">'
        f'<input type="hidden" name="expected_attempt_count" '
        f'value="{expected_attempt_count}">'
    )


def _delivery_evidence_fields(*, field_suffix: str) -> str:
    return f"""<label class="saas-field" for="delivery-source-{escape(field_suffix)}"><span>{escape(translate("inbox.delivery.verification_source"))}</span>
<select id="delivery-source-{escape(field_suffix)}" name="verification_source" required>{_delivery_verification_source_options()}</select></label>
<label class="saas-field" for="delivery-reason-{escape(field_suffix)}"><span>{escape(translate("inbox.delivery.review_reason"))}</span>
<textarea id="delivery-reason-{escape(field_suffix)}" name="review_reason" required maxlength="500"></textarea></label>"""


def _delivery_resolution_form(
    *,
    tenant_id: str,
    selected_item: InboxItem,
    csrf_token: str,
    resolution: str,
    title_key: str,
    description_key: str,
    button_key: str,
    include_provider_message_id: bool = False,
) -> str:
    field_suffix = resolution.lower().replace("_", "-")
    provider_message_field = ""
    if include_provider_message_id:
        provider_message_field = f"""<label class="saas-field" for="provider-message-{field_suffix}"><span>{escape(translate("inbox.delivery.provider_message_id"))}</span>
<input id="provider-message-{field_suffix}" name="provider_message_id" required maxlength="255" pattern="[A-Za-z0-9][A-Za-z0-9._:/=+\\-]{{0,254}}"></label>"""
    return f"""<section class="saas-draft-review"><h4>{escape(translate(title_key))}</h4>
<p class="saas-muted">{escape(translate(description_key))}</p>
<form method="post" action="{_tenant_root(tenant_id)}/delivery/{selected_item.item_id}/resolve">
<input type="hidden" name="csrf_token" value="{escape(csrf_token)}">
{_delivery_fence_fields(selected_item)}
<input type="hidden" name="resolution" value="{resolution}">
{_delivery_evidence_fields(field_suffix=field_suffix)}
{provider_message_field}
<button class="saas-button" type="submit">{escape(translate(button_key))}</button></form></section>"""


def _render_delivery_action_panel(
    tenant_id: str,
    selected_item: InboxItem,
    *,
    csrf_token: str,
) -> str:
    attempt_count = selected_item.expected_attempt_count or 0
    error_code = selected_item.delivery_error_code or selected_item.reason
    delivery_details = definition_list(
        (
            (translate("common.account"), selected_item.account_name),
            (translate("common.platform"), selected_item.platform),
            (translate("inbox.waiting"), format_age(selected_item.created_at)),
            (translate("inbox.delivery.error_code"), error_code),
            (
                translate("inbox.delivery.attempt"),
                translate("inbox.delivery.attempt_value", attempt=attempt_count),
            ),
            ("Item ID", str(selected_item.item_id)),
        )
    )
    header = f"""<div class="saas-action-panel"><h3>{escape(translate("inbox.current_action"))}</h3>
{status_badge(selected_item.status)}
<p><strong>{escape(error_code)}</strong></p>
{delivery_details}"""
    if selected_item.status == "FAILED":
        retry_form = f"""<div class="saas-alert warning" style="margin:16px 0">{escape(translate("inbox.warning.retry"))}</div>
<form method="post" action="{_tenant_root(tenant_id)}/delivery/{selected_item.item_id}/retry">
<input type="hidden" name="csrf_token" value="{escape(csrf_token)}">
{_delivery_fence_fields(selected_item)}
{_delivery_evidence_fields(field_suffix="failed-retry")}
<button class="saas-button primary" type="submit">{escape(translate("inbox.delivery.retry_confirmed_failure"))}</button></form>"""
        return f"{header}{retry_form}</div>"
    review_forms = "".join(
        (
            _delivery_resolution_form(
                tenant_id=tenant_id,
                selected_item=selected_item,
                csrf_token=csrf_token,
                resolution="CONFIRMED_NOT_SENT_RETRY",
                title_key="inbox.delivery.not_sent_title",
                description_key="inbox.delivery.not_sent_description",
                button_key="inbox.delivery.not_sent_action",
            ),
            _delivery_resolution_form(
                tenant_id=tenant_id,
                selected_item=selected_item,
                csrf_token=csrf_token,
                resolution="CONFIRMED_SENT",
                title_key="inbox.delivery.sent_title",
                description_key="inbox.delivery.sent_description",
                button_key="inbox.delivery.sent_action",
                include_provider_message_id=True,
            ),
            _delivery_resolution_form(
                tenant_id=tenant_id,
                selected_item=selected_item,
                csrf_token=csrf_token,
                resolution="CANCEL",
                title_key="inbox.delivery.cancel_title",
                description_key="inbox.delivery.cancel_description",
                button_key="inbox.delivery.cancel_action",
            ),
        )
    )
    warning = f'<div class="saas-alert warning" style="margin:16px 0">{escape(translate("inbox.warning.verify"))}</div>'
    return f"{header}{warning}{review_forms}</div>"


def _render_inbox_action_panel(
    tenant_id: str,
    selected_item: InboxItem | None,
    *,
    csrf_token: str = "",
) -> str:
    if selected_item is None:
        return ""
    if selected_item.queue == "delivery":
        return _render_delivery_action_panel(
            tenant_id,
            selected_item,
            csrf_token=csrf_token,
        )
    if selected_item.queue == "human":
        action_label = translate("inbox.action.claim")
        warning = translate("inbox.warning.claim")
    elif selected_item.queue == "drafts":
        draft_text = selected_item.draft_text or ""
        expected_generation = (
            str(selected_item.decision_generation)
            if selected_item.decision_generation is not None
            else ""
        )
        expected_review_action = selected_item.review_action or selected_item.status
        return f"""<div class="saas-action-panel"><h3>{escape(translate("inbox.current_action"))}</h3>
{status_badge(selected_item.status)}
<p><strong>{escape(selected_item.reason)}</strong></p>
{definition_list(((translate("common.account"), selected_item.account_name), (translate("common.platform"), selected_item.platform), (translate("inbox.waiting"), format_age(selected_item.created_at)), ("Item ID", str(selected_item.item_id))))}
<div class="saas-alert warning" style="margin:16px 0">{escape(translate("inbox.warning.review"))}</div>
<section class="saas-draft-review"><h4>{escape(translate("inbox.original_draft"))}</h4>
<blockquote>{escape(draft_text)}</blockquote>
<form method="post" action="{_tenant_root(tenant_id)}/decisions/{selected_item.item_id}/approve">
<input type="hidden" name="csrf_token" value="{escape(csrf_token)}">
<input type="hidden" name="expected_generation" value="{escape(expected_generation)}">
<input type="hidden" name="expected_review_action" value="{escape(expected_review_action)}">
<label class="saas-field"><span>{escape(translate("inbox.final_reply"))}</span>
<textarea name="final_reply_text" required maxlength="10000">{escape(draft_text)}</textarea></label>
<button class="saas-button primary" type="submit">{escape(translate("inbox.approve_send"))}</button></form>
<form method="post" action="{_tenant_root(tenant_id)}/decisions/{selected_item.item_id}/discard">
<input type="hidden" name="csrf_token" value="{escape(csrf_token)}">
<input type="hidden" name="expected_generation" value="{escape(expected_generation)}">
<input type="hidden" name="expected_review_action" value="{escape(expected_review_action)}">
<label class="saas-field"><span>{escape(translate("inbox.rejection_reason"))}</span>
<input name="review_reason" required maxlength="500"></label>
<button class="saas-button" type="submit">{escape(translate("inbox.reject_draft"))}</button></form></section>
<p class="saas-muted">{escape(translate("inbox.return_after_completion"))}</p></div>"""
    elif selected_item.status == "NEEDS_REVIEW":
        action_label = translate("inbox.action.verify_delivery")
        warning = translate("inbox.warning.verify")
    else:
        action_label = translate("inbox.action.retry_delivery")
        warning = translate("inbox.warning.retry")
    return f"""<div class="saas-action-panel"><h3>{escape(translate("inbox.current_action"))}</h3>
{status_badge(selected_item.status)}
<p><strong>{escape(selected_item.reason)}</strong></p>
{definition_list(((translate("common.account"), selected_item.account_name), (translate("common.platform"), selected_item.platform), (translate("inbox.waiting"), format_age(selected_item.created_at)), ("Item ID", str(selected_item.item_id))))}
<div class="saas-alert warning" style="margin:16px 0">{escape(warning)}</div>
{primary_action(f"{_tenant_root(tenant_id)}/conversations/{selected_item.conversation_id}", action_label)}
<p class="saas-muted">{escape(translate("inbox.return_after_completion"))}</p></div>"""


def _render_conversations_workspace(
    *,
    conversation_items: str,
    item_count: int,
    workspace_empty: str,
) -> str:
    list_content = conversation_items or (
        f'<div class="saas-empty"><h2>{escape(translate("conversations.empty_title"))}</h2>'
        f"<p>{escape(translate('conversations.empty_description'))}</p></div>"
    )
    return f"""<div class="saas-inbox-layout saas-conversations-layout" data-conversation-workspace>
  <section class="saas-inbox-column saas-list-pane" aria-label="{escape(translate("conversations.list_label"))}" data-conversation-list data-list-filter>
    <header class="saas-inbox-column-header saas-list-pane-header">
      <div class="saas-list-pane-title"><h1>{escape(translate("conversations.title"))}</h1>
      <span class="saas-muted">{escape(translate("conversations.item_count", count=item_count))}</span></div>
      <label for="conversation-list-search">{escape(translate("conversations.search_label"))}</label>
      <input id="conversation-list-search" type="search" autocomplete="off" data-list-search
             placeholder="{escape(translate("conversations.search_placeholder"))}">
    </header>
    <div data-list-items>{list_content}</div>
    <div class="saas-empty" data-search-empty hidden>
      <h2>{escape(translate("conversations.search_empty_title"))}</h2>
      <p>{escape(translate("conversations.search_empty_description"))}</p>
    </div>
  </section>
  <section class="saas-inbox-column saas-workspace-pane" aria-label="{escape(translate("conversations.workspace_label"))}" data-conversation-main>
    {workspace_empty}
  </section>
</div>"""


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
                .join(
                    models.Contact,
                    and_(
                        models.Conversation.contact_id == models.Contact.id,
                        models.Contact.tenant_id == tenant_id,
                        models.Contact.platform_account_id
                        == models.Conversation.platform_account_id,
                    ),
                )
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
    rendered_conversation_items: list[str] = []
    for conversation, contact, account, latest_at in rows:
        contact_name = contact.display_name or translate("common.anonymous_contact")
        filter_text = f"{contact_name} {account.name} {conversation.platform}"
        rendered_conversation_items.append(
            f'<a class="saas-work-item" '
            f'href="{_tenant_root(tenant_id)}/conversations/{conversation.id}" '
            f'data-filter-text="{escape(filter_text)}">'
            f'<div class="saas-work-item-title"><span>{escape(contact_name)}</span>'
            f"<span>{format_datetime(latest_at)}</span></div>"
            f"<p>{escape(account.name)} · {escape(conversation.platform)} · "
            f"{escape(conversation.channel_type)}</p>"
            f'<div class="saas-work-item-meta"><span>{escape(translate("conversations.latest_message"))}</span>'
            f"<span>{escape(translate('conversations.open'))}</span></div></a>"
        )
    conversation_items = "".join(rendered_conversation_items)
    if conversation_items:
        first_conversation = rows[0][0]
        workspace_empty = empty_state(
            translate("conversations.select_title"),
            translate("conversations.select_description"),
            action_html=primary_action(
                f"{_tenant_root(tenant_id)}/conversations/{first_conversation.id}",
                translate("conversations.open_latest"),
            ),
        )
    else:
        workspace_empty = empty_state(
            translate("conversations.empty_title"),
            translate("conversations.empty_description"),
        )
    body = _render_conversations_workspace(
        conversation_items=conversation_items,
        item_count=len(rows),
        workspace_empty=workspace_empty,
    )
    return _render_page(
        principal=principal,
        tenant_id=tenant_id,
        title=translate("conversations.title"),
        description=translate("conversations.description"),
        body=body,
        active_navigation="conversations",
        inbox_count=inbox_summary.total,
        workbench=True,
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
                select(models.Conversation, models.PlatformAccount)
                .join(
                    models.PlatformAccount,
                    and_(
                        models.Conversation.platform_account_id == models.PlatformAccount.id,
                        models.PlatformAccount.tenant_id == tenant_id,
                    ),
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
        conversation, account = row
        contact = await session.scalar(
            select(models.Contact).where(
                models.Contact.id == conversation.contact_id,
                models.Contact.tenant_id == tenant_id,
                models.Contact.platform_account_id == conversation.platform_account_id,
            )
        )
        if contact is None:
            logger.warning(
                "conversation detail scope mismatch conversation_id=%s relation=contact "
                "related_id=%s",
                conversation_id,
                conversation.contact_id,
            )
            raise HTTPException(status_code=404, detail="conversation_not_found")
        invalid_source_outbox_id = await session.scalar(
            select(models.Message.source_outbox_id)
            .outerjoin(
                models.OutboxMessage,
                models.Message.source_outbox_id == models.OutboxMessage.id,
            )
            .where(
                models.Message.conversation_id == conversation_id,
                models.Message.source_outbox_id.is_not(None),
                or_(
                    models.OutboxMessage.id.is_(None),
                    models.OutboxMessage.tenant_id != tenant_id,
                    models.OutboxMessage.conversation_id != conversation_id,
                    models.OutboxMessage.platform_account_id != conversation.platform_account_id,
                ),
            )
            .limit(1)
        )
        if invalid_source_outbox_id is not None:
            logger.warning(
                "conversation detail scope mismatch conversation_id=%s "
                "relation=message_source_outbox related_id=%s",
                conversation_id,
                invalid_source_outbox_id,
            )
            raise HTTPException(status_code=404, detail="conversation_not_found")
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
        (message for message in newest_messages if message.direction == "inbound"),
        None,
    )
    message_rows = "".join(
        '<article class="saas-card saas-card-body">'
        f"<div class='saas-muted'>{escape(message.sender_type)} · "
        f"{format_datetime(message.created_at, include_year=True)}</div>"
        f"<p>{escape(message.text or translate('common.non_text_message'))}</p></article>"
        for message in reversed(newest_messages)
    )
    work_actions = ""
    if work_item is not None and work_item.status == "WAITING":
        work_actions = f"""<form class="saas-form" method="post" action="{_tenant_root(tenant_id)}/work-items/{work_item.id}/claim">
<input type="hidden" name="csrf_token" value="{csrf}"><input type="hidden" name="expected_version" value="{work_item.version}">
<button class="saas-button primary" type="submit">{escape(translate("conversation.claim"))}</button></form>"""
    elif work_item is not None and work_item.assigned_actor == principal.actor:
        work_actions = f"""<form class="saas-form" method="post" action="{_tenant_root(tenant_id)}/work-items/{work_item.id}/resolve">
<input type="hidden" name="csrf_token" value="{csrf}"><input type="hidden" name="expected_version" value="{work_item.version}">
<button class="saas-button" type="submit">{escape(translate("conversation.resolve"))}</button></form>"""
    reply_form = ""
    if reply_target is not None:
        work_fields = ""
        if work_item is not None:
            work_fields = (
                f'<input type="hidden" name="work_item_id" value="{work_item.id}">'
                f'<input type="hidden" name="expected_version" value="{work_item.version}">'
            )
        reply_form = f"""<section class="saas-card"><div class="saas-card-header"><div><h2>{escape(translate("conversation.reply_title"))}</h2>
<p>{escape(translate("conversation.reply_description"))}</p></div></div><div class="saas-card-body">
<form class="saas-form" method="post" action="{_tenant_root(tenant_id)}/conversations/{conversation_id}/reply">
<input type="hidden" name="csrf_token" value="{csrf}"><input type="hidden" name="reply_to_message_id" value="{reply_target.id}">
<input type="hidden" name="idempotency_key" value="{uuid.uuid4()}">{work_fields}
<label for="manual-reply">{escape(translate("conversation.reply_label"))}</label><textarea id="manual-reply" name="text" rows="5" maxlength="10000" required></textarea>
<button class="saas-button primary" type="submit">{escape(translate("conversation.send_reply"))}</button></form></div></section>"""
    body = (
        '<section class="saas-card"><div class="saas-card-body">'
        f"{definition_list(((translate('common.contact'), contact.display_name or translate('common.anonymous_contact')), (translate('common.account'), account.name), (translate('common.platform'), conversation.platform), (translate('common.channel'), conversation.channel_type), ('Conversation ID', conversation.id)))}"
        f"{work_actions}</div></section>"
        f'<div class="saas-stack">{message_rows}</div>{reply_form}'
    )
    response = _render_page(
        principal=principal,
        tenant_id=tenant_id,
        title=contact.display_name or translate("conversation.detail_title"),
        description=translate("conversation.detail_description"),
        body=body,
        active_navigation="conversations",
        inbox_count=inbox_summary.total,
        breadcrumbs=(
            (
                translate("conversations.title"),
                f"{_tenant_root(tenant_id)}/conversations",
            ),
            (translate("common.details"), None),
        ),
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
        work_item_id = uuid.UUID(form["work_item_id"]) if form.get("work_item_id") else None
        expected_version = int(form["expected_version"]) if form.get("expected_version") else None
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
    if request.method == "GET" and request.query_params.get("q"):
        return RedirectResponse(
            f"{_tenant_root(tenant_id)}/knowledge-query",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    query = ""
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
            allowed_brand_ids: tuple[str, ...] | None = None
            if not principal.is_admin:
                owned_brand_ids = set(
                    await session.scalars(
                        select(models.PlatformAccount.brand_id)
                        .where(
                            models.PlatformAccount.tenant_id == tenant_id,
                            models.PlatformAccount.owner_user_id == principal.user_id,
                        )
                        .distinct()
                    )
                )
                allowed_brand_ids = tuple(sorted({"default", *owned_brand_ids}))
            documents = await execute_search_published_knowledge(
                session,
                SearchPublishedKnowledgeQuery(
                    required_tenant_id=tenant_id,
                    actor=principal.actor,
                    search_text=query,
                    allowed_brand_ids=allowed_brand_ids,
                ),
            )
            documents = [
                document
                for document in documents
                if not _knowledge_document_is_sensitive(document)
                and not any(
                    has_contact_like(str(value or ""))
                    for value in (
                        document.brand_id,
                        document.platform,
                        document.category,
                        document.source_file,
                    )
                )
            ]
    csrf = _csrf(request)
    results = "".join(
        '<article class="saas-card"><div class="saas-card-header"><div>'
        f"<h2>{escape(document.question)}</h2>"
        f"<p>{escape(_knowledge_visible_metadata(document, document.brand_id))} · {escape(_knowledge_visible_metadata(document, document.platform))}</p></div>"
        f"{status_badge(document.status)}</div>"
        f'<div class="saas-card-body"><p>{escape(document.reply)}</p></div></article>'
        for document in documents
    )
    if query and not results:
        results = empty_state(
            translate("knowledge_query.empty_title"),
            translate("knowledge_query.empty_description"),
        )
    body = f"""<section class="saas-card"><div class="saas-card-body">
<form class="saas-form" method="post" role="search" aria-label="{escape(translate("knowledge_query.title"))}"><input type="hidden" name="csrf_token" value="{csrf}">
<label for="knowledge-query">{escape(translate("knowledge_query.label"))}</label>
<div class="saas-form-row"><input id="knowledge-query" name="q" value="{escape(query)}" maxlength="500" required>
<button class="saas-button primary" type="submit">{escape(translate("knowledge_query.submit"))}</button></div>
<p class="saas-muted">{escape(translate("knowledge_query.notice"))}</p></form></div></section>
<div class="saas-stack">{results}</div>"""
    request_location_token = set_request_location(request.url.path, ())
    try:
        response = _render_page(
            principal=principal,
            tenant_id=tenant_id,
            title=translate("knowledge_query.title"),
            description=translate("knowledge_query.description"),
            body=body,
            active_navigation="knowledge-query",
            inbox_count=inbox_summary.total,
        )
    finally:
        reset_request_location(request_location_token)
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
        f"<th>{escape(translate('common.time'))}</th>"
        f"<th>{escape(translate('common.action'))}</th>"
        f"<th>{escape(translate('common.resource'))}</th>"
        f"<th>ID</th></tr></thead><tbody>{rows}</tbody></table></div>"
        if rows
        else empty_state(
            translate("activity.empty_title"),
            translate("activity.empty_description"),
        )
    )
    return _render_page(
        principal=principal,
        tenant_id=tenant_id,
        title=translate("activity.title"),
        description=translate("activity.description"),
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
    known_platform = (
        platform
        if platform
        in {
            "email",
            "facebook",
            "feishu",
            "instagram",
            "telegram",
            "whatsapp",
            "x",
        }
        else "email"
    )
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
    return f'<span class="saas-account-avatar fallback" aria-hidden="true">{escape(initial)}</span>'


def _channel_account_health(account: models.PlatformAccount) -> tuple[str, str]:
    if account.status != "active":
        return "danger", translate("channels.health.disabled")
    config = dict(account.config or {})
    health_values = {
        str(config.get("meta_health_status") or ""),
        str(config.get("email_health_status") or ""),
        str(config.get("feishu_health_status") or ""),
    }
    if "ERROR" in health_values:
        return "warning", translate("channels.health.reauthorize")
    if "PROVISIONING" in health_values:
        return "info", translate("channels.health.provisioning")
    return "success", translate("channels.health.connected")


def _channel_job_error_message(job: models.ProvisioningJob) -> str:
    if job.status == "FAILED":
        return translate("channels.job.failed")
    error_code = str(job.last_error_code or "")
    if error_code == "ACCOUNT_OWNER_CONFLICT":
        return translate("channels.job.owner_conflict")
    if error_code.startswith(("EMAIL_", "imap_", "smtp_")):
        return translate("channels.job.email_error")
    if error_code.startswith("X_"):
        return translate("channels.job.x_error")
    if error_code.startswith(("META_", "PLATFORM_HTTP_")):
        return translate("channels.job.meta_error")
    return translate("channels.job.default_error")


def _channel_job_action(
    job: models.ProvisioningJob,
    *,
    tenant_id: str,
    csrf: str,
) -> str:
    if job.status not in {"NEEDS_ACTION", "FAILED"}:
        return ""
    if job.status == "FAILED" and provisioning_job_is_in_flight(job):
        return f'<span class="saas-muted">{escape(translate("channels.job.waiting_retry"))}</span>'
    if requires_secret_resubmission(job) and job.platform in {"telegram", "email"}:
        return (
            '<button class="saas-button small" type="button" '
            f'data-open-channel-dialog="{escape(job.platform)}-dialog">'
            f"{escape(translate('channels.job.refill_credentials'))}</button>"
        )
    if requires_secret_resubmission(job):
        return (
            '<a class="saas-button small" href="#add-channels">'
            f"{escape(translate('channels.job.reauthorize'))}</a>"
        )
    return f"""<form method="post" action="{_tenant_root(tenant_id)}/channels/jobs/{job.id}/retry">
<input type="hidden" name="csrf_token" value="{escape(csrf)}">
<button class="saas-button small" type="submit">{escape(translate("button.retry"))}</button></form>"""


def _render_connected_channel(
    account: models.PlatformAccount,
    *,
    tenant_id: str,
    owner_name: str | None,
    kill_switch_enabled: bool,
) -> str:
    health_tone, health_label = _channel_account_health(account)
    username = (
        f"@{account.provider_username.lstrip('@')}"
        if account.provider_username and account.platform not in {"email", "telegram"}
        else account.provider_username
    )
    profile_line = username or account.external_account_id or translate("channels.platform_account")
    connected_at = account.profile_updated_at or account.created_at
    owner_label = owner_name or translate("channels.organization_account")
    kill_switch_label = translate(
        "channels.kill_switch.enabled" if kill_switch_enabled else "channels.kill_switch.disabled"
    )
    return f"""<article class="saas-connected-channel">
<div class="saas-connected-identity">{_channel_avatar(account)}<div>
<h3>{escape(account.name)}</h3><p>{escape(profile_line)}</p></div></div>
<div class="saas-connected-meta"><span class="saas-platform-label">
<img src="{_channel_icon_path(account.platform)}" alt="">{escape(account.platform.title())}</span>
<span class="saas-status {health_tone}">{escape(health_label)}</span>
<span class="saas-muted">{escape(translate("channels.owner", owner=owner_label))}</span>
<span class="saas-muted">{escape(kill_switch_label)}</span>
<span class="saas-muted">{escape(translate("channels.recent_connection", time=format_datetime(connected_at, include_year=True)))}</span></div>
<div class="saas-provider-actions"><a class="saas-button small" href="{_tenant_root(tenant_id)}/channels/accounts/{account.id}">{escape(translate("channels.manage_account"))}</a></div>
</article>"""


def _channel_oauth_form(
    *,
    action: str,
    csrf: str,
    tenant_id: str,
    label: str,
    brand_id: str = "default",
    platform: str | None = None,
    available: bool,
) -> str:
    disabled = "" if available else ' disabled aria-disabled="true"'
    pending_label = escape(translate("common.connecting"))
    platform_input = (
        f'<input type="hidden" name="platform" value="{escape(platform)}">' if platform else ""
    )
    return f"""<form method="post" action="{escape(action)}" data-channel-oauth-form data-pending-label="{pending_label}">
<input type="hidden" name="csrf_token" value="{escape(csrf)}">
<input type="hidden" name="tenant_id" value="{escape(tenant_id)}">
<input type="hidden" name="brand_id" value="{escape(brand_id)}">{platform_input}
<button class="saas-button primary small" type="submit" data-pending-label="{pending_label}"{disabled}>{escape(label)}</button>
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
        translate("channels.available") if available else translate("channels.admin_configuration")
    )
    state_class = "ready" if available else "managed"
    return f"""<article class="saas-provider-card{" disabled" if not available else ""}">
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
    requested_brand_id = request.query_params.get("brand_id", "") if principal.is_admin else ""
    try:
        channel_brand_id = (
            normalize_agent_slug(requested_brand_id) if requested_brand_id else "default"
        )
    except AgentControlPlaneValidationError as exc:
        raise HTTPException(status_code=422, detail="invalid_agent_scope") from exc
    async with get_session_factory()() as session:
        if requested_brand_id:
            agent_scope_exists = await session.scalar(
                select(models.Agent.id)
                .where(
                    models.Agent.tenant_id == tenant_id,
                    models.Agent.legacy_brand_id == channel_brand_id,
                    models.Agent.status == "active",
                )
                .limit(1)
            )
            if agent_scope_exists is None:
                raise HTTPException(status_code=404, detail="agent_not_found")
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
        owner_names = dict(
            (
                await session.execute(
                    select(models.AdminUser.id, models.AdminUser.username).where(
                        models.AdminUser.tenant_id == tenant_id,
                    )
                )
            ).all()
        )
        job_scope = models.ProvisioningJob.tenant_id == tenant_id
        if not principal.is_admin:
            job_scope = and_(
                job_scope,
                models.ProvisioningJob.owner_user_id == principal.user_id,
            )
        jobs = list(
            (
                await session.execute(
                    select(models.ProvisioningJob)
                    .where(job_scope)
                    .order_by(models.ProvisioningJob.created_at.desc())
                    .limit(12)
                )
            )
            .scalars()
            .all()
        )
    settings = get_settings()
    redis = aioredis.from_url(settings.redis_url)
    try:
        kill_switch_values = (
            await redis.mget(
                [f"killswitch:account:{tenant_id}:{account.id}" for account in accounts]
            )
            if accounts
            else []
        )
    finally:
        await redis.aclose()
    kill_switches = {
        account.id: kill_switch_values[index] is not None for index, account in enumerate(accounts)
    }
    facebook_app = await facebook_app_credentials(tenant_id)
    instagram_app = await instagram_app_credentials(tenant_id)
    x_available = settings.x_integration_enabled and x_app_credentials() is not None
    facebook_available = settings.facebook_messenger_enabled and facebook_app is not None
    instagram_direct_available = settings.instagram_messaging_enabled and instagram_app is not None
    instagram_meta_available = settings.instagram_messaging_enabled and facebook_app is not None
    csrf = _csrf(request)
    connected_channels = "".join(
        _render_connected_channel(
            account,
            tenant_id=tenant_id,
            owner_name=owner_names.get(account.owner_user_id),
            kill_switch_enabled=kill_switches.get(account.id, False),
        )
        for account in accounts
    )
    if not connected_channels:
        connected_channels = empty_state(
            translate("channels.connected_empty_title"),
            translate("channels.connected_empty_description"),
        )
    job_cards = (
        "".join(
            f"""<article class="saas-channel-job" data-channel-job
data-job-url="{_tenant_root(tenant_id)}/channels/jobs/{job.id}"
data-job-status="{escape(job.status)}">
<div><img src="{_channel_icon_path(job.platform)}" alt=""><strong>{escape(job.platform.title())}</strong>
<span>{format_datetime(job.created_at, include_year=True)}</span>
<span>{escape(translate("channels.owner", owner=owner_names.get(job.owner_user_id) or translate("channels.organization_account")))}</span></div>
<div>{status_badge(job.status)}<span data-job-step>{escape(job.current_step)}</span>{_channel_job_action(job, tenant_id=tenant_id, csrf=csrf)}</div>
{f'<p class="saas-job-error">{escape(_channel_job_error_message(job))}</p>' if job.last_error_code else ""}
</article>"""
            for job in jobs
        )
        or f'<p class="saas-muted">{escape(translate("channels.no_jobs"))}</p>'
    )
    oauth_status = request.query_params.get("status", "")
    job_id = request.query_params.get("job_id", "")
    error_code = request.query_params.get("code", "")
    banner = ""
    if oauth_status in {"processing", "connected"}:
        banner = (
            '<div class="saas-alert success" role="status">'
            f"{escape(translate('channels.banner.processing'))}"
            f"{escape(translate('channels.banner.job', job_id=job_id)) if job_id else ''}</div>"
        )
    elif oauth_status == "error":
        banner = (
            '<div class="saas-alert danger" role="alert">'
            f"{escape(translate('channels.banner.error', error_code=error_code or 'oauth_failed'))}</div>"
        )
    if channel_brand_id != "default":
        banner = (
            '<div class="saas-alert" role="status">'
            f"{escape(translate('channels.agent_scope_banner', agent_id=channel_brand_id))}</div>"
            f"{banner}"
        )
    x_actions = _channel_oauth_form(
        action=f"{_tenant_root(tenant_id)}/channels/oauth/x/start",
        csrf=csrf,
        tenant_id=tenant_id,
        label=translate("channels.oauth.x"),
        brand_id=channel_brand_id,
        available=x_available,
    )
    facebook_actions = _channel_oauth_form(
        action=f"{_tenant_root(tenant_id)}/channels/oauth/meta/start",
        csrf=csrf,
        tenant_id=tenant_id,
        label=translate("channels.oauth.facebook"),
        brand_id=channel_brand_id,
        platform="facebook",
        available=facebook_available,
    )
    instagram_actions = _channel_oauth_form(
        action=f"{_tenant_root(tenant_id)}/channels/oauth/instagram/start",
        csrf=csrf,
        tenant_id=tenant_id,
        label="Instagram Login",
        brand_id=channel_brand_id,
        available=instagram_direct_available,
    ) + _channel_oauth_form(
        action=f"{_tenant_root(tenant_id)}/channels/oauth/meta/start",
        csrf=csrf,
        tenant_id=tenant_id,
        label=translate("channels.oauth.instagram_meta"),
        brand_id=channel_brand_id,
        platform="instagram",
        available=instagram_meta_available,
    )
    telegram_available = settings.platform_integration_enabled("telegram")
    whatsapp_available = settings.platform_integration_enabled("whatsapp")
    feishu_available = settings.platform_integration_enabled("feishu")
    email_available = settings.platform_integration_enabled("email")

    def manual_button(dialog_id: str, label: str, *, available: bool) -> str:
        disabled = "" if available else ' disabled aria-disabled="true"'
        return (
            '<button class="saas-button primary small" type="button" '
            f'data-open-channel-dialog="{escape(dialog_id)}"{disabled}>'
            f"{escape(label)}</button>"
        )

    feishu_actions = manual_button(
        "feishu-dialog",
        translate("channels.fill_feishu_credentials"),
        available=feishu_available,
    )
    if principal.is_admin:
        feishu_actions += (
            f'<a class="saas-button small" href="{_tenant_root(tenant_id)}/channels/feishu/handoff">'
            f"{escape(translate('channels.feishu_handoff'))}</a>"
        )
    provider_cards = "".join(
        (
            _channel_provider_card(
                platform="x",
                title="X",
                description=translate("channels.provider.x_description"),
                actions=x_actions,
                available=x_available,
            ),
            _channel_provider_card(
                platform="facebook",
                title="Facebook",
                description=translate("channels.provider.facebook_description"),
                actions=facebook_actions,
                available=facebook_available,
            ),
            _channel_provider_card(
                platform="instagram",
                title="Instagram",
                description=translate("channels.provider.instagram_description"),
                actions=instagram_actions,
                available=instagram_direct_available or instagram_meta_available,
            ),
            _channel_provider_card(
                platform="telegram",
                title="Telegram Bot",
                description=translate("channels.provider.telegram_description"),
                actions=manual_button(
                    "telegram-dialog",
                    translate("channels.fill_bot_token"),
                    available=telegram_available,
                ),
                available=telegram_available,
            ),
            _channel_provider_card(
                platform="email",
                title="Email",
                description=translate("channels.provider.email_description"),
                actions=manual_button(
                    "email-dialog",
                    translate("channels.fill_email_credentials"),
                    available=email_available,
                ),
                available=email_available,
            ),
            _channel_provider_card(
                platform="whatsapp",
                title="WhatsApp",
                description=translate("channels.provider.whatsapp_description"),
                actions=manual_button(
                    "whatsapp-dialog",
                    translate("channels.fill_whatsapp_credentials"),
                    available=whatsapp_available,
                ),
                available=whatsapp_available,
            ),
            _channel_provider_card(
                platform="feishu",
                title="Feishu",
                description=translate("channels.provider.feishu_description"),
                actions=feishu_actions,
                available=feishu_available,
            ),
        )
    )
    pending_label = escape(translate("common.connecting"))
    dialogs = f"""<dialog class="saas-channel-dialog" id="telegram-dialog" aria-labelledby="telegram-dialog-title">
<form class="saas-form" method="post" action="{_tenant_root(tenant_id)}/channels/accounts/telegram" data-channel-credential-form data-pending-label="{pending_label}">
<div class="saas-dialog-header"><div><div class="saas-eyebrow">Telegram</div><h2 id="telegram-dialog-title">{escape(translate("channels.telegram_dialog_title"))}</h2></div>
<button class="saas-dialog-close" type="button" data-close-channel-dialog aria-label="{escape(translate("button.close"))}">×</button></div>
<div class="saas-alert">{escape(translate("channels.telegram_secret_notice"))}</div>
<input type="hidden" name="csrf_token" value="{csrf}"><input type="hidden" name="tenant_id" value="{escape(tenant_id)}">
<input type="hidden" name="brand_id" value="{escape(channel_brand_id)}"><input type="hidden" name="automation_default" value="BOT_DRAFT_ONLY">
<label for="telegram-name">{escape(translate("channels.display_name"))} <span class="saas-optional">{escape(translate("common.optional"))}</span></label><input id="telegram-name" name="name" placeholder="{escape(translate("channels.telegram_name_placeholder"))}">
<label for="telegram-token">Bot Token</label><input id="telegram-token" name="token" type="password" autocomplete="new-password" required>
<div class="saas-dialog-actions"><button class="saas-button" type="button" data-close-channel-dialog>{escape(translate("button.cancel"))}</button>
<button class="saas-button primary" type="submit" data-pending-label="{pending_label}">{escape(translate("channels.validate_connect"))}</button></div></form></dialog>
<dialog class="saas-channel-dialog wide" id="email-dialog" aria-labelledby="email-dialog-title">
<form class="saas-form" method="post" action="{_tenant_root(tenant_id)}/channels/accounts/email" data-channel-credential-form data-pending-label="{pending_label}">
<div class="saas-dialog-header"><div><div class="saas-eyebrow">Email</div><h2 id="email-dialog-title">{escape(translate("channels.email_dialog_title"))}</h2></div>
<button class="saas-dialog-close" type="button" data-close-channel-dialog aria-label="{escape(translate("button.close"))}">×</button></div>
<div class="saas-alert">{escape(translate("channels.email_secret_notice"))}</div>
<input type="hidden" name="csrf_token" value="{csrf}"><input type="hidden" name="tenant_id" value="{escape(tenant_id)}">
<input type="hidden" name="brand_id" value="{escape(channel_brand_id)}"><input type="hidden" name="automation_default" value="BOT_DRAFT_ONLY">
<div class="saas-form-grid"><div><label for="email-address">{escape(translate("channels.email_address"))}</label><input id="email-address" name="email_address" type="email" autocomplete="email" required></div>
<div><label for="email-username">{escape(translate("channels.login_username"))}</label><input id="email-username" name="username" autocomplete="username" required></div></div>
<label for="email-password">{escape(translate("channels.password"))}</label><input id="email-password" name="password" type="password" autocomplete="new-password" required>
<div class="saas-form-grid"><div><label for="imap-host">IMAP Host</label><input id="imap-host" name="imap_host" required></div>
<div><label for="smtp-host">SMTP Host</label><input id="smtp-host" name="smtp_host" required></div></div>
<input type="hidden" name="imap_port" value="993"><input type="hidden" name="smtp_port" value="465">
<input type="hidden" name="smtp_security" value="ssl"><input type="hidden" name="mailbox" value="INBOX">
<input type="hidden" name="internal_domain_policy" value="ignore">
<div class="saas-dialog-actions"><button class="saas-button" type="button" data-close-channel-dialog>{escape(translate("button.cancel"))}</button>
<button class="saas-button primary" type="submit" data-pending-label="{pending_label}">{escape(translate("channels.validate_connect"))}</button></div></form></dialog>
<dialog class="saas-channel-dialog wide" id="whatsapp-dialog" aria-labelledby="whatsapp-dialog-title">
<form class="saas-form" method="post" action="{_tenant_root(tenant_id)}/channels/accounts/whatsapp" data-channel-credential-form data-pending-label="{pending_label}">
<div class="saas-dialog-header"><div><div class="saas-eyebrow">WhatsApp</div><h2 id="whatsapp-dialog-title">{escape(translate("channels.whatsapp_dialog_title"))}</h2></div>
<button class="saas-dialog-close" type="button" data-close-channel-dialog aria-label="{escape(translate("button.close"))}">×</button></div>
<input type="hidden" name="csrf_token" value="{csrf}"><input type="hidden" name="tenant_id" value="{escape(tenant_id)}">
<input type="hidden" name="brand_id" value="{escape(channel_brand_id)}"><input type="hidden" name="automation_default" value="BOT_DRAFT_ONLY"><input type="hidden" name="api_version" value="v23.0">
<label for="whatsapp-name">{escape(translate("channels.display_name"))}</label><input id="whatsapp-name" name="name" required>
<label for="whatsapp-account-id">WhatsApp Business Account ID</label><input id="whatsapp-account-id" name="external_account_id" required>
<label for="whatsapp-app-id">Meta App ID</label><input id="whatsapp-app-id" name="app_id" required>
<label for="whatsapp-access-token">Access Token</label><input id="whatsapp-access-token" name="access_token" type="password" autocomplete="new-password" required>
<label for="whatsapp-app-secret">App Secret</label><input id="whatsapp-app-secret" name="app_secret" type="password" autocomplete="new-password" required>
<label for="whatsapp-verify-token">Verify Token</label><input id="whatsapp-verify-token" name="verify_token" type="password" autocomplete="new-password" required>
<div class="saas-dialog-actions"><button class="saas-button" type="button" data-close-channel-dialog>{escape(translate("button.cancel"))}</button>
<button class="saas-button primary" type="submit" data-pending-label="{pending_label}">{escape(translate("channels.validate_connect"))}</button></div></form></dialog>
<dialog class="saas-channel-dialog wide" id="feishu-dialog" aria-labelledby="feishu-dialog-title">
<form class="saas-form" method="post" action="{_tenant_root(tenant_id)}/channels/accounts/feishu" data-channel-credential-form data-pending-label="{pending_label}">
<div class="saas-dialog-header"><div><div class="saas-eyebrow">Feishu</div><h2 id="feishu-dialog-title">{escape(translate("channels.feishu_dialog_title"))}</h2></div>
<button class="saas-dialog-close" type="button" data-close-channel-dialog aria-label="{escape(translate("button.close"))}">×</button></div>
<input type="hidden" name="csrf_token" value="{csrf}"><input type="hidden" name="tenant_id" value="{escape(tenant_id)}">
<input type="hidden" name="brand_id" value="{escape(channel_brand_id)}"><input type="hidden" name="automation_default" value="BOT_DRAFT_ONLY">
<input type="hidden" name="api_base_url" value="https://open.feishu.cn/open-apis"><input type="hidden" name="group_mode" value="mentions_only">
<label for="feishu-name">{escape(translate("channels.display_name"))}</label><input id="feishu-name" name="name" required>
<label for="feishu-app-id">App ID</label><input id="feishu-app-id" name="app_id" required>
<label for="feishu-app-secret">App Secret</label><input id="feishu-app-secret" name="app_secret" type="password" autocomplete="new-password" required>
<label for="feishu-verification-token">Verification Token</label><input id="feishu-verification-token" name="verification_token" type="password" autocomplete="new-password" required>
<label for="feishu-encrypt-key">Encrypt Key</label><input id="feishu-encrypt-key" name="encrypt_key" type="password" autocomplete="new-password" required>
<div class="saas-dialog-actions"><button class="saas-button" type="button" data-close-channel-dialog>{escape(translate("button.cancel"))}</button>
<button class="saas-button primary" type="submit" data-pending-label="{pending_label}">{escape(translate("channels.validate_connect"))}</button></div></form></dialog>"""
    body = f"""{banner}<section><div class="saas-section-title"><div><h2>{escape(translate("channels.connected_title"))}</h2>
<p>{escape(translate("channels.connected_description"))}</p></div></div>
<div class="saas-connected-grid">{connected_channels}</div></section>
<section id="add-channels"><div class="saas-section-title"><div><h2>{escape(translate("channels.add_title"))}</h2>
<p>{escape(translate("channels.add_description"))}</p></div></div>
<div class="saas-provider-grid">{provider_cards}</div></section>
<section><div class="saas-section-title"><div><h2>{escape(translate("channels.progress_title"))}</h2>
<p>{escape(translate("channels.progress_description"))}</p></div></div>
<div class="saas-channel-jobs">{job_cards}</div></section>{dialogs}
<script src="/static/channels.js" defer></script>"""
    response = _render_page(
        principal=principal,
        tenant_id=tenant_id,
        title=translate("channels.title"),
        description=translate("channels.description"),
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
    body = f"""<section class="saas-card"><div class="saas-card-header"><div><h2>{escape(translate("profile.account_information"))}</h2></div></div><div class="saas-card-body">
{definition_list(((translate("profile.username"), principal.username), (translate("profile.role"), principal.role), ("Tenant", tenant_id), ("User ID", principal.user_id or translate("common.system"))))}
<p><a class="saas-button primary" href="/auth/change-password">{escape(translate("profile.change_password"))}</a></p></div></section>
<div class="saas-section-title"><div><h2>{escape(translate("profile.permissions"))}</h2>
<p>{escape(translate("profile.permissions_description"))}</p></div>
{secondary_action(f"{_tenant_root(tenant_id)}/channels", translate("profile.open_channels"))}</div>"""
    return _render_page(
        principal=principal,
        tenant_id=tenant_id,
        title=translate("profile.title"),
        description=translate("profile.description"),
        body=body,
        active_navigation="profile",
        inbox_count=inbox_summary.total,
    )


def _channel_actor(principal: Principal) -> ChannelActor:
    return ChannelActor(
        actor=principal.actor,
        role="ADMIN" if principal.is_admin else "USER",
        user_id=principal.user_id,
        session_id=principal.session_id,
    )


def _channel_management_http_error(exc: ChannelManagementError) -> HTTPException:
    if isinstance(exc, ChannelNotFoundError):
        return HTTPException(status_code=404, detail=exc.code)
    if isinstance(exc, ChannelConflictError):
        return HTTPException(status_code=409, detail=exc.code)
    if isinstance(exc, ChannelPermissionError):
        return HTTPException(status_code=403, detail=exc.code)
    if isinstance(exc, ChannelValidationError) and exc.code.endswith("_integration_disabled"):
        return HTTPException(status_code=503, detail=exc.code)
    return HTTPException(status_code=422, detail=exc.code)


def _required_form_bool(form: dict[str, str], field_name: str) -> bool:
    value = form.get(field_name)
    if value == "true":
        return True
    if value == "false":
        return False
    raise HTTPException(status_code=422, detail=f"invalid_boolean:{field_name}")


def _required_form_int(form: dict[str, str], field_name: str) -> int:
    try:
        return int(form.get(field_name) or "")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"invalid_integer:{field_name}") from exc


def _channel_redirect(tenant_id: str, account_id: uuid.UUID | None = None) -> RedirectResponse:
    path = f"{_tenant_root(tenant_id)}/channels"
    if account_id is not None:
        path = f"{path}/accounts/{account_id}"
    return RedirectResponse(path, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/app/t/{tenant_id}/channels/accounts/{platform}")
async def connect_channel_account(
    request: Request,
    tenant_id: str,
    platform: str,
) -> Response:
    principal = await _require_tenant_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    try:
        command = build_provisioning_command(
            route_tenant_id=tenant_id,
            platform=platform,
            actor=_channel_actor(principal),
            values=form,
        )
        job_id = await submit_channel_provisioning(command)
    except ChannelManagementError as exc:
        raise _channel_management_http_error(exc) from exc
    except (PermissionError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RedirectResponse(
        f"{_tenant_root(tenant_id)}/channels?status=processing&job_id={job_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/app/t/{tenant_id}/profile/accounts/{platform}")
async def connect_personal_account_compatibility(
    request: Request,
    tenant_id: str,
    platform: str,
) -> Response:
    return await connect_channel_account(request, tenant_id, platform)


@router.get(
    "/app/t/{tenant_id}/channels/accounts/{account_id}",
    response_class=HTMLResponse,
)
async def channel_account_detail(
    request: Request,
    tenant_id: str,
    account_id: uuid.UUID,
) -> Response:
    principal = await _require_tenant_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    async with get_session_factory()() as session:
        inbox_summary = await _load_inbox_summary(session, principal, tenant_id)
        statement = select(models.PlatformAccount).where(
            models.PlatformAccount.id == account_id,
            _account_scope_condition(principal, tenant_id),
        )
        account = await session.scalar(statement)
        if account is None:
            raise HTTPException(status_code=404, detail="platform_account_not_found")
        owner = (
            await session.get(models.AdminUser, account.owner_user_id)
            if account.owner_user_id is not None
            else None
        )
        assignable_owners = (
            list(
                (
                    await session.execute(
                        select(models.AdminUser)
                        .where(
                            models.AdminUser.tenant_id == tenant_id,
                            models.AdminUser.role == "USER",
                            models.AdminUser.status == "active",
                        )
                        .order_by(models.AdminUser.username)
                    )
                ).scalars()
            )
            if principal.is_admin
            else []
        )
    redis = aioredis.from_url(get_settings().redis_url)
    try:
        kill_switch_enabled = bool(
            await redis.exists(f"killswitch:account:{tenant_id}:{account_id}")
        )
    finally:
        await redis.aclose()
    csrf = _csrf(request)
    account_path = f"{_tenant_root(tenant_id)}/channels/accounts/{account.id}"
    next_status_enabled = account.status != "active"
    next_status_label = translate(
        "channels.account.enable" if next_status_enabled else "channels.account.disable"
    )
    next_automation_target = (
        "BOT_DRAFT_ONLY" if account.automation_default == "BOT_ACTIVE" else "BOT_ACTIVE"
    )
    owner_options = '<option value="">Tenant managed</option>' + "".join(
        f'<option value="{candidate.id}"'
        f"{' selected' if candidate.id == account.owner_user_id else ''}>"
        f"{escape(candidate.username)}</option>"
        for candidate in assignable_owners
    )
    owner_form = (
        f"""<form class="saas-form" method="post" action="{account_path}/owner">
<input type="hidden" name="csrf_token" value="{csrf}"><input type="hidden" name="expected_config_version" value="{account.config_version}">
<label for="channel-owner">{escape(translate("channels.account.owner"))}</label><select id="channel-owner" name="owner_user_id">{owner_options}</select>
<button class="saas-button" type="submit">{escape(translate("channels.account.save_owner"))}</button></form>"""
        if principal.is_admin
        else ""
    )
    xchat_form = ""
    if account.platform == "x" and get_settings().xchat_enabled:
        xchat_form = f"""<form class="saas-form" method="post" action="{account_path}/xchat/repair">
<input type="hidden" name="csrf_token" value="{csrf}"><label for="channel-xchat-pin">XChat PIN</label>
<input id="channel-xchat-pin" name="xchat_pin" type="password" inputmode="numeric" pattern="[0-9]{{4}}" maxlength="4" autocomplete="new-password" required>
<button class="saas-button" type="submit">{escape(translate("channels.account.repair_xchat"))}</button></form>"""
    body = f"""<section class="saas-card"><div class="saas-card-header"><div><h2>{escape(account.name)}</h2>
<p>{escape(account.platform.title())} · {escape(account.public_id or str(account.id))}</p></div>{status_badge(account.status)}</div>
<div class="saas-card-body">{definition_list(((translate("channels.account.owner"), owner.username if owner else translate("channels.organization_account")), (translate("channels.account.health"), _channel_account_health(account)[1]), (translate("channels.account.automation"), account.automation_default), (translate("channels.account.kill_switch"), translate("channels.kill_switch.enabled") if kill_switch_enabled else translate("channels.kill_switch.disabled")), ("Config version", account.config_version)))}</div></section>
<div class="saas-grid two"><section class="saas-card"><div class="saas-card-header"><h2>{escape(translate("channels.account.lifecycle"))}</h2></div><div class="saas-card-body">
<form class="saas-form" method="post" action="{account_path}/rename"><input type="hidden" name="csrf_token" value="{csrf}"><input type="hidden" name="expected_config_version" value="{account.config_version}">
<label for="channel-account-name">{escape(translate("channels.display_name"))}</label><input id="channel-account-name" name="name" value="{escape(account.name)}" maxlength="255" required><button class="saas-button" type="submit">{escape(translate("channels.account.rename"))}</button></form>
<form class="saas-form" method="post" action="{account_path}/status"><input type="hidden" name="csrf_token" value="{csrf}"><input type="hidden" name="expected_config_version" value="{account.config_version}"><input type="hidden" name="expected_status" value="{escape(account.status)}"><input type="hidden" name="enabled" value="{"true" if next_status_enabled else "false"}"><button class="saas-button" type="submit">{escape(next_status_label)}</button></form>
<p><a class="saas-button" href="{_tenant_root(tenant_id)}/channels#add-channels">{escape(translate("channels.account.reauthorize"))}</a></p></div></section>
<section class="saas-card"><div class="saas-card-header"><h2>{escape(translate("channels.account.controls"))}</h2></div><div class="saas-card-body">
<form class="saas-form" method="post" action="{account_path}/automation"><input type="hidden" name="csrf_token" value="{csrf}"><input type="hidden" name="expected_config_version" value="{account.config_version}"><input type="hidden" name="target" value="{next_automation_target}"><button class="saas-button" type="submit">{escape(translate("channels.account.set_automation", target=next_automation_target))}</button></form>
<form class="saas-form" method="post" action="{account_path}/kill-switch"><input type="hidden" name="csrf_token" value="{csrf}"><input type="hidden" name="enabled" value="{"false" if kill_switch_enabled else "true"}"><button class="saas-button" type="submit">{escape(translate("channels.account.set_kill_switch", enabled=not kill_switch_enabled))}</button></form>
{owner_form}{xchat_form}</div></section></div>"""
    response = _render_page(
        principal=principal,
        tenant_id=tenant_id,
        title=translate("channels.account.title"),
        description=translate("channels.account.description"),
        body=body,
        active_navigation="channels",
        inbox_count=inbox_summary.total,
        breadcrumbs=(
            (translate("channels.title"), f"{_tenant_root(tenant_id)}/channels"),
            (account.name, None),
        ),
    )
    return _ensure_channel_csrf(response, request, csrf)


def _ensure_channel_csrf(response: Response, request: Request, csrf: str) -> Response:
    if not request.cookies.get("reply_admin_csrf"):
        response.set_cookie(
            "reply_admin_csrf",
            csrf,
            httponly=False,
            samesite="lax",
            secure=_secure_cookie(request),
        )
    return response


async def _channel_mutation_principal_and_form(
    request: Request,
    tenant_id: str,
) -> tuple[Principal, dict[str, str]] | Response:
    principal = await _require_tenant_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    return principal, form


@router.post("/app/t/{tenant_id}/channels/accounts/{account_id}/rename")
async def rename_channel_account_route(
    request: Request, tenant_id: str, account_id: uuid.UUID
) -> Response:
    context = await _channel_mutation_principal_and_form(request, tenant_id)
    if isinstance(context, Response):
        return context
    principal, form = context
    try:
        await rename_channel_account(
            tenant_id=tenant_id,
            account_id=account_id,
            actor=_channel_actor(principal),
            name=form.get("name") or "",
            expected_config_version=_required_form_int(form, "expected_config_version"),
        )
    except ChannelManagementError as exc:
        raise _channel_management_http_error(exc) from exc
    return _channel_redirect(tenant_id, account_id)


@router.post("/app/t/{tenant_id}/channels/accounts/{account_id}/status")
async def set_channel_account_status_route(
    request: Request, tenant_id: str, account_id: uuid.UUID
) -> Response:
    context = await _channel_mutation_principal_and_form(request, tenant_id)
    if isinstance(context, Response):
        return context
    principal, form = context
    try:
        await set_channel_account_status(
            tenant_id=tenant_id,
            account_id=account_id,
            actor=_channel_actor(principal),
            enabled=_required_form_bool(form, "enabled"),
            expected_config_version=_required_form_int(form, "expected_config_version"),
            expected_status=form.get("expected_status") or "",
        )
    except ChannelManagementError as exc:
        raise _channel_management_http_error(exc) from exc
    return _channel_redirect(tenant_id, account_id)


@router.post("/app/t/{tenant_id}/channels/accounts/{account_id}/automation")
async def set_channel_account_automation_route(
    request: Request, tenant_id: str, account_id: uuid.UUID
) -> Response:
    context = await _channel_mutation_principal_and_form(request, tenant_id)
    if isinstance(context, Response):
        return context
    principal, form = context
    target = form.get("target") or ""
    try:
        await set_channel_account_automation(
            tenant_id=tenant_id,
            account_id=account_id,
            actor=_channel_actor(principal),
            target=target,  # type: ignore[arg-type]
            expected_config_version=_required_form_int(form, "expected_config_version"),
        )
    except ChannelManagementError as exc:
        raise _channel_management_http_error(exc) from exc
    return _channel_redirect(tenant_id, account_id)


@router.post("/app/t/{tenant_id}/channels/accounts/{account_id}/kill-switch")
async def set_channel_account_kill_switch_route(
    request: Request, tenant_id: str, account_id: uuid.UUID
) -> Response:
    context = await _channel_mutation_principal_and_form(request, tenant_id)
    if isinstance(context, Response):
        return context
    principal, form = context
    try:
        await set_channel_account_kill_switch(
            tenant_id=tenant_id,
            account_id=account_id,
            actor=_channel_actor(principal),
            enabled=_required_form_bool(form, "enabled"),
        )
    except ChannelManagementError as exc:
        raise _channel_management_http_error(exc) from exc
    return _channel_redirect(tenant_id, account_id)


@router.post("/app/t/{tenant_id}/channels/accounts/{account_id}/owner")
async def assign_channel_account_owner_route(
    request: Request, tenant_id: str, account_id: uuid.UUID
) -> Response:
    context = await _channel_mutation_principal_and_form(request, tenant_id)
    if isinstance(context, Response):
        return context
    principal, form = context
    owner_value = (form.get("owner_user_id") or "").strip()
    try:
        owner_user_id = uuid.UUID(owner_value) if owner_value else None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid_owner_user_id") from exc
    try:
        await assign_channel_account_owner(
            tenant_id=tenant_id,
            account_id=account_id,
            actor=_channel_actor(principal),
            owner_user_id=owner_user_id,
            expected_config_version=_required_form_int(form, "expected_config_version"),
        )
    except ChannelManagementError as exc:
        raise _channel_management_http_error(exc) from exc
    return _channel_redirect(tenant_id, account_id)


@router.post("/app/t/{tenant_id}/channels/accounts/{account_id}/xchat/repair")
async def repair_channel_xchat_route(
    request: Request, tenant_id: str, account_id: uuid.UUID
) -> Response:
    context = await _channel_mutation_principal_and_form(request, tenant_id)
    if isinstance(context, Response):
        return context
    principal, form = context
    try:
        await repair_channel_xchat(
            tenant_id=tenant_id,
            account_id=account_id,
            actor=_channel_actor(principal),
            pin=form.get("xchat_pin") or "",
        )
    except ChannelManagementError as exc:
        raise _channel_management_http_error(exc) from exc
    except XChatActivationError as exc:
        raise HTTPException(status_code=422, detail=exc.code) from exc
    return _channel_redirect(tenant_id, account_id)


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
    async with get_session_factory()() as session:
        statement = select(models.ProvisioningJob).where(
            models.ProvisioningJob.id == job_id,
            models.ProvisioningJob.tenant_id == tenant_id,
        )
        if not principal.is_admin:
            statement = statement.where(models.ProvisioningJob.owner_user_id == principal.user_id)
        job = (await session.execute(statement)).scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="provisioning_job_not_found")
    payload = public_job(job)
    payload["last_error_message"] = _channel_job_error_message(job) if job.last_error_code else None
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return payload


@router.post("/app/t/{tenant_id}/channels/jobs/{job_id}/retry")
async def retry_channel_job_route(
    request: Request,
    tenant_id: str,
    job_id: uuid.UUID,
) -> Response:
    context = await _channel_mutation_principal_and_form(request, tenant_id)
    if isinstance(context, Response):
        return context
    principal, _form_values = context
    try:
        await retry_channel_job(
            tenant_id=tenant_id,
            job_id=job_id,
            actor=_channel_actor(principal),
        )
    except ChannelManagementError as exc:
        raise _channel_management_http_error(exc) from exc
    return _channel_redirect(tenant_id)


def _feishu_handoff_http_error(exc: FeishuHandoffError) -> HTTPException:
    if isinstance(exc, FeishuHandoffNotFound):
        return HTTPException(status_code=404, detail=exc.code)
    if isinstance(exc, FeishuHandoffConflict):
        return HTTPException(status_code=409, detail=exc.code)
    return HTTPException(status_code=422, detail=exc.code)


def _feishu_handoff_redirect(tenant_id: str, notice: str) -> RedirectResponse:
    return RedirectResponse(
        f"{_tenant_root(tenant_id)}/channels/feishu/handoff?{urlencode({'notice': notice})}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get(
    "/app/t/{tenant_id}/channels/feishu/handoff",
    response_class=HTMLResponse,
)
async def tenant_feishu_handoff_page(request: Request, tenant_id: str) -> Response:
    principal = await _require_tenant_admin_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    async with get_session_factory()() as session:
        inbox_summary = await _load_inbox_summary(session, principal, tenant_id)
    snapshot = await load_feishu_handoff_snapshot(tenant_id)
    csrf = _csrf(request)
    selected_account_id = snapshot.config.feishu_platform_account_id if snapshot.config else None
    account_options = "".join(
        f'<option value="{account.id}"'
        f"{' selected' if account.id == selected_account_id else ''}>"
        f"{escape(account.name)} · {escape(account.public_id or str(account.id))}</option>"
        for account in snapshot.accounts
    )
    if not account_options:
        account_options = '<option value="">Connect a Feishu account first</option>'
    config = snapshot.config
    operator_rows = (
        "".join(
            f"<tr><td>{escape(operator.display_name or '—')}</td>"
            f"<td><code>{escape(operator.operator_open_id)}</code></td>"
            f"<td>{status_badge(operator.status)}</td><td>"
            f'<form method="post" action="{_tenant_root(tenant_id)}/channels/feishu/handoff/operators/{operator.id}/status">'
            f'<input type="hidden" name="csrf_token" value="{csrf}">'
            f'<input type="hidden" name="enabled" value="{"false" if operator.status == "ACTIVE" else "true"}">'
            f'<button class="saas-button small" type="submit">{escape(translate("admin.handoff.disable") if operator.status == "ACTIVE" else translate("admin.handoff.enable"))}</button>'
            "</form></td></tr>"
            for operator in snapshot.operators
        )
        or '<tr><td colspan="4">—</td></tr>'
    )
    failure_rows = (
        "".join(
            f"<tr><td><code>{str(intent.public_id)[:8]}</code></td>"
            f"<td>{status_badge(intent.status)}</td>"
            f"<td><code>{escape(intent.last_error_code or '—')}</code></td></tr>"
            for intent in snapshot.failures
        )
        or '<tr><td colspan="3">—</td></tr>'
    )
    notice = request.query_params.get("notice") or ""
    notice_html = f'<div class="saas-alert" role="status">{escape(notice)}</div>' if notice else ""
    body = f"""{notice_html}<div class="saas-grid two"><section class="saas-card"><div class="saas-card-header"><h2>{escape(translate("admin.handoff.route_title"))}</h2></div><div class="saas-card-body">
<form class="saas-form" method="post" action="{_tenant_root(tenant_id)}/channels/feishu/handoff/config"><input type="hidden" name="csrf_token" value="{csrf}">
<label for="handoff-account">{escape(translate("admin.handoff.account"))}</label><select id="handoff-account" name="feishu_platform_account_id" required>{account_options}</select>
<label for="handoff-chat">{escape(translate("admin.handoff.chat_id"))}</label><input id="handoff-chat" name="destination_chat_id" value="{escape(config.destination_chat_id if config else "")}" maxlength="256" required>
<label><input type="checkbox" name="enabled" value="true"{" checked" if config and config.enabled else ""}> {escape(translate("admin.handoff.enable_cards"))}</label>
<button class="saas-button primary" type="submit">{escape(translate("admin.handoff.save_config"))}</button></form>
<form method="post" action="{_tenant_root(tenant_id)}/channels/feishu/handoff/test"><input type="hidden" name="csrf_token" value="{csrf}"><button class="saas-button" type="submit">{escape(translate("admin.handoff.send_test"))}</button></form></div></section>
<section class="saas-card"><div class="saas-card-header"><h2>{escape(translate("admin.handoff.operator_permissions"))}</h2></div><div class="saas-card-body">
<form class="saas-form" method="post" action="{_tenant_root(tenant_id)}/channels/feishu/handoff/operators"><input type="hidden" name="csrf_token" value="{csrf}">
<label for="operator-open-id">Operator Open ID</label><input id="operator-open-id" name="operator_open_id" maxlength="128" required>
<label for="operator-name">{escape(translate("admin.handoff.display_name"))}</label><input id="operator-name" name="display_name" maxlength="100">
<label><input type="checkbox" name="can_claim" value="true" checked> {escape(translate("admin.handoff.can_claim"))}</label>
<label><input type="checkbox" name="can_resolve" value="true" checked> {escape(translate("admin.handoff.can_resolve"))}</label>
<button class="saas-button primary" type="submit">{escape(translate("admin.handoff.save_operator"))}</button></form></div></section></div>
<section class="saas-card"><div class="saas-card-header"><h2>{escape(translate("admin.handoff.operator_permissions"))}</h2></div><div class="saas-card-body"><div class="saas-table-wrap"><table class="saas-table"><tbody>{operator_rows}</tbody></table></div></div></section>
<section class="saas-card"><div class="saas-card-header"><h2>{escape(translate("admin.handoff.failures"))}</h2></div><div class="saas-card-body"><div class="saas-table-wrap"><table class="saas-table"><tbody>{failure_rows}</tbody></table></div></div></section>"""
    response = _render_page(
        principal=principal,
        tenant_id=tenant_id,
        title=translate("admin.handoff.title"),
        description=translate("admin.handoff.description"),
        body=body,
        active_navigation="channels",
        inbox_count=inbox_summary.total,
        breadcrumbs=(
            (translate("channels.title"), f"{_tenant_root(tenant_id)}/channels"),
            (translate("admin.handoff.title"), None),
        ),
    )
    return _ensure_channel_csrf(response, request, csrf)


@router.post("/app/t/{tenant_id}/channels/feishu/handoff/config")
async def save_tenant_feishu_handoff_config(request: Request, tenant_id: str) -> Response:
    principal = await _require_tenant_admin_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    try:
        account_id = uuid.UUID(form.get("feishu_platform_account_id") or "")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid_feishu_account_id") from exc
    try:
        await save_feishu_handoff_config(
            tenant_id=tenant_id,
            actor=principal.actor,
            account_id=account_id,
            destination_chat_id=form.get("destination_chat_id") or "",
            enabled=form.get("enabled") == "true",
        )
    except FeishuHandoffError as exc:
        raise _feishu_handoff_http_error(exc) from exc
    return _feishu_handoff_redirect(tenant_id, "config_saved")


@router.post("/app/t/{tenant_id}/channels/feishu/handoff/operators")
async def save_tenant_feishu_handoff_operator(request: Request, tenant_id: str) -> Response:
    principal = await _require_tenant_admin_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    try:
        await upsert_feishu_handoff_operator(
            tenant_id=tenant_id,
            actor=principal.actor,
            operator_open_id=form.get("operator_open_id") or "",
            display_name=form.get("display_name") or "",
            can_claim=form.get("can_claim") == "true",
            can_resolve=form.get("can_resolve") == "true",
        )
    except FeishuHandoffError as exc:
        raise _feishu_handoff_http_error(exc) from exc
    return _feishu_handoff_redirect(tenant_id, "operator_saved")


@router.post("/app/t/{tenant_id}/channels/feishu/handoff/operators/{operator_id}/status")
async def set_tenant_feishu_handoff_operator_status(
    request: Request,
    tenant_id: str,
    operator_id: uuid.UUID,
) -> Response:
    principal = await _require_tenant_admin_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    try:
        await set_feishu_handoff_operator_status(
            tenant_id=tenant_id,
            actor=principal.actor,
            operator_id=operator_id,
            enabled=_required_form_bool(form, "enabled"),
        )
    except FeishuHandoffError as exc:
        raise _feishu_handoff_http_error(exc) from exc
    return _feishu_handoff_redirect(tenant_id, "operator_updated")


@router.post("/app/t/{tenant_id}/channels/feishu/handoff/test")
async def send_tenant_feishu_handoff_test(request: Request, tenant_id: str) -> Response:
    principal = await _require_tenant_admin_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    outcome = await send_feishu_handoff_test_card(
        tenant_id=tenant_id,
        actor=principal.actor,
        title=translate("admin.handoff.test_card_title"),
        content=(
            translate("admin.handoff.test_card_connected", tenant_id=tenant_id)
            + translate("admin.handoff.test_card_safe")
        ),
    )
    return _feishu_handoff_redirect(tenant_id, outcome)


def _knowledge_location(
    tenant_id: str,
    *,
    status_filter: str = "all",
    brand_id: str = "",
    platform: str = "",
    category: str = "",
    notice: str = "",
    **notice_values: object,
) -> str:
    query = {
        key: str(value)
        for key, value in {
            "status_filter": status_filter if status_filter != "all" else "",
            "brand_id": brand_id,
            "platform": platform,
            "category": category,
            "notice": notice,
            **notice_values,
        }.items()
        if value not in (None, "")
    }
    root = f"{_tenant_root(tenant_id)}/knowledge"
    return f"{root}?{urlencode(query)}" if query else root


def _knowledge_http_error(exc: KnowledgeApplicationError) -> HTTPException:
    if isinstance(exc, KnowledgeNotFoundError):
        return HTTPException(status_code=404, detail=exc.code)
    if isinstance(exc, KnowledgeConflictError):
        return HTTPException(status_code=409, detail=exc.code)
    return HTTPException(status_code=422, detail=exc.code)


def _knowledge_filter_select(name: str, label: str, values: tuple[str, ...], selected: str) -> str:
    options = f'<option value="">{escape(translate("common.all"))}</option>' + "".join(
        f'<option value="{escape(value)}"'
        f"{' selected' if value == selected else ''}>{escape(value)}</option>"
        for value in values
    )
    return (
        f'<label class="saas-field"><span>{escape(label)}</span>'
        f'<select name="{escape(name)}">{options}</select></label>'
    )


def _knowledge_document_is_sensitive(document: models.KnowledgeDocument) -> bool:
    return bool(
        document.is_official_contact
        or document.protected_values
        or has_contact_like(document.question or "")
        or has_contact_like(document.reply or "")
    )


def _knowledge_list_question(document: models.KnowledgeDocument) -> str:
    if _knowledge_document_is_sensitive(document):
        return translate("knowledge.detail.sensitive_redacted")
    return document.question[:100]


def _knowledge_visible_metadata(
    document: models.KnowledgeDocument,
    value: object,
) -> object:
    if _knowledge_document_is_sensitive(document) or has_contact_like(str(value or "")):
        return translate("knowledge.detail.sensitive_redacted")
    return value


@router.get("/app/t/{tenant_id}/knowledge", response_class=HTMLResponse)
async def tenant_knowledge(
    request: Request,
    tenant_id: str,
    status_filter: str = "all",
    brand_id: str = "",
    platform: str = "",
    category: str = "",
) -> Response:
    principal = await _require_tenant_admin_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    try:
        async with get_session_factory()() as session:
            inbox_summary = await _load_inbox_summary(session, principal, tenant_id)
            documents = await execute_list_knowledge_documents(
                session,
                ListKnowledgeDocumentsQuery(
                    required_tenant_id=tenant_id,
                    actor=principal.actor,
                    status_filter=status_filter,
                    brand_id=brand_id or None,
                    platform=platform or None,
                    category=category or None,
                ),
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
                    knowledge_review_condition(),
                )
            )
            filter_values = await load_knowledge_filter_values(
                session,
                required_tenant_id=tenant_id,
                actor=principal.actor,
            )
            batch_rows = (
                await session.execute(
                    select(
                        models.KnowledgeDocument.import_batch_id,
                        models.KnowledgeDocument.source_file,
                        func.count(),
                    )
                    .where(
                        models.KnowledgeDocument.tenant_id == tenant_id,
                        models.KnowledgeDocument.import_batch_id.is_not(None),
                        models.KnowledgeDocument.status == "draft",
                        models.KnowledgeDocument.language_detection_status == "english",
                        models.KnowledgeDocument.language_verified.is_(False),
                    )
                    .group_by(
                        models.KnowledgeDocument.import_batch_id,
                        models.KnowledgeDocument.source_file,
                    )
                    .order_by(func.max(models.KnowledgeDocument.created_at).desc())
                )
            ).all()
    except KnowledgeApplicationError as exc:
        raise _knowledge_http_error(exc) from exc

    brands, platforms, categories = filter_values
    safe_brand_filter = brand_id if brand_id in brands else ""
    safe_platform_filter = platform if platform in platforms else ""
    safe_category_filter = category if category in categories else ""

    def tab_href(target: str) -> str:
        return _knowledge_location(
            tenant_id,
            status_filter=target,
            brand_id=safe_brand_filter,
            platform=safe_platform_filter,
            category=safe_category_filter,
        )

    filter_tabs = tabs(
        (
            ("all", tab_href("all"), translate("agent.list_summary", count=sum(counts.values()))),
            (
                "review",
                tab_href("review"),
                translate("knowledge.filter.review", count=int(review_count or 0)),
            ),
            (
                "draft",
                tab_href("draft"),
                translate("knowledge.filter.draft", count=int(counts.get("draft", 0))),
            ),
            (
                "published",
                tab_href("published"),
                translate("knowledge.filter.published", count=int(counts.get("published", 0))),
            ),
        ),
        status_filter,
    )
    safe_brand_prefill = safe_brand_filter or "default"
    filters = f"""<form class="saas-filter-bar" method="get">
<input type="hidden" name="status_filter" value="{escape(status_filter)}">
{_knowledge_filter_select("brand_id", "Brand", brands, safe_brand_filter)}
{_knowledge_filter_select("platform", "Platform", platforms, safe_platform_filter)}
{_knowledge_filter_select("category", translate("common.category"), categories, safe_category_filter)}
<button class="saas-button" type="submit">{escape(translate("common.apply_filters"))}</button></form>"""
    csrf = _csrf(request)
    add_form = f"""<section class="saas-card"><div class="saas-card-header"><div><h2>{escape(translate("knowledge.add_document"))}</h2></div></div>
<div class="saas-card-body"><form class="saas-form" method="post" action="{_tenant_root(tenant_id)}/knowledge/documents">
<input type="hidden" name="csrf_token" value="{escape(csrf)}">
<label>Question<input name="question" maxlength="2000" required></label>
<label>Reply<textarea name="reply" maxlength="10000" required></textarea></label>
<div class="saas-form-row"><label>Brand<input name="brand_id" maxlength="64" value="{escape(safe_brand_prefill)}" required></label>
<label>Platform<input name="platform" maxlength="32" value="{escape(safe_platform_filter)}"></label>
<label>{escape(translate("common.category"))}<input name="category" maxlength="64" value="{escape(safe_category_filter)}"></label></div>
<label>Protected values JSON<input name="protected_values_json" placeholder='["Acme Portal"]'></label>
<label><input type="checkbox" name="is_official_contact" value="true"> Official/contact-only content</label>
<button class="saas-button primary" type="submit">{escape(translate("knowledge.add_document"))}</button></form></div></section>"""
    import_form = f"""<section class="saas-card"><div class="saas-card-header"><div><h2>CSV Import</h2></div></div>
<div class="saas-card-body"><form class="saas-form" method="post" enctype="multipart/form-data" action="{_tenant_root(tenant_id)}/knowledge/import">
<input type="hidden" name="csrf_token" value="{escape(csrf)}"><label>Default brand<input name="brand_id" value="{escape(safe_brand_prefill)}" required></label>
<label>UTF-8 CSV (max 2 MiB / 2000 rows)<input type="file" name="file" accept=".csv" required></label>
<button class="saas-button primary" type="submit">Import</button></form></div></section>"""
    batch_options = "".join(
        f'<option value="{escape(row.import_batch_id)}">CSV batch · {escape(row[2])}</option>'
        for row in batch_rows
    )
    batch_forms = f"""<div class="saas-grid two"><section class="saas-card"><div class="saas-card-body">
<form class="saas-form" method="post" action="{_tenant_root(tenant_id)}/knowledge/bulk-confirm-english"><input type="hidden" name="csrf_token" value="{escape(csrf)}">
<label>English import batch<select name="import_batch_id" required>{batch_options or '<option value="">No pending batch</option>'}</select></label>
<button class="saas-button" type="submit">Confirm English batch</button></form></div></section>
<section class="saas-card"><div class="saas-card-body"><form method="post" action="{_tenant_root(tenant_id)}/knowledge/bulk-publish">
<input type="hidden" name="csrf_token" value="{escape(csrf)}"><button class="saas-button" type="submit">Publish safe drafts</button></form></div></section></div>"""
    next_review = next(
        (
            document
            for document in documents
            if document.status == "draft" and not document.language_verified
        ),
        None,
    )
    next_review_html = (
        f'<section class="saas-next-action"><div><div class="saas-eyebrow">{escape(translate("knowledge.next_review"))}</div>'
        f"<h2>{escape(_knowledge_list_question(next_review))}</h2></div>{primary_action(f'{_tenant_root(tenant_id)}/knowledge/documents/{next_review.id}', translate('knowledge.review_document'))}</section>"
        if next_review
        else ""
    )
    rows = "".join(
        f'<tr><td><strong>{escape(_knowledge_list_question(document))}</strong><br><span class="saas-muted">{escape(_knowledge_visible_metadata(document, document.category or translate("knowledge.uncategorized")))}</span></td>'
        f"<td>{escape(_knowledge_visible_metadata(document, document.brand_id))} / {escape(_knowledge_visible_metadata(document, document.platform or translate('knowledge.detail.all_platforms')))}</td>"
        f"<td>{escape(document.detected_language)} / {escape(document.language_detection_status)}</td><td>{status_badge(document.status)}</td>"
        f"<td>{escape(translate('knowledge.language_confirmed') if document.language_verified else translate('knowledge.language_pending'))}</td>"
        f'<td><a href="{_tenant_root(tenant_id)}/knowledge/documents/{document.id}">{escape(translate("knowledge.review_action"))}</a></td></tr>'
        for document in documents
    )
    table = (
        f'<div class="saas-table-wrap"><table class="saas-table"><thead><tr><th>{escape(translate("knowledge.question_category"))}</th><th>Scope</th><th>{escape(translate("common.language"))}</th><th>{escape(translate("knowledge.publication_status"))}</th><th>{escape(translate("common.review"))}</th><th></th></tr></thead><tbody>{rows}</tbody></table></div>'
        if rows
        else empty_state(
            translate("knowledge.empty_title"), translate("knowledge.empty_description")
        )
    )
    safe_request_query = tuple(
        (key, value)
        for key, value in (
            ("status_filter", status_filter if status_filter != "all" else ""),
            ("brand_id", safe_brand_filter),
            ("platform", safe_platform_filter),
            ("category", safe_category_filter),
        )
        if value
    )
    request_location_token = set_request_location(request.url.path, safe_request_query)
    try:
        response = _render_page(
            principal=principal,
            tenant_id=tenant_id,
            title=translate("knowledge.title"),
            description=translate("knowledge.description"),
            body=f'{filter_tabs}{filters}{next_review_html}<div class="saas-grid two">{add_form}{import_form}</div>{batch_forms}{table}',
            active_navigation="knowledge",
            inbox_count=inbox_summary.total,
        )
    finally:
        reset_request_location(request_location_token)
    if not request.cookies.get("reply_admin_csrf"):
        response.set_cookie(
            "reply_admin_csrf", csrf, httponly=False, samesite="lax", secure=_secure_cookie(request)
        )
    response.headers["Cache-Control"] = "no-store"
    return response


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
    try:
        async with get_session_factory()() as session:
            inbox_summary = await _load_inbox_summary(session, principal, tenant_id)
            document = await execute_get_knowledge_document(
                session,
                GetKnowledgeDocumentQuery(
                    required_tenant_id=tenant_id,
                    actor=principal.actor,
                    document_id=document_id,
                ),
            )
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
            audits = list(
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
                ).scalars()
            )
    except KnowledgeApplicationError as exc:
        raise _knowledge_http_error(exc) from exc
    sensitive_content = _knowledge_document_is_sensitive(document)
    visible_question = (
        translate("knowledge.detail.sensitive_redacted") if sensitive_content else document.question
    )
    visible_reply = (
        translate("knowledge.detail.sensitive_redacted") if sensitive_content else document.reply
    )
    checklist = "".join(
        (
            _progress_row(
                bool(document.question.strip()),
                translate("knowledge.detail.question_present"),
            ),
            _progress_row(
                bool(document.reply.strip()),
                translate("knowledge.detail.reply_present"),
            ),
            _progress_row(
                document.language_verified,
                translate("knowledge.detail.language_verified"),
            ),
            _progress_row(
                bool(chunk_count),
                translate(
                    "knowledge.detail.chunks_available",
                    count=int(chunk_count or 0),
                ),
            ),
            _progress_row(
                document.is_official_contact or not has_contact_like(document.reply or ""),
                translate("knowledge.detail.official_contact_reviewed"),
                warning=has_contact_like(document.reply or "") and not document.is_official_contact,
            ),
        )
    )
    audit_rows = "".join(
        f"<tr><td>{format_datetime(audit.created_at)}</td><td>{escape(audit.actor)}</td>"
        f"<td>{escape(audit.action)}</td></tr>"
        for audit in audits
    )
    csrf = _csrf(request)
    action_root = f"{_tenant_root(tenant_id)}/knowledge/documents/{document.id}"
    if document.status == "published":
        publication_action = f"""<form method="post" action="{action_root}/unpublish"><input type="hidden" name="csrf_token" value="{escape(csrf)}"><button class="saas-button" type="submit">Unpublish</button></form>"""
        classification_action = ""
        confirmation_action = ""
        delete_action = ""
    else:
        publication_action = f"""<form method="post" action="{action_root}/publish"><input type="hidden" name="csrf_token" value="{escape(csrf)}"><button class="saas-button primary" type="submit">Publish</button></form>"""
        classification_action = f"""<form method="post" action="{action_root}/official-contact"><input type="hidden" name="csrf_token" value="{escape(csrf)}"><input type="hidden" name="target" value="{"false" if document.is_official_contact else "true"}"><button class="saas-button" type="submit">{"Remove official classification" if document.is_official_contact else "Mark official/contact-only"}</button></form>"""
        confirmation_reason = (
            '<label>Confirmation reason<input name="confirmation_reason" maxlength="500" minlength="10" required></label>'
            if document.language_detection_status == "unknown"
            else '<input type="hidden" name="confirmation_reason" value="">'
        )
        confirmation_action = (
            f"""<form class="saas-form" method="post" action="{action_root}/confirm-english"><input type="hidden" name="csrf_token" value="{escape(csrf)}">{confirmation_reason}<button class="saas-button" type="submit">Confirm English</button></form>"""
            if not document.language_verified
            and document.language_detection_status not in {"mixed", "non_english"}
            else ""
        )
        delete_action = f"""<form method="post" action="{action_root}/delete"><input type="hidden" name="csrf_token" value="{escape(csrf)}"><button class="saas-button danger" type="submit">Delete draft</button></form>"""
    body = f"""<div class="saas-grid two" style="margin-top:0">
<section class="saas-card"><div class="saas-card-header"><div><h2>{escape(translate("knowledge.detail.content"))}</h2>
<p>{escape(translate("knowledge.detail.content_description"))}</p></div>{status_badge(document.status)}</div>
<div class="saas-card-body"><h3>{escape(translate("knowledge.detail.question"))}</h3><p>{escape(visible_question)}</p>
<h3>{escape(translate("knowledge.detail.reply_candidate"))}</h3><p>{escape(visible_reply)}</p></div></section>
<section class="saas-card"><div class="saas-card-header"><div><h2>{escape(translate("knowledge.detail.checklist"))}</h2>
<p>{escape(translate("knowledge.detail.checklist_description"))}</p></div></div>
<div class="saas-card-body"><ul class="saas-progress-list">{checklist}</ul></div></section>
</div>
<section class="saas-card"><div class="saas-card-header"><div><h2>Actions</h2></div></div><div class="saas-card-body"><div class="saas-form-row">{confirmation_action}{classification_action}{publication_action}{delete_action}</div></div></section>
<div class="saas-grid two">
<section class="saas-card"><div class="saas-card-header"><div><h2>{escape(translate("knowledge.detail.source_scope"))}</h2></div></div>
<div class="saas-card-body">{definition_list((("Brand", _knowledge_visible_metadata(document, document.brand_id)), ("Platform", _knowledge_visible_metadata(document, document.platform or translate("knowledge.detail.all_platforms"))), (translate("common.category"), _knowledge_visible_metadata(document, document.category or translate("knowledge.uncategorized"))), (translate("knowledge.detail.source_file"), _knowledge_visible_metadata(document, document.source_file or translate("knowledge.manual_entry"))), (translate("knowledge.detail.localizations"), int(localization_count or 0)), (translate("common.updated"), format_datetime(document.updated_at, include_year=True))))}</div></section>
<section class="saas-card"><div class="saas-card-header"><div><h2>{escape(translate("knowledge.detail.audit_history"))}</h2></div></div>
<div class="saas-card-body">{f'<table class="saas-table"><tbody>{audit_rows}</tbody></table>' if audit_rows else f'<p class="saas-muted">{escape(translate("knowledge.detail.no_audit"))}</p>'}</div></section>
</div>"""
    response = _render_page(
        principal=principal,
        tenant_id=tenant_id,
        title=translate("knowledge.detail.title"),
        description=translate("knowledge.detail.description"),
        body=body,
        active_navigation="knowledge",
        inbox_count=inbox_summary.total,
        breadcrumbs=(
            (translate("knowledge.title"), f"{_tenant_root(tenant_id)}/knowledge"),
            (translate("common.review"), None),
        ),
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
    category_options = (
        f'<option value="">{escape(translate("audit.all_categories"))}</option>'
        + "".join(
            f'<option value="{escape(value)}"'
            f"{' selected' if value == category else ''}>{escape(value)}</option>"
            for value in categories
        )
    )
    filters = f"""<form class="saas-filter-bar" method="get" aria-label="{escape(translate("audit.title"))}">
<label class="saas-field"><span>{escape(translate("common.category"))}</span><select name="category">{category_options}</select></label>
<button class="saas-button" type="submit">{escape(translate("common.apply_filters"))}</button></form>"""
    rows = "".join(
        f"<tr><td>{format_datetime(audit.created_at, include_year=True)}</td>"
        f"<td>{escape(audit.actor)}</td><td>{escape(audit.category)}</td>"
        f"<td><strong>{escape(audit.action)}</strong><br>"
        f'<span class="saas-muted">{escape(audit.subject_type)} · '
        f"{escape(audit.subject_id)}</span></td>"
        f'<td><a href="{_tenant_root(tenant_id)}/audit/{audit.id}">{escape(translate("audit.view"))}</a></td></tr>'
        for audit in audits
    )
    table = (
        '<div class="saas-table-wrap"><table class="saas-table"><thead><tr>'
        f"<th>{escape(translate('common.time'))}</th><th>Actor</th>"
        f"<th>{escape(translate('common.category'))}</th>"
        f"<th>{escape(translate('audit.action_resource'))}</th><th></th>"
        f"</tr></thead><tbody>{rows}</tbody></table></div>"
        if rows
        else empty_state(
            translate("audit.empty_title"),
            translate("audit.empty_description"),
        )
    )
    return _render_page(
        principal=principal,
        tenant_id=tenant_id,
        title=translate("audit.title"),
        description=translate("audit.description"),
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
    safe_audit_detail = _audit_detail_for_display(audit)
    body = f"""<div class="saas-grid two" style="margin-top:0">
<section class="saas-card"><div class="saas-card-header"><div><h2>{escape(translate("audit.detail.facts"))}</h2>
<p>{escape(translate("audit.detail.facts_description"))}</p></div></div><div class="saas-card-body">
{definition_list((("Audit ID", audit.id), (translate("common.time"), format_datetime(audit.created_at, include_year=True)), ("Actor", audit.actor), (translate("common.category"), audit.category), (translate("common.action"), audit.action), (translate("audit.detail.resource_type"), audit.subject_type), (translate("common.resource") + " ID", audit.subject_id)))}</div></section>
<section class="saas-card"><div class="saas-card-header"><div><h2>{escape(translate("audit.detail.structured"))}</h2>
<p>{escape(translate("audit.detail.structured_description"))}</p></div></div><div class="saas-card-body">
{safe_json_details(safe_audit_detail, summary=translate("audit.detail.expand"))}</div></section></div>"""
    return _render_page(
        principal=principal,
        tenant_id=tenant_id,
        title=translate("audit.detail.title"),
        description=f"{audit.action} · {audit.subject_type}",
        body=body,
        active_navigation="audit",
        inbox_count=inbox_summary.total,
        breadcrumbs=(
            (translate("audit.title"), f"{_tenant_root(tenant_id)}/audit"),
            (str(audit.id), None),
        ),
    )


_HISTORICAL_KNOWLEDGE_AUDIT_SENSITIVE_KEYS = frozenset(
    {
        "question",
        "reply",
        "protected_values",
        "confirmation_reason",
        "source_file",
        "brand",
        "platform",
        "category",
    }
)


def _audit_detail_for_display(audit: models.AuditLog) -> dict:
    detail = dict(audit.detail or {})
    is_knowledge_audit = (
        audit.subject_type in {"knowledge_document", "knowledge_import_batch"}
        or "KNOWLEDGE" in audit.action
    )
    if not is_knowledge_audit:
        return detail
    for key in _HISTORICAL_KNOWLEDGE_AUDIT_SENSITIVE_KEYS:
        if key not in detail or detail[key] in (None, "", [], {}):
            continue
        serialized_value = repr(detail.pop(key))
        detail.setdefault(
            f"{key}_hash",
            hashlib.sha256(serialized_value.encode()).hexdigest(),
        )
        detail.setdefault(f"{key}_length", len(serialized_value))
    return detail


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
        f"<td><strong>{escape(row.contact.display_name or translate('common.anonymous_contact'))}</strong><br>"
        f'<span class="saas-muted">{escape(row.account.name)} · '
        f"{escape(row.conversation.channel_type)}</span></td>"
        f"<td>{status_badge(row.job.status)}</td>"
        f"<td>{status_badge(row.decision.action) if row.decision else '—'}</td>"
        f"<td>g{escape(row.job.decision_generation if row.job.decision_generation is not None else '—')}</td>"
        f'<td><a href="{_tenant_root(tenant_id)}/journeys/{row.job.id}">{escape(translate("journey.view"))}</a></td></tr>'
        for row in journey_rows
    )
    table = (
        '<div class="saas-table-wrap"><table class="saas-table"><thead><tr>'
        f"<th>{escape(translate('journey.start_time'))}</th>"
        f"<th>{escape(translate('journey.conversation'))}</th><th>Job</th>"
        f"<th>{escape(translate('journey.decision'))}</th>"
        "<th>Generation</th><th></th>"
        f"</tr></thead><tbody>{rows}</tbody></table></div>"
        if rows
        else empty_state(
            translate("journey.empty_title"),
            translate("journey.empty_description"),
        )
    )
    return _render_page(
        principal=principal,
        tenant_id=tenant_id,
        title=translate("journey.title"),
        description=translate("journey.description"),
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
            translate("journey.raw_received") if raw_event else translate("journey.raw_missing"),
            raw_event.processing_status if raw_event else translate("journey.historical_trigger"),
            raw_event.received_at if raw_event else job.created_at,
            "success" if raw_event else "neutral",
        ),
        _journey_timeline_item(
            "2",
            translate("journey.message_normalized"),
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
        role_label = (
            translate("journey.review_outbox")
            if decision and outbox.id == decision.review_outbox_id
            else translate("journey.send_outbox")
        )
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
                translate("journey.delivery_attempt"),
                f"{attempt.outcome} · {attempt.error_code or translate('journey.no_error_code')}",
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
        else f'<p class="saas-muted">{escape(translate("journey.no_decision"))}</p>'
    )
    evidence_details = safe_json_details(
        {
            "job_snapshot": job.snapshot,
            "raw_event_context": raw_event.context if raw_event else None,
            "decision_rag_evidence": decision.rag_evidence if decision else None,
        },
        summary=translate("journey.expand_evidence"),
    )
    body = f"""<div class="saas-grid" style="grid-template-columns:minmax(0,1fr) 340px;margin-top:0">
<section class="saas-card"><div class="saas-card-header"><div><h2>{escape(translate("journey.timeline"))}</h2>
<p>{escape(translate("journey.timeline_description"))}</p></div></div>
<div class="saas-card-body"><ol class="saas-timeline">{"".join(timeline_items)}</ol></div></section>
<aside class="saas-card"><div class="saas-card-header"><div><h2>{escape(translate("journey.context"))}</h2></div></div>
<div class="saas-card-body">{definition_list(((translate("common.contact"), contact.display_name or translate("common.anonymous_contact")), (translate("common.account"), account.name), (translate("common.platform"), conversation.platform), (translate("journey.conversation"), conversation.id), ("Generation", job.decision_generation), ("Job ID", job.id)))}</div></aside></div>
<div class="saas-grid two"><section class="saas-card"><div class="saas-card-header"><div><h2>{escape(translate("journey.decision_source"))}</h2></div></div>
<div class="saas-card-body">{decision_details}</div></section>
<section class="saas-card"><div class="saas-card-header"><div><h2>{escape(translate("journey.structured_evidence"))}</h2></div></div>
<div class="saas-card-body">{evidence_details}</div></section></div>"""
    return _render_page(
        principal=principal,
        tenant_id=tenant_id,
        title=translate("journey.detail_title"),
        description=translate("journey.detail_description", job_id=job.id),
        body=body,
        active_navigation="journeys",
        inbox_count=inbox_summary.total,
        breadcrumbs=(
            (translate("journey.title"), f"{_tenant_root(tenant_id)}/journeys"),
            (str(job.id), None),
        ),
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


@router.get("/app/t/{tenant_id}/health", response_class=HTMLResponse)
async def tenant_health(request: Request, tenant_id: str) -> Response:
    principal = await _require_tenant_admin_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    current_time = datetime.now(UTC)
    day_ago = current_time - timedelta(hours=24)
    async with get_session_factory()() as session:
        inbox_summary = await _load_inbox_summary(session, principal, tenant_id)
        health_metrics = await _load_health_metrics(
            session,
            frozenset({tenant_id}),
            current_time,
        )
        outbox_rows = list(
            (
                await session.execute(
                    select(models.OutboxMessage)
                    .where(models.OutboxMessage.tenant_id == tenant_id)
                    .order_by(models.OutboxMessage.created_at.desc())
                    .limit(50)
                )
            ).scalars()
        )
        ingress_rows = list(
            (
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
                            models.RawEvent.tenant_id == tenant_id,
                            and_(
                                models.RawEvent.tenant_id.is_(None),
                                models.NormalizedEvent.tenant_id == tenant_id,
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
        )

    metric_rows = "".join(
        f'<tr data-health="{escape(metric.key)}">'
        f"<td><strong>{escape(metric.label)}</strong></td>"
        f"<td>{status_badge(metric.level)}</td>"
        f"<td>{escape(translate('admin.health.backlog_summary', action_count=metric.action_count, warning_count=metric.warning_count))}</td>"
        f"<td>{escape(_health_age(current_time, metric.oldest_at))}</td>"
        f"<td>{secondary_action(metric.href, translate('admin.common.view'), small=True)}</td></tr>"
        for metric in health_metrics
    )
    delivery_rows = (
        "".join(
            f"<tr><td>{escape(format_datetime(outbox.created_at, include_year=True))}</td>"
            f"<td>{status_badge(outbox.status)}</td>"
            f"<td>{escape(outbox.destination_type)}</td>"
            f"<td>{escape(str((outbox.payload or {}).get('text', ''))[:80])}</td>"
            f"<td>{escape(outbox.attempt_count)}</td>"
            f"<td>{escape(outbox.last_error_code or '—')}</td></tr>"
            for outbox in outbox_rows
        )
        or f'<tr><td colspan="6">{escape(translate("admin.health.no_delivery_records"))}</td></tr>'
    )
    ingress_table_rows = (
        "".join(
            f"<tr><td>{escape(source)}</td><td>{status_badge(processing_status)}</td>"
            f"<td>{escape(count)}</td><td>{escape(format_datetime(last_at, include_year=True))}</td></tr>"
            for source, processing_status, count, last_at in ingress_rows
        )
        or f'<tr><td colspan="4">{escape(translate("admin.health.no_ingress"))}</td></tr>'
    )
    body = f"""<section class="saas-card"><div class="saas-card-header"><div>
<h2>{escape(translate("admin.health.core_pipeline"))}</h2></div></div>
<div class="saas-table-wrap"><table class="saas-table"><tbody>{metric_rows}</tbody></table></div></section>
<section class="saas-card"><div class="saas-card-header"><div><h2>Outbox</h2>
<p>{escape(translate("admin.health.outbox_hint"))}</p></div></div>
<div class="saas-table-wrap"><table class="saas-table"><tbody>{delivery_rows}</tbody></table></div></section>
<section class="saas-card" id="ingress"><div class="saas-card-header"><div>
<h2>{escape(translate("admin.health.ingress"))}</h2></div></div>
<div class="saas-table-wrap"><table class="saas-table"><tbody>{ingress_table_rows}</tbody></table></div></section>"""
    return _render_page(
        principal=principal,
        tenant_id=tenant_id,
        title=translate("admin.health.title"),
        description=translate("admin.health.description"),
        body=body,
        active_navigation="health",
        inbox_count=inbox_summary.total,
    )


@router.get("/app/t/{tenant_id}/settings", response_class=HTMLResponse)
async def tenant_settings(request: Request, tenant_id: str) -> Response:
    principal = await _require_tenant_admin_principal(request, tenant_id)
    if isinstance(principal, Response):
        return principal
    async with get_session_factory()() as session:
        inbox_summary = await _load_inbox_summary(session, principal, tenant_id)
        account_count = await session.scalar(
            select(func.count()).where(models.PlatformAccount.tenant_id == tenant_id)
        )
    tenant_root = _tenant_root(tenant_id)
    canonical_links = "".join(
        (
            secondary_action(
                f"{_agent_root(tenant_id, DEFAULT_TENANT_ID)}/instructions",
                translate("settings.business_instructions"),
            ),
            secondary_action(
                f"{tenant_root}/knowledge",
                translate("settings.knowledge_governance"),
            ),
            secondary_action(
                f"{tenant_root}/channels",
                translate("agent.overview.channel_accounts"),
            ),
            secondary_action(
                f"{tenant_root}/channels/feishu/handoff",
                translate("nav.feishu_handoff"),
            ),
            secondary_action(f"{tenant_root}/health", translate("nav.system_health")),
            secondary_action(f"{tenant_root}/audit", translate("nav.audit_center")),
            secondary_action(f"{tenant_root}/journeys", translate("nav.processing_journey")),
        )
    )
    body = f"""<section class="saas-card"><div class="saas-card-header"><div><h2>{escape(translate("settings.identity"))}</h2>
<p>{escape(translate("settings.identity_description"))}</p></div></div><div class="saas-card-body">
{definition_list((("Tenant ID", tenant_id), (translate("settings.workspace_user"), principal.username), (translate("settings.account_count"), int(account_count or 0)), (translate("settings.default_mode"), "BOT_DRAFT_ONLY")))}</div></section>
<div class="saas-section-title"><div><h2>{escape(translate("settings.configuration"))}</h2>
<p>{escape(translate("settings.configuration_description"))}</p></div></div>
<div class="saas-action-row">{canonical_links}</div>
<section class="saas-card"><div class="saas-card-header"><div><h2>{escape(translate("settings.preferences"))}</h2></div></div>
<div class="saas-card-body">{definition_list(((translate("settings.language"), translate("settings.not_persisted")), (translate("settings.timezone"), translate("settings.not_persisted")), (translate("settings.notifications"), translate("settings.not_persisted"))))}
<p class="saas-muted">{escape(translate("settings.preferences_deferred"))}</p></div></section>"""
    return _render_page(
        principal=principal,
        tenant_id=tenant_id,
        title=translate("settings.title"),
        description=translate("settings.description"),
        body=body,
        active_navigation="settings",
        inbox_count=inbox_summary.total,
    )


async def _require_system_principal(request: Request) -> Principal | Response:
    principal = await _require_web_principal(request)
    if isinstance(principal, Response):
        return principal
    principal.require_superadmin()
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


def _redact_system_audit_detail(value: object) -> object:
    if isinstance(value, dict):
        redacted_detail: dict[str, object] = {}
        for key, item in value.items():
            normalized_key = str(key).casefold()
            is_secret_key = any(
                secret_part in normalized_key for secret_part in _SYSTEM_AUDIT_SECRET_KEY_PARTS
            )
            if is_secret_key or normalized_key not in _SYSTEM_AUDIT_SAFE_DETAIL_KEYS:
                redacted_detail[str(key)] = "[REDACTED]"
            else:
                redacted_detail[str(key)] = _redact_system_audit_detail(item)
        return redacted_detail
    if isinstance(value, list):
        return [_redact_system_audit_detail(item) for item in value]
    return value


def _security_configuration_complete() -> bool:
    settings = get_settings()
    return all(
        (
            bool(settings.admin_username.strip()),
            bool(settings.admin_password.get_secret_value()),
            len(settings.admin_session_secret.get_secret_value()) >= 32,
            DEFAULT_TENANT_ID in settings.allowed_admin_tenants,
            bool(settings.platform_secret_key_list),
        )
    )


def _canonical_system_audit_category(category: str) -> str:
    return _SYSTEM_AUDIT_CATEGORY_ALIASES.get(category, category)


async def _load_system_overview() -> tuple[dict[str, int | str | bool], list[models.AuditLog]]:
    current_time = datetime.now(UTC)
    query_categories = SYSTEM_AUDIT_CATEGORIES | frozenset(_SYSTEM_AUDIT_CATEGORY_ALIASES)
    async with get_session_factory()() as session:
        active_user_count = int(
            await session.scalar(
                select(func.count())
                .select_from(models.AdminUser)
                .where(
                    models.AdminUser.tenant_id == DEFAULT_TENANT_ID,
                    models.AdminUser.role == "USER",
                    models.AdminUser.status == "active",
                )
            )
            or 0
        )
        disabled_user_count = int(
            await session.scalar(
                select(func.count())
                .select_from(models.AdminUser)
                .where(
                    models.AdminUser.tenant_id == DEFAULT_TENANT_ID,
                    models.AdminUser.status == "disabled",
                )
            )
            or 0
        )
        active_session_count = int(
            await session.scalar(
                select(func.count())
                .select_from(models.AdminSession)
                .where(models.AdminSession.expires_at > current_time)
            )
            or 0
        )
        expired_session_count = int(
            await session.scalar(
                select(func.count())
                .select_from(models.AdminSession)
                .where(models.AdminSession.expires_at <= current_time)
            )
            or 0
        )
        recent_audits = list(
            (
                await session.execute(
                    select(models.AuditLog)
                    .where(
                        models.AuditLog.category.in_(query_categories),
                        models.AuditLog.subject_type.in_(_SYSTEM_AUDIT_SUBJECT_TYPES),
                    )
                    .order_by(models.AuditLog.created_at.desc())
                    .limit(12)
                )
            ).scalars()
        )

    global_kill_switch_state = "unavailable"
    redis = aioredis.from_url(get_settings().redis_url)
    try:
        global_kill_switch_state = (
            "enabled"
            if await redis.exists(f"killswitch:global:{DEFAULT_TENANT_ID}")
            else "disabled"
        )
    except Exception:  # noqa: BLE001 - overview remains available during Redis incidents
        logger.exception("system overview could not read the global kill switch")
    finally:
        await redis.aclose()

    summary: dict[str, int | str | bool] = {
        "active_user_count": active_user_count,
        "disabled_user_count": disabled_user_count,
        "active_session_count": active_session_count,
        "expired_session_count": expired_session_count,
        "global_kill_switch_state": global_kill_switch_state,
        "security_configuration_complete": _security_configuration_complete(),
    }
    return summary, recent_audits


@router.get("/admin/system/overview", response_class=HTMLResponse)
async def system_overview(request: Request) -> Response:
    principal = await _require_system_principal(request)
    if isinstance(principal, Response):
        return principal
    summary, recent_audits = await _load_system_overview()
    audit_rows = "".join(
        f"<tr><td>{format_datetime(audit.created_at)}</td>"
        f"<td>{escape(_canonical_system_audit_category(audit.category))}</td>"
        f"<td>{escape(audit.actor)}</td><td>{escape(audit.action)}</td></tr>"
        for audit in recent_audits
    )
    body = f"""<section class="saas-alert warning">
{escape(translate("system.overview.security_notice"))}</section>
<div class="saas-grid three">
{metric_card(summary["active_user_count"], translate("system.overview.active_users"))}
{metric_card(summary["disabled_user_count"], translate("system.overview.disabled_users"))}
{metric_card(summary["active_session_count"], translate("system.overview.active_sessions"))}</div>
<div class="saas-grid three">
{metric_card(summary["expired_session_count"], translate("system.overview.expired_sessions"))}
{metric_card(summary["global_kill_switch_state"], translate("system.overview.global_kill_switch"))}
{metric_card(translate("common.complete") if summary["security_configuration_complete"] else translate("common.incomplete"), translate("system.overview.security_configuration"))}</div>
<div class="saas-section-title"><div><h2>{escape(translate("system.overview.operations"))}</h2>
<p>{escape(translate("system.overview.operations_description"))}</p></div></div>
<div class="saas-action-row">{secondary_action("/admin/system/users", translate("system.overview.access"))}{secondary_action("/admin/system/safety", translate("system.overview.safety"))}{secondary_action("/admin/system/audit", translate("home.view_all"))}</div>
<div class="saas-section-title"><div><h2>{escape(translate("system.overview.recent_activity"))}</h2></div>
{secondary_action("/admin/system/audit", translate("home.view_all"), small=True)}</div>
{f'<div class="saas-table-wrap"><table class="saas-table"><tbody>{audit_rows}</tbody></table></div>' if audit_rows else f'<p class="saas-muted">{escape(translate("system.overview.no_audit"))}</p>'}"""
    return _render_system_page(
        principal=principal,
        title=translate("system.overview.title"),
        description=translate("system.overview.description"),
        body=body,
        active_navigation="system-overview",
    )


@router.get("/admin/system/audit", response_class=HTMLResponse)
async def system_audit(request: Request, category: str = "") -> Response:
    principal = await _require_system_principal(request)
    if isinstance(principal, Response):
        return principal
    if category and category not in SYSTEM_AUDIT_CATEGORIES:
        raise HTTPException(status_code=422, detail="invalid_system_audit_category")
    query_categories = SYSTEM_AUDIT_CATEGORIES | frozenset(_SYSTEM_AUDIT_CATEGORY_ALIASES)
    async with get_session_factory()() as session:
        statement = select(models.AuditLog).where(
            models.AuditLog.category.in_(query_categories),
            models.AuditLog.subject_type.in_(_SYSTEM_AUDIT_SUBJECT_TYPES),
        )
        if category:
            matching_categories = {category} | {
                source
                for source, target in _SYSTEM_AUDIT_CATEGORY_ALIASES.items()
                if target == category
            }
            statement = statement.where(models.AuditLog.category.in_(matching_categories))
        audits = list(
            (
                await session.execute(
                    statement.order_by(models.AuditLog.created_at.desc()).limit(500)
                )
            ).scalars()
        )
    category_options = (
        f'<option value="">{escape(translate("system.audit.all_categories"))}</option>'
        + "".join(
            f'<option value="{escape(value)}"'
            f"{' selected' if value == category else ''}>{escape(value)}</option>"
            for value in sorted(SYSTEM_AUDIT_CATEGORIES)
        )
    )
    rows = "".join(
        f"<tr><td>{format_datetime(audit.created_at, include_year=True)}</td>"
        f"<td>{escape(audit.actor)}</td>"
        f"<td>{escape(_canonical_system_audit_category(audit.category))}</td>"
        f"<td>{escape(audit.action)}</td>"
        f"<td>{escape(audit.subject_type)} · {escape(audit.subject_id)}"
        f"{safe_json_details(_redact_system_audit_detail(audit.detail or {}), summary=translate('common.structured_details'))}</td></tr>"
        for audit in audits
    )
    body = f"""<form class="saas-filter-bar" method="get" aria-label="{escape(translate("system.audit.title"))}"><label class="saas-field">
<span>{escape(translate("common.category"))}</span><select name="category">{category_options}</select></label>
<button class="saas-button" type="submit">{escape(translate("common.apply_filters"))}</button></form>
<div class="saas-table-wrap"><table class="saas-table"><thead><tr>
<th>{escape(translate("common.time"))}</th><th>Actor</th><th>{escape(translate("common.category"))}</th><th>{escape(translate("common.action"))}</th><th>{escape(translate("common.resource"))}</th>
</tr></thead><tbody>{rows}</tbody></table></div>"""
    return _render_system_page(
        principal=principal,
        title=translate("system.audit.title"),
        description=translate("system.audit.description"),
        body=body,
        active_navigation="system-audit",
    )


@router.get("/help", response_class=HTMLResponse)
async def product_help(request: Request) -> Response:
    principal = await _require_web_principal(request)
    if isinstance(principal, Response):
        return principal
    body = f"""<div class="saas-grid two" style="margin-top:0">
<section class="saas-card"><div class="saas-card-header"><div><h2>{escape(translate("help.get_started"))}</h2></div></div>
<div class="saas-card-body"><ol class="saas-progress-list">
<li class="saas-progress-item"><span class="saas-progress-icon done">1</span><span>{escape(translate("help.step.select"))}</span></li>
<li class="saas-progress-item"><span class="saas-progress-icon done">2</span><span>{escape(translate("help.step.agents"))}</span></li>
<li class="saas-progress-item"><span class="saas-progress-icon done">3</span><span>{escape(translate("help.step.inbox"))}</span></li>
</ol></div></section>
<section class="saas-card"><div class="saas-card-header"><div><h2>{escape(translate("help.safety"))}</h2></div></div>
<div class="saas-card-body"><p>{escape(translate("help.safety_description"))}</p>
<p>{escape(translate("help.read_only_description"))}</p></div></section></div>"""
    return HTMLResponse(
        render_saas_page(
            principal=principal,
            title=translate("help.title"),
            description=translate("help.description"),
            body=body,
            active_navigation="",
            tenant_id=None,
        )
    )
