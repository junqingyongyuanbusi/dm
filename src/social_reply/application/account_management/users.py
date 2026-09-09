# ruff: noqa: E501

import html
import secrets
import uuid

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, or_, select

from social_reply.application.account_management.admin import (
    _csrf,
    _form,
    _require_csrf,
    _secure_cookie,
    _web_principal,
)
from social_reply.application.account_management.member_i18n import (
    member_role_label,
    member_translate,
)
from social_reply.application.account_management.permissions import ROLE_CAPABILITIES
from social_reply.application.account_management.saas_ui import (
    render_saas_page,
    status_badge,
)
from social_reply.application.account_management.system_user_management import (
    WORKSPACE_MEMBER_ROLES,
    SystemUserActor,
    SystemUserAuthenticationError,
    SystemUserConflictError,
    SystemUserManagementError,
    SystemUserNotFoundError,
    SystemUserValidationError,
    create_system_user,
    force_system_user_password_reset,
    revoke_system_user_sessions,
    set_system_user_role,
    set_system_user_status,
)
from social_reply.application.account_management.templating import render_template, trusted_html
from social_reply.application.account_management.ui_i18n import translate
from social_reply.application.account_management.workspace_member_access import (
    WorkspaceMemberAccess,
    get_workspace_member_access,
    parse_member_access_form,
    require_workspace_member_manager,
    set_workspace_member_access,
)
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


async def _user_manager(request: Request):
    principal = await _web_principal(request, require_admin=False)
    if isinstance(principal, Response):
        return principal
    if request.url.path.startswith("/admin/system/"):
        principal.require_superadmin()
    else:
        principal.require_tenant_admin()
        principal.require_tenant(DEFAULT_TENANT_ID)
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


def _users_base_path(request: Request) -> str:
    return "/admin/system/users" if request.url.path.startswith("/admin/system/") else "/admin/users"


def _role_field(selected: str = "AGENT") -> str:
    selected = "AGENT" if selected == "USER" else selected
    options = "".join(
        f'<option value="{role}"{" selected" if role == selected else ""}>{html.escape(member_role_label(role))}</option>'
        for role in WORKSPACE_MEMBER_ROLES
    )
    return f'<label>{translate("admin.users.role")}<select name="role">{options}</select></label>'


def _user_actions(
    user: models.AdminUser,
    csrf: str,
    base_path: str,
    emergency: bool,
    *,
    allow_self_status_role: bool = True,
    include_access: bool = True,
) -> str:
    target_status = "active" if user.status == "disabled" else "disabled"
    emergency_field = (
        f'<label>{html.escape(translate("admin.users.emergency_reason"))}'
        '<input name="emergency_reason" maxlength="500"></label>' if emergency else ""
    )
    status_form = (
        f"""<form method="post" action="{base_path}/{user.id}/status">
{_csrf_field(csrf)}<input type="hidden" name="status" value="{target_status}">
{_reauthentication_field()}{emergency_field}<button class="saas-button" type="submit">{html.escape(translate("admin.users.set_status", status=target_status))}</button></form>"""
        if allow_self_status_role
        else ""
    )
    role_form = (
        f"""<form method="post" action="{base_path}/{user.id}/role">
{_csrf_field(csrf)}{_role_field(user.role)}{_reauthentication_field()}{emergency_field}
<button class="saas-button" type="submit">{translate("admin.users.change_role")}</button></form>"""
        if allow_self_status_role
        else ""
    )
    reset_form = f"""<form method="post" action="{base_path}/{user.id}/password-reset">
{_csrf_field(csrf)}{_field("initial_password", translate("admin.users.initial_password"), input_type="password", autocomplete="new-password")}
{_reauthentication_field()}<button class="saas-button" type="submit">{html.escape(translate("admin.users.force_password_reset"))}</button></form>"""
    revoke_form = f"""<form method="post" action="{base_path}/{user.id}/sessions/revoke">
{_csrf_field(csrf)}{_reauthentication_field()}<button class="saas-button" type="submit">{html.escape(translate("admin.users.revoke_sessions"))}</button></form>"""
    access_link = (
        f'<a class="saas-button" href="/admin/users/{user.id}/access">{html.escape(member_translate("access.title"))}</a>'
        if not emergency and include_access else ""
    )
    return (
        '<details class="saas-danger-action"><summary>'
        f'{html.escape(translate("admin.users.manage"))}</summary>'
        f'<div class="saas-danger-action-body">{access_link}{status_form}{role_form}{reset_form}{revoke_form}</div></details>'
    )


def _member_list_statement():
    member = models.AdminUser
    account = models.PlatformAccount
    grant = models.AccountAccessGrant
    active_grant = select(grant.id).where(
        grant.tenant_id == member.tenant_id,
        grant.platform_account_id == account.id,
        grant.user_id == member.id,
        grant.active.is_(True),
    ).correlate(member, account).exists()
    # EXISTS counts each account once, including accounts both owned and granted.
    account_count = select(func.count(account.id)).where(
        account.tenant_id == member.tenant_id,
        or_(member.role == "WORKSPACE_ADMIN", account.owner_user_id == member.id, active_grant),
    ).correlate(member).scalar_subquery()
    return select(member, account_count.label("account_count")).where(
        member.tenant_id == DEFAULT_TENANT_ID,
        member.role.in_((*WORKSPACE_MEMBER_ROLES, "USER")),
    ).order_by(member.created_at, member.username)


def _member_presentation(member, account_count: int, principal, csrf: str) -> dict:
    member_status = (
        "disabled" if member.status == "disabled"
        else "password_required" if member.status == "active" and member.must_change_password
        else "active" if member.status == "active" else "unknown"
    )
    return {
        "id": member.id,
        "username": member.username,
        "initial": member.username[:1].upper(),
        "role": "AGENT" if member.role == "USER" else member.role,
        "role_label": member_role_label(member.role),
        "account_count": account_count,
        "is_self": member.id == principal.user_id,
        "can_assign": not getattr(principal, "is_superadmin", False),
        "status": member_status,
        "status_label": member_translate(f"team.{member_status}") if member_status != "unknown" else "—",
        "actions": trusted_html(_user_actions(
            member, csrf, "/admin/users", getattr(principal, "is_superadmin", False),
            allow_self_status_role=member.id != principal.user_id, include_access=False,
        )),
    }


def _member_create_form(csrf: str) -> str:
    create_fields = (
        _csrf_field(csrf)
        + _field("username", translate("admin.users.username"), autocomplete="username")
        + _field("initial_password", translate("admin.users.initial_password"), input_type="password", autocomplete="new-password")
        + _role_field() + _reauthentication_field()
    )
    return render_template(
        "tenant/member_create.html", copy=member_translate,
        create_fields=trusted_html(create_fields),
    )


def _render_member_workspace(principal, members, csrf: str, *, tab: str = "members") -> str:
    active_tab = tab if tab in {"members", "roles", "scope"} else "members"
    capabilities = sorted(set().union(*(ROLE_CAPABILITIES[role] for role in WORKSPACE_MEMBER_ROLES)))
    return render_template(
        "tenant/member_workspace.html", copy=member_translate,
        active_tab=active_tab,
        members=[_member_presentation(member, count, principal, csrf) for member, count in members],
        roles=WORKSPACE_MEMBER_ROLES, role_label=member_role_label,
        capabilities=capabilities, role_capabilities=ROLE_CAPABILITIES,
    )


async def _workspace_users_page(request: Request, principal, notice: str) -> Response:
    if principal.is_superadmin:
        principal.require_tenant(DEFAULT_TENANT_ID)
    else:
        require_workspace_member_manager(principal)
    async with get_session_factory()() as session:
        members = list((await session.execute(_member_list_statement())).all())
    csrf = _csrf(request)
    banner = (
        f'<div class="banner ok" role="status">{html.escape(translate("admin.users.operation_completed"))}</div>'
        if notice else ""
    )
    body = banner + _render_member_workspace(
        principal, members, csrf, tab=request.query_params.get("tab", "members"),
    )
    response = HTMLResponse(render_saas_page(
        principal=principal, title=member_translate("team.title"),
        description=member_translate("team.description"), body=body,
        active_navigation="users", tenant_id=DEFAULT_TENANT_ID,
        primary_action_html=_member_create_form(csrf),
    ))
    if not request.cookies.get(_CSRF_COOKIE):
        response.set_cookie(
            _CSRF_COOKIE, csrf, httponly=False, samesite="lax", secure=_secure_cookie(request),
        )
    return response


@router.get("/users", response_class=HTMLResponse)
async def legacy_users_page(request: Request) -> Response:
    return await users_page(request, notice=request.query_params.get("notice", ""))


@router.get("/system/users", response_class=HTMLResponse)
async def users_page(request: Request, notice: str = "") -> Response:
    principal = await _user_manager(request)
    base_path = _users_base_path(request)
    if isinstance(principal, Response):
        return principal
    if base_path == "/admin/users":
        return await _workspace_users_page(request, principal, notice)
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
    rows = (
        "".join(
            f"<tr><td>{html.escape(user.username)}</td>"
            f"<td>{html.escape(member_role_label(user.role))}</td>"
            f"<td>{status_badge(user.status)}</td>"
            f"<td>{html.escape(translate('admin.users.must_change_password') if user.must_change_password else translate('admin.users.normal'))}</td>"
            f"<td class='muted'>{user.created_at:%Y-%m-%d %H:%M}</td>"
            f"<td>{_user_actions(user, csrf, base_path, principal.is_superadmin, allow_self_status_role=not (principal.is_workspace_admin and not principal.is_superadmin and user.id == principal.user_id))}</td></tr>"
            for user in users
        )
        or f"<tr><td colspan='6' class='muted'>{translate('admin.users.empty')}</td></tr>"
    )
    create_form = f"""<section class="saas-card"><div class="saas-card-header"><div><h2>{translate("admin.users.create_title")}</h2>
<p>{translate("admin.users.default_tenant_hint")}</p></div></div><div class="saas-card-body"><form class="saas-form" method="post" action="{base_path}">{_csrf_field(csrf)}
{_field("username", translate("admin.users.username"), autocomplete="username")}
{_field("initial_password", translate("admin.users.initial_password"), input_type="password", autocomplete="new-password")}
{_role_field()}{_reauthentication_field()}
<button class="saas-button primary" type="submit">{translate("admin.users.create")}</button></form></div></section>"""
    banner = (
        f'<div class="banner ok">{html.escape(translate("admin.users.operation_completed"))}</div>'
        if notice
        else ""
    )
    page_title = translate("admin.users.title")
    body = f"""{banner}<section class="saas-next-action"><div><div class="saas-eyebrow">{html.escape(translate("nav.users_access"))}</div>
<h2>{html.escape(page_title)}</h2><p>{html.escape(translate("admin.users.description"))}</p></div>{status_badge("active", label=DEFAULT_TENANT_ID)}</section>
<div class="saas-grid two">{create_form}<section class="saas-card"><div class="saas-card-header"><div><h2>{translate("admin.users.list_title")}</h2><p>{translate("admin.users.default_tenant_hint")}</p></div></div><div class="saas-card-body"><div class="saas-table-wrap"><table class="saas-table">
<thead><tr><th>{translate("admin.users.username")}</th><th>{translate("admin.users.role")}</th>
<th>{translate("admin.users.account_status")}</th><th>{translate("admin.users.password_status")}</th>
<th>{translate("admin.users.created_at")}</th><th>{translate("admin.common.operation")}</th></tr></thead>
<tbody>{rows}</tbody></table></div></div></section></div>"""
    response = HTMLResponse(
        render_saas_page(
            principal=principal,
            title=page_title,
            description=translate("admin.users.description"),
            body=body,
            active_navigation="system-users" if base_path.startswith("/admin/system/") else "users",
            tenant_id=None if base_path.startswith("/admin/system/") else DEFAULT_TENANT_ID,
            system_admin=base_path.startswith("/admin/system/"),
        )
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
    principal = await _user_manager(request)
    if isinstance(principal, Response):
        return principal, None
    form = await _form(request)
    _require_csrf(request, form)
    return principal, form


def _redirect(notice: str, request: Request) -> RedirectResponse:
    return RedirectResponse(
        f"{_users_base_path(request)}?notice={notice}",
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
            role=form.get("role", "AGENT"),
            bootstrap_password=form.get("bootstrap_password", ""),
            actor=_actor(principal),
        )
    except SystemUserManagementError as exc:
        raise _management_http_error(exc) from exc
    return _redirect("created", request)


@router.post("/users/{user_id}/status")
@router.post("/system/users/{user_id}/status")
async def change_user_status(request: Request, user_id: uuid.UUID) -> Response:
    principal, form = await _management_form(request)
    if isinstance(principal, Response):
        return principal
    assert form is not None
    if principal.is_workspace_admin and not principal.is_superadmin and principal.user_id == user_id:
        raise HTTPException(status_code=403, detail="cannot_modify_own_status")
    try:
        await set_system_user_status(
            user_id=user_id,
            user_status=form.get("status", ""),
            bootstrap_password=form.get("bootstrap_password", ""),
            actor=_actor(principal),
            emergency_reason=form.get("emergency_reason", ""),
        )
    except SystemUserManagementError as exc:
        raise _management_http_error(exc) from exc
    return _redirect("status-updated", request)


@router.post("/users/{user_id}/password-reset")
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
    return _redirect("password-reset", request)


@router.post("/users/{user_id}/sessions/revoke")
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
    return _redirect("sessions-revoked", request)


@router.post("/users/{user_id}/role")
@router.post("/system/users/{user_id}/role")
async def change_user_role(request: Request, user_id: uuid.UUID) -> Response:
    principal, form = await _management_form(request)
    if isinstance(principal, Response):
        return principal
    assert form is not None
    if principal.is_workspace_admin and not principal.is_superadmin and principal.user_id == user_id:
        raise HTTPException(status_code=403, detail="cannot_modify_own_role")
    try:
        await set_system_user_role(
            user_id=user_id,
            role=form.get("role", ""),
            bootstrap_password=form.get("bootstrap_password", ""),
            actor=_actor(principal),
            emergency_reason=form.get("emergency_reason", ""),
        )
    except SystemUserManagementError as exc:
        raise _management_http_error(exc) from exc
    return _redirect("role-updated", request)


def _member_access_form(member: WorkspaceMemberAccess, csrf: str) -> str:
    return render_template(
        "tenant/member_access.html", member=member, copy=member_translate,
        csrf_field=trusted_html(_csrf_field(csrf)),
        confirmation_field=trusted_html(_reauthentication_field()),
        operator_fields=(
            ("operator_reply_enabled", "access.operator_reply", member.operator_reply_enabled),
            ("operator_takeover_enabled", "access.operator_takeover", member.operator_takeover_enabled),
        ),
    )


@router.get("/users/{user_id}/access", response_class=HTMLResponse)
async def member_access_page(request: Request, user_id: uuid.UUID) -> Response:
    principal = await _user_manager(request)
    if isinstance(principal, Response):
        return principal
    require_workspace_member_manager(principal)
    try:
        member = await get_workspace_member_access(principal=principal, user_id=user_id)
    except SystemUserManagementError as exc:
        raise _management_http_error(exc) from exc
    csrf = _csrf(request)
    page_title = member_translate("access.title")
    body = f"""<section class="member-workspace member-workspace-access"><header class="member-workspace-access-heading"><div class="member-workspace-identity">
<span class="member-workspace-avatar" aria-hidden="true">{html.escape(member.username[:1].upper())}</span>
<div><h2>{html.escape(member.username)}</h2><p>{html.escape(member_role_label(member.role))}</p></div></div>
<a class="member-workspace-button" href="/admin/users">{html.escape(member_translate("access.back"))}</a></header>
{_member_access_form(member, csrf)}</section>"""
    response = HTMLResponse(render_saas_page(
        principal=principal, title=page_title,
        description=member_translate("access.description"), body=body,
        active_navigation="users", tenant_id=DEFAULT_TENANT_ID,
    ))
    if not request.cookies.get(_CSRF_COOKIE):
        response.set_cookie(
            _CSRF_COOKIE, csrf, httponly=False, samesite="lax", secure=_secure_cookie(request),
        )
    return response


@router.post("/users/{user_id}/access")
async def change_member_access(request: Request, user_id: uuid.UUID) -> Response:
    principal, form = await _management_form(request)
    if isinstance(principal, Response):
        return principal
    require_workspace_member_manager(principal)
    assert form is not None
    try:
        await set_workspace_member_access(
            user_id=user_id, selection=parse_member_access_form(form),
            bootstrap_password=form.get("bootstrap_password", ""), actor=_actor(principal),
            expected_revision=form.get("access_revision", ""),
        )
    except SystemUserManagementError as exc:
        raise _management_http_error(exc) from exc
    return _redirect("access-updated", request)
