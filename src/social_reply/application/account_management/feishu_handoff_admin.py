import html
import logging
import uuid
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from social_reply.application.account_management.admin import (
    _csrf,
    _ensure_csrf,
    _form,
    _page,
    _pill,
    _require_csrf,
    _web_principal,
    tenant_id_or_default,
)
from social_reply.connectors.errors import PermanentSendError, RetryableSendError
from social_reply.connectors.feishu.client import FeishuClient, FeishuClientError
from social_reply.connectors.registry import get_platform_sender
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.shared.config import get_settings

router = APIRouter(prefix="/admin", tags=["admin-feishu-handoff"])
logger = logging.getLogger(__name__)

_NOTICES = {
    "config_saved": ("ok", "飞书人工通知配置已保存。"),
    "operator_saved": ("ok", "客服权限已保存。"),
    "operator_updated": ("ok", "客服状态已更新。"),
    "test_sent": ("ok", "测试卡片已发送。"),
    "test_ambiguous": (
        "warn",
        "测试卡片结果未知，请先检查群聊，不要立即重复发送。",
    ),
    "test_failed": ("err", "测试卡片发送失败，请检查账号健康和群 ID。"),
}


def _redirect(tenant_id: str, notice: str) -> RedirectResponse:
    query = urlencode({"tenant_id": tenant_id, "notice": notice})
    return RedirectResponse(
        f"/admin/feishu-handoff?{query}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _tenant_picker(principal, selected: str) -> str:
    if principal.tenant_id is not None:
        return f'<p class="hint">Tenant：<code>{html.escape(selected)}</code></p>'
    options = "".join(
        f'<option value="{html.escape(tenant)}"'
        f"{' selected' if tenant == selected else ''}>{html.escape(tenant)}</option>"
        for tenant in sorted(principal.allowed_tenants)
    )
    return (
        '<form method="get" action="/admin/feishu-handoff" class="filters">'
        '<label for="f-handoff-tenant">Tenant</label>'
        f'<select id="f-handoff-tenant" name="tenant_id" required>{options}</select>'
        '<button class="btn-ghost">查看</button></form>'
    )


def _hidden(name: str, value: str) -> str:
    return f'<input type="hidden" name="{html.escape(name)}" value="{html.escape(value)}">'


async def _lock_config(session, tenant_id: str) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"social-reply:feishu-handoff-config:{tenant_id}"},
    )


def _callback_url(account: models.PlatformAccount | None) -> str:
    if account is None or not account.public_id:
        return "—"
    return (
        f"{get_settings().public_base_url.rstrip('/')}/webhooks/feishu/"
        f"{account.public_id}/card-actions"
    )


@router.get("/integrations/feishu/handoff", response_class=HTMLResponse)
@router.get("/feishu-handoff", response_class=HTMLResponse)
async def feishu_handoff_page(request: Request) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    tenant_id = tenant_id_or_default(principal, request.query_params.get("tenant_id") or "")
    csrf = _csrf(request)
    async with get_session_factory()() as session:
        accounts = list(
            (
                await session.execute(
                    select(models.PlatformAccount)
                    .where(
                        models.PlatformAccount.tenant_id == tenant_id,
                        models.PlatformAccount.platform == "feishu",
                        models.PlatformAccount.status == "active",
                    )
                    .order_by(models.PlatformAccount.name)
                )
            ).scalars()
        )
        config = await session.scalar(
            select(models.TenantFeishuHandoffConfig).where(
                models.TenantFeishuHandoffConfig.tenant_id == tenant_id
            )
        )
        operators = list(
            (
                await session.execute(
                    select(models.FeishuHandoffOperator)
                    .where(models.FeishuHandoffOperator.tenant_id == tenant_id)
                    .order_by(models.FeishuHandoffOperator.created_at)
                )
            ).scalars()
        )
        failures = list(
            (
                await session.execute(
                    select(models.HandoffNotificationIntent)
                    .where(
                        models.HandoffNotificationIntent.tenant_id == tenant_id,
                        models.HandoffNotificationIntent.status.in_(
                            ("BLOCKED_CONFIG", "FAILED", "NEEDS_REVIEW")
                        ),
                    )
                    .order_by(models.HandoffNotificationIntent.updated_at.desc())
                    .limit(50)
                )
            ).scalars()
        )
    selected_account = next(
        (
            account
            for account in accounts
            if config is not None and account.id == config.feishu_platform_account_id
        ),
        None,
    )
    account_options = "".join(
        f'<option value="{account.id}"'
        f"{' selected' if selected_account and account.id == selected_account.id else ''}>"
        f"{html.escape(account.name)} · {html.escape(account.public_id or str(account.id))}"
        "</option>"
        for account in accounts
    )
    if not account_options:
        account_options = '<option value="">请先连接一个 Feishu 账号</option>'
    enabled = config is not None and config.enabled
    checked = " checked" if enabled else ""
    chat_id = config.destination_chat_id if config is not None else ""
    callback_url = html.escape(_callback_url(selected_account))
    feature_status = _pill(
        "ENABLED" if get_settings().feishu_handoff_notifications_enabled else "DISABLED"
    )
    config_form = "".join(
        (
            '<section class="card"><h2>通知路由</h2>',
            '<p class="hint">一名 Tenant 第一版使用一个企业自建应用 Bot',
            "和一个客服群。配置变更不会复制已经发送的历史卡片。</p>",
            '<form method="post" action="/admin/feishu-handoff/config">',
            _hidden("csrf_token", csrf),
            _hidden("tenant_id", tenant_id),
            '<label for="f-handoff-account">Feishu 通知账号</label>',
            '<select id="f-handoff-account" name="feishu_platform_account_id" required>',
            account_options,
            "</select>",
            '<label for="f-handoff-chat">客服群 Chat ID</label>',
            '<input id="f-handoff-chat" name="destination_chat_id" ',
            f'value="{html.escape(chat_id)}" maxlength="256" required>',
            '<label class="check"><input type="checkbox" name="enabled" ',
            f'value="true"{checked}>启用人工接管卡片</label>',
            '<button class="btn-block">保存通知配置</button></form>',
            '<div class="tablewrap"><table class="kv"><tbody>',
            "<tr><th>Card Action Callback</th><td><code>",
            callback_url,
            "</code></td></tr><tr><th>配置版本</th><td>",
            str(config.config_version) if config else "—",
            "</td></tr><tr><th>功能开关</th><td>",
            feature_status,
            "</td></tr></tbody></table></div>",
            '<form method="post" action="/admin/feishu-handoff/test">',
            _hidden("csrf_token", csrf),
            _hidden("tenant_id", tenant_id),
            '<button class="btn-ghost">发送测试卡片</button></form></section>',
        )
    )

    account_names = {account.id: account.name for account in accounts}
    operator_rows = (
        "".join(
            "".join(
                (
                    f"<tr><td>{html.escape(operator.display_name or '—')}</td>",
                    "<td>",
                    html.escape(
                        account_names.get(
                            operator.feishu_platform_account_id,
                            str(operator.feishu_platform_account_id),
                        )
                    ),
                    "</td>",
                    f"<td><code>{html.escape(operator.operator_open_id)}</code></td>",
                    f"<td>{'是' if operator.can_claim else '否'}</td>",
                    f"<td>{'是' if operator.can_resolve else '否'}</td>",
                    f"<td>{_pill(operator.status)}</td><td>",
                    '<form class="inline" method="post" action="/admin/feishu-handoff/',
                    f'operators/{operator.id}/toggle">',
                    _hidden("csrf_token", csrf),
                    _hidden("tenant_id", tenant_id),
                    '<button class="btn-sm btn-ghost">',
                    "禁用" if operator.status == "ACTIVE" else "启用",
                    "</button></form></td></tr>",
                )
            )
            for operator in operators
        )
        or '<tr><td colspan="7" class="muted">尚未配置客服</td></tr>'
    )
    operator_card = "".join(
        (
            '<section class="card"><h2>客服权限</h2>',
            '<p class="hint">Open ID 只在当前 Feishu 应用范围内有效。',
            "群成员不会自动获得工单权限。</p>",
            '<form method="post" action="/admin/feishu-handoff/operators">',
            _hidden("csrf_token", csrf),
            _hidden("tenant_id", tenant_id),
            '<label for="f-operator-open-id">Operator Open ID</label>',
            '<input id="f-operator-open-id" name="operator_open_id" ',
            'maxlength="128" required>',
            '<label for="f-operator-name">显示名称</label>',
            '<input id="f-operator-name" name="display_name" maxlength="100">',
            '<label class="check"><input type="checkbox" name="can_claim" ',
            'value="true" checked>允许认领</label>',
            '<label class="check"><input type="checkbox" name="can_resolve" ',
            'value="true" checked>允许解决并恢复 Bot</label>',
            '<button class="btn-block">保存客服权限</button></form>',
            '<div class="tablewrap"><table><thead><tr><th>名称</th><th>App</th>',
            "<th>Open ID</th><th>认领</th><th>解决</th><th>状态</th><th>操作</th>",
            f"</tr></thead><tbody>{operator_rows}</tbody></table></div></section>",
        )
    )

    failure_rows = (
        "".join(
            f"<tr><td><code>{str(intent.public_id)[:8]}</code></td>"
            f"<td>{_pill(intent.status)}</td>"
            f"<td>{html.escape(intent.desired_card_state)}</td>"
            f"<td><code>{html.escape(intent.last_error_code or '—')}</code></td>"
            f"<td>{intent.attempt_count}</td>"
            f'<td><a href="/admin/conversations/{intent.conversation_id}">查看</a></td></tr>'
            for intent in failures
        )
        or '<tr><td colspan="6" class="muted">没有需要处理的通知</td></tr>'
    )
    failure_card = "".join(
        (
            '<section class="card"><h2>通知异常</h2>',
            '<p class="hint">歧义创建不会自动重试，',
            "避免一个工单在客服群出现多张卡片。</p>",
            '<div class="tablewrap"><table><thead><tr><th>通知</th><th>状态</th>',
            "<th>卡片状态</th><th>错误</th><th>尝试</th><th>操作</th></tr></thead>",
            f"<tbody>{failure_rows}</tbody></table></div></section>",
        )
    )

    notice = ""
    notice_key = request.query_params.get("notice") or ""
    if notice_key in _NOTICES:
        tone, message = _NOTICES[notice_key]
        notice = f'<div class="banner {tone}">{html.escape(message)}</div>'
    body = (
        "<h1>飞书人工通知</h1>"
        '<p class="lede">配置 HANDOFF 客服群、操作员权限和通知恢复状态。</p>'
        f"{notice}{_tenant_picker(principal, tenant_id)}"
        f"{config_form}{operator_card}{failure_card}"
    )
    response = HTMLResponse(
        _page("飞书人工通知", body, active="handoff", show_users=principal.is_superadmin)
    )
    return _ensure_csrf(response, request, csrf)


@router.post("/feishu-handoff/config")
async def save_feishu_handoff_config(request: Request) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    tenant_id = tenant_id_or_default(principal, form.get("tenant_id") or "")
    try:
        account_id = uuid.UUID(form.get("feishu_platform_account_id") or "")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid_feishu_account_id") from exc
    chat_id = (form.get("destination_chat_id") or "").strip()
    if not chat_id or len(chat_id) > 256:
        raise HTTPException(status_code=422, detail="invalid_feishu_chat_id")
    enabled = form.get("enabled") == "true"
    async with get_session_factory()() as session:
        account = await session.get(models.PlatformAccount, account_id)
        if (
            account is None
            or account.tenant_id != tenant_id
            or account.platform != "feishu"
            or account.status != "active"
        ):
            raise HTTPException(status_code=404, detail="feishu_account_not_found")
        await _lock_config(session, tenant_id)
        config = await session.scalar(
            select(models.TenantFeishuHandoffConfig)
            .where(models.TenantFeishuHandoffConfig.tenant_id == tenant_id)
            .with_for_update()
        )
        previous = None
        if config is None:
            config = models.TenantFeishuHandoffConfig(
                tenant_id=tenant_id,
                feishu_platform_account_id=account_id,
                destination_chat_id=chat_id,
                enabled=enabled,
                config_version=1,
            )
            session.add(config)
        else:
            previous = {
                "account_id": str(config.feishu_platform_account_id),
                "chat_id": config.destination_chat_id,
                "enabled": config.enabled,
                "config_version": config.config_version,
            }
            changed = (
                config.feishu_platform_account_id != account_id
                or config.destination_chat_id != chat_id
                or config.enabled != enabled
            )
            config.feishu_platform_account_id = account_id
            config.destination_chat_id = chat_id
            config.enabled = enabled
            if changed:
                config.config_version += 1
        await session.flush()
        session.add(
            models.AuditLog(
                tenant_id=tenant_id,
                category="admin_action",
                actor=principal.actor,
                action="SET_FEISHU_HANDOFF_CONFIG",
                subject_type="tenant_feishu_handoff_config",
                subject_id=str(config.id),
                detail={
                    "previous": previous,
                    "account_id": str(account_id),
                    "chat_id": chat_id,
                    "enabled": enabled,
                    "config_version": config.config_version,
                },
            )
        )
        await session.commit()
    return _redirect(tenant_id, "config_saved")


@router.post("/feishu-handoff/operators")
async def save_feishu_handoff_operator(request: Request) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    tenant_id = tenant_id_or_default(principal, form.get("tenant_id") or "")
    open_id = (form.get("operator_open_id") or "").strip()
    display_name = (form.get("display_name") or "").strip()
    can_claim = form.get("can_claim") == "true"
    can_resolve = form.get("can_resolve") == "true"
    if not open_id or len(open_id) > 128:
        raise HTTPException(status_code=422, detail="invalid_operator_open_id")
    if len(display_name) > 100 or not (can_claim or can_resolve):
        raise HTTPException(status_code=422, detail="invalid_operator_permissions")
    async with get_session_factory()() as session:
        config = await session.scalar(
            select(models.TenantFeishuHandoffConfig).where(
                models.TenantFeishuHandoffConfig.tenant_id == tenant_id
            )
        )
        if config is None:
            raise HTTPException(status_code=409, detail="feishu_handoff_config_required")
        operator_id = uuid.uuid4()
        saved_id = (
            await session.execute(
                pg_insert(models.FeishuHandoffOperator)
                .values(
                    id=operator_id,
                    tenant_id=tenant_id,
                    feishu_platform_account_id=config.feishu_platform_account_id,
                    operator_open_id=open_id,
                    display_name=display_name or None,
                    can_claim=can_claim,
                    can_resolve=can_resolve,
                    status="ACTIVE",
                )
                .on_conflict_do_update(
                    index_elements=["feishu_platform_account_id", "operator_open_id"],
                    set_={
                        "display_name": display_name or None,
                        "can_claim": can_claim,
                        "can_resolve": can_resolve,
                        "status": "ACTIVE",
                    },
                )
                .returning(models.FeishuHandoffOperator.id)
            )
        ).scalar_one()
        session.add(
            models.AuditLog(
                tenant_id=tenant_id,
                category="admin_action",
                actor=principal.actor,
                action="SET_FEISHU_HANDOFF_OPERATOR",
                subject_type="feishu_handoff_operator",
                subject_id=str(saved_id),
                detail={
                    "open_id": open_id,
                    "can_claim": can_claim,
                    "can_resolve": can_resolve,
                },
            )
        )
        await session.commit()
    return _redirect(tenant_id, "operator_saved")


@router.post("/feishu-handoff/operators/{operator_id}/toggle")
async def toggle_feishu_handoff_operator(request: Request, operator_id: uuid.UUID) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    tenant_id = tenant_id_or_default(principal, form.get("tenant_id") or "")
    async with get_session_factory()() as session:
        operator = (
            await session.execute(
                select(models.FeishuHandoffOperator)
                .where(
                    models.FeishuHandoffOperator.id == operator_id,
                    models.FeishuHandoffOperator.tenant_id == tenant_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if operator is None:
            raise HTTPException(status_code=404, detail="feishu_handoff_operator_not_found")
        previous = operator.status
        operator.status = "DISABLED" if operator.status == "ACTIVE" else "ACTIVE"
        session.add(
            models.AuditLog(
                tenant_id=tenant_id,
                category="admin_action",
                actor=principal.actor,
                action="SET_FEISHU_HANDOFF_OPERATOR_STATUS",
                subject_type="feishu_handoff_operator",
                subject_id=str(operator.id),
                detail={"from": previous, "to": operator.status},
            )
        )
        await session.commit()
    return _redirect(tenant_id, "operator_updated")


@router.post("/feishu-handoff/test")
async def send_feishu_handoff_test_card(request: Request) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    tenant_id = tenant_id_or_default(principal, form.get("tenant_id") or "")
    async with get_session_factory()() as session:
        config = await session.scalar(
            select(models.TenantFeishuHandoffConfig).where(
                models.TenantFeishuHandoffConfig.tenant_id == tenant_id
            )
        )
        account = (
            await session.get(models.PlatformAccount, config.feishu_platform_account_id)
            if config is not None
            else None
        )
    if (
        not get_settings().feishu_enabled
        or config is None
        or not config.enabled
        or account is None
        or account.tenant_id != tenant_id
        or account.platform != "feishu"
        or str((account.config or {}).get("feishu_health_status") or "") != "READY"
    ):
        return _redirect(tenant_id, "test_failed")
    card = {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "Reply Core 配置测试"},
            "template": "blue",
        },
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        f"Tenant `{tenant_id}` 的人工接管通知已连接。"
                        "此卡片不创建工单，也不包含客户数据。"
                    ),
                }
            ]
        },
    }
    outcome = "test_failed"
    try:
        sender = await get_platform_sender(account.id)
        if not isinstance(sender, FeishuClient):
            raise PermanentSendError("FEISHU_HANDOFF_SENDER_INVALID")
        provider_message_id = await sender.create_interactive_card(
            chat_id=config.destination_chat_id,
            card=card,
            provider_uuid=str(uuid.uuid4()),
        )
    except (httpx.ConnectError, httpx.ConnectTimeout, PermanentSendError, RetryableSendError):
        provider_message_id = None
    except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError, FeishuClientError):
        outcome = "test_ambiguous"
        provider_message_id = None
    except Exception as exc:  # noqa: BLE001 - an unknown create outcome must not be retried blindly
        logger.exception(
            "Unexpected Feishu handoff test-card result tenant_id=%s error_type=%s",
            tenant_id,
            type(exc).__name__,
        )
        outcome = "test_ambiguous"
        provider_message_id = None
    else:
        outcome = "test_sent"
    async with get_session_factory()() as session:
        session.add(
            models.AuditLog(
                tenant_id=tenant_id,
                category="admin_action",
                actor=principal.actor,
                action="SEND_FEISHU_HANDOFF_TEST_CARD",
                subject_type="tenant_feishu_handoff_config",
                subject_id=str(config.id),
                detail={
                    "outcome": outcome,
                    "provider_message_id": provider_message_id,
                },
            )
        )
        await session.commit()
    return _redirect(tenant_id, outcome)
