import uuid
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse

from social_reply.application.account_management.admin import (
    _form,
    _require_csrf,
    _web_principal,
    tenant_id_or_default,
)
from social_reply.application.account_management.auth import Principal
from social_reply.application.account_management.feishu_handoff_service import (
    FeishuHandoffConflict,
    FeishuHandoffError,
    FeishuHandoffNotFound,
    save_feishu_handoff_config,
    send_feishu_handoff_test_card,
    set_feishu_handoff_operator_status,
    upsert_feishu_handoff_operator,
)
from social_reply.application.account_management.ui_i18n import translate
from social_reply.connectors.registry import get_platform_sender

router = APIRouter(prefix="/admin", tags=["admin-feishu-handoff"])


def _redirect(tenant_id: str, notice: str | None = None) -> RedirectResponse:
    target = f"/app/t/{tenant_id}/channels/feishu/handoff"
    if notice:
        target = f"{target}?{urlencode({'tenant_id': tenant_id, 'notice': notice})}"
    return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)


def _service_http_error(exc: FeishuHandoffError) -> HTTPException:
    if isinstance(exc, FeishuHandoffNotFound):
        return HTTPException(status_code=404, detail=exc.code)
    if isinstance(exc, FeishuHandoffConflict):
        return HTTPException(status_code=409, detail=exc.code)
    return HTTPException(status_code=422, detail=exc.code)


async def _admin_form_context(
    request: Request,
) -> tuple[Principal, dict[str, str], str] | Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    tenant_id = tenant_id_or_default(principal, form.get("tenant_id") or "")
    return principal, form, tenant_id


@router.get("/integrations/feishu/handoff")
@router.get("/feishu-handoff")
async def feishu_handoff_page(request: Request) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    tenant_id = tenant_id_or_default(
        principal,
        request.query_params.get("tenant_id") or "",
    )
    return _redirect(tenant_id)


@router.post("/feishu-handoff/config")
async def save_feishu_handoff_config_compatibility(request: Request) -> Response:
    context = await _admin_form_context(request)
    if isinstance(context, Response):
        return context
    principal, form, tenant_id = context
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
            principal=principal,
        )
    except FeishuHandoffError as exc:
        raise _service_http_error(exc) from exc
    return _redirect(tenant_id, "config_saved")


@router.post("/feishu-handoff/operators")
async def save_feishu_handoff_operator_compatibility(request: Request) -> Response:
    context = await _admin_form_context(request)
    if isinstance(context, Response):
        return context
    principal, form, tenant_id = context
    try:
        employee_id = uuid.UUID(form["admin_user_id"]) if form.get("admin_user_id") else None
        previous_id = (
            uuid.UUID(form["expected_admin_user_id"])
            if form.get("expected_admin_user_id")
            else None
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid_feishu_operator_staff_id") from exc
    try:
        await upsert_feishu_handoff_operator(
            tenant_id=tenant_id,
            actor=principal.actor,
            operator_open_id=form.get("operator_open_id") or "",
            display_name=form.get("display_name") or "",
            can_claim=form.get("can_claim") == "true",
            can_resolve=form.get("can_resolve") == "true",
            admin_user_id=employee_id,
            expected_admin_user_id=previous_id,
            confirm_rebind=form.get("confirm_rebind") == "true",
            principal=principal,
        )
    except FeishuHandoffError as exc:
        raise _service_http_error(exc) from exc
    return _redirect(tenant_id, "operator_saved")


@router.post("/feishu-handoff/operators/{operator_id}/toggle")
async def toggle_feishu_handoff_operator_compatibility(
    request: Request,
    operator_id: uuid.UUID,
) -> Response:
    context = await _admin_form_context(request)
    if isinstance(context, Response):
        return context
    principal, form, tenant_id = context
    enabled_value = form.get("enabled")
    if enabled_value not in {"true", "false"}:
        raise HTTPException(status_code=422, detail="enabled_target_required")
    try:
        await set_feishu_handoff_operator_status(
            tenant_id=tenant_id,
            actor=principal.actor,
            operator_id=operator_id,
            enabled=enabled_value == "true",
            principal=principal,
        )
    except FeishuHandoffError as exc:
        raise _service_http_error(exc) from exc
    return _redirect(tenant_id, "operator_updated")


@router.post("/feishu-handoff/test")
async def send_feishu_handoff_test_card_compatibility(request: Request) -> Response:
    context = await _admin_form_context(request)
    if isinstance(context, Response):
        return context
    principal, _form_values, tenant_id = context
    outcome = await send_feishu_handoff_test_card(
        tenant_id=tenant_id,
        actor=principal.actor,
        title=translate("admin.handoff.test_card_title"),
        content=(
            translate("admin.handoff.test_card_connected", tenant_id=tenant_id)
            + translate("admin.handoff.test_card_safe")
        ),
        sender_factory=get_platform_sender,
        principal=principal,
    )
    return _redirect(tenant_id, outcome)
