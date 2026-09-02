# ruff: noqa: E501

import html
import secrets
import uuid

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from social_reply.application.account_management.admin import (
    _csrf,
    _form,
    _page,
    _require_csrf,
    _secure_cookie,
    _web_principal,
)
from social_reply.application.account_management.system_user_management import (
    SystemUserActor,
    SystemUserAuthenticationError,
    SystemUserConflictError,
    SystemUserManagementError,
    SystemUserNotFoundError,
    SystemUserValidationError,
    create_system_user,
    force_system_user_password_reset,
    revoke_system_user_sessions,
    set_system_user_status,
)
from social_reply.application.account_management.ui_i18n import translate
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.shared.config import DEFAULT_TENANT_ID

router = APIRouter(prefix="/admin", tags=["admin-users"])
_CSRF_COOKIE = "reply_admin_csrf"


def _field(
    name: str,
    label: str,
    *,
    input_type: str = "text",
    autocomplete: str = "",
) -> str:
    field_id = f"f-user-{name}-{secrets.token_hex(3)}"
    autocomplete_attribute = (
        f' autocomplete="{html.escape(autocomplete, quote=True)}"' if autocomplete else ""
    )
    return (
        f'<label for="{field_id}">{html.escape(label)}</label>'
        f'<input id="{field_id}" name="{name}" type="{input_type}"'
        f"{autocomplete_attribute} required>"
    )


async def _superadmin(request: Request):
    principal = await _web_principal(request, require_admin=False)
    if isinstance(principal, Response):
        return principal
    principal.require_superadmin()
    return principal


def _actor(principal) -> SystemUserActor:
    return SystemUserActor(actor=principal.actor, session_id=principal.session_id)


def _management_http_error(exc: SystemUserManagementError) -> HTTPException:
    if isinstance(exc, SystemUserAuthenticationError):
        return HTTPException(status_code=401, detail=exc.code)
    if isinstance(exc, SystemUserValidationError):
        return HTTPException(status_code=422, detail=exc.code)
    if isinstance(exc, SystemUserConflictError):
        return HTTPException(status_code=409, detail=exc.code)
    if isinstance(exc, SystemUserNotFoundError):
        return HTTPException(status_code=404, detail=exc.code)
    return HTTPException(status_code=500, detail="system_user_management_failed")


def _reauthentication_field() -> str:
    return _field(
        "bootstrap_password",
        translate("admin.users.bootstrap_password"),
        input_type="password",
        autocomplete="current-password",
    )


def _csrf_field(csrf: str) -> str:
    return f'<input type="hidden" name="csrf_token" value="{html.escape(csrf, quote=True)}">'


def _user_actions(user: models.AdminUser, csrf: str) -> str:
    target_status = "active" if user.status == "disabled" else "disabled"
    status_form = f"""<form method="post" action="/admin/system/users/{user.id}/status">
{_csrf_field(csrf)}<input type="hidden" name="status" value="{target_status}">
{_reauthentication_field()}<button>{html.escape(translate("admin.users.set_status", status=target_status))}</button></form>"""
    reset_form = f"""<form method="post" action="/admin/system/users/{user.id}/password-reset">
{_csrf_field(csrf)}{_field("initial_password", translate("admin.users.initial_password"), input_type="password", autocomplete="new-password")}
{_reauthentication_field()}<button>{html.escape(translate("admin.users.force_password_reset"))}</button></form>"""
    revoke_form = f"""<form method="post" action="/admin/system/users/{user.id}/sessions/revoke">
{_csrf_field(csrf)}{_reauthentication_field()}<button>{html.escape(translate("admin.users.revoke_sessions"))}</button></form>"""
    return f'<details><summary>{html.escape(translate("admin.users.manage"))}</summary>{status_form}{reset_form}{revoke_form}</details>'


@router.get("/users", response_class=HTMLResponse)
async def legacy_users_page(request: Request) -> Response:
    principal = await _superadmin(request)
    if isinstance(principal, Response):
        return principal
    return RedirectResponse("/admin/system/users", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/system/users", response_class=HTMLResponse)
async def users_page(request: Request, notice: str = "") -> Response:
    principal = await _superadmin(request)
    if isinstance(principal, Response):
        return principal
    async with get_session_factory()() as session:
        users = list(
            (
                await session.execute(
                    select(models.AdminUser)
                    .where(models.AdminUser.tenant_id == DEFAULT_TENANT_ID)
                    .order_by(models.AdminUser.created_at, models.AdminUser.username)
                )
            ).scalars()
        )

    csrf = _csrf(request)
    role_labels = {
        "USER": translate("admin.users.role.user"),
    }
    rows = (
        "".join(
            f"<tr><td>{html.escape(user.username)}</td>"
            f"<td>{html.escape(role_labels.get(user.role, user.role))}</td>"
            f"<td>{html.escape(user.status)}</td>"
            f"<td>{html.escape(translate('admin.users.must_change_password') if user.must_change_password else translate('admin.users.normal'))}</td>"
            f"<td class='muted'>{user.created_at:%Y-%m-%d %H:%M}</td>"
            f"<td>{_user_actions(user, csrf)}</td></tr>"
            for user in users
        )
        or f"<tr><td colspan='6' class='muted'>{translate('admin.users.empty')}</td></tr>"
    )
    create_form = f"""<section class="card"><h2>{translate("admin.users.create_title")}</h2>
<p class="hint">{translate("admin.users.default_tenant_hint")}</p>
<form method="post" action="/admin/system/users">{_csrf_field(csrf)}
{_field("username", translate("admin.users.username"), autocomplete="username")}
{_field("initial_password", translate("admin.users.initial_password"), input_type="password", autocomplete="new-password")}
{_reauthentication_field()}
<button class="btn-block">{translate("admin.users.create")}</button></form></section>"""
    banner = (
        f'<div class="banner ok">{html.escape(translate("admin.users.operation_completed"))}</div>'
        if notice
        else ""
    )
    page_title = translate("admin.users.title")
    body = f"""<h1>{page_title}</h1><p class="lede">{translate("admin.users.description")}</p>{banner}
<section class="card"><p><strong>Tenant:</strong> <code>{DEFAULT_TENANT_ID}</code></p></section>
{create_form}<section class="card"><h2>{translate("admin.users.list_title")}</h2><div class="tablewrap"><table>
<thead><tr><th>{translate("admin.users.username")}</th><th>{translate("admin.users.role")}</th>
<th>{translate("admin.users.account_status")}</th><th>{translate("admin.users.password_status")}</th>
<th>{translate("admin.users.created_at")}</th><th>{translate("admin.common.operation")}</th></tr></thead>
<tbody>{rows}</tbody></table></div></section>"""
    response = HTMLResponse(
        _page(page_title, body, active="users", show_users=True, principal=principal)
    )
    if not request.cookies.get(_CSRF_COOKIE):
        response.set_cookie(
            _CSRF_COOKIE,
            csrf,
            httponly=False,
            samesite="lax",
            secure=_secure_cookie(request),
        )
    return response


async def _management_form(request: Request):
    principal = await _superadmin(request)
    if isinstance(principal, Response):
        return principal, None
    form = await _form(request)
    _require_csrf(request, form)
    return principal, form


def _redirect(notice: str) -> RedirectResponse:
    return RedirectResponse(
        f"/admin/system/users?notice={notice}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/system/users")
@router.post("/users")
async def create_user(request: Request) -> Response:
    principal, form = await _management_form(request)
    if isinstance(principal, Response):
        return principal
    assert form is not None
    try:
        await create_system_user(
            username=form.get("username", ""),
            initial_password=form.get("initial_password", ""),
            role=form.get("role", "USER"),
            bootstrap_password=form.get("bootstrap_password", ""),
            actor=_actor(principal),
        )
    except SystemUserManagementError as exc:
        raise _management_http_error(exc) from exc
    return _redirect("created")


@router.post("/system/users/{user_id}/status")
async def change_user_status(request: Request, user_id: uuid.UUID) -> Response:
    principal, form = await _management_form(request)
    if isinstance(principal, Response):
        return principal
    assert form is not None
    try:
        await set_system_user_status(
            user_id=user_id,
            user_status=form.get("status", ""),
            bootstrap_password=form.get("bootstrap_password", ""),
            actor=_actor(principal),
        )
    except SystemUserManagementError as exc:
        raise _management_http_error(exc) from exc
    return _redirect("status-updated")


@router.post("/system/users/{user_id}/password-reset")
async def reset_user_password(request: Request, user_id: uuid.UUID) -> Response:
    principal, form = await _management_form(request)
    if isinstance(principal, Response):
        return principal
    assert form is not None
    try:
        await force_system_user_password_reset(
            user_id=user_id,
            initial_password=form.get("initial_password", ""),
            bootstrap_password=form.get("bootstrap_password", ""),
            actor=_actor(principal),
        )
    except SystemUserManagementError as exc:
        raise _management_http_error(exc) from exc
    return _redirect("password-reset")


@router.post("/system/users/{user_id}/sessions/revoke")
async def revoke_user_sessions(request: Request, user_id: uuid.UUID) -> Response:
    principal, form = await _management_form(request)
    if isinstance(principal, Response):
        return principal
    assert form is not None
    try:
        await revoke_system_user_sessions(
            user_id=user_id,
            bootstrap_password=form.get("bootstrap_password", ""),
            actor=_actor(principal),
        )
    except SystemUserManagementError as exc:
        raise _management_http_error(exc) from exc
    return _redirect("sessions-revoked")
