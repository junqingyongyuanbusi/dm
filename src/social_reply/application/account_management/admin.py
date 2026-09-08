import html
import re
import secrets
import uuid
from contextvars import ContextVar, Token
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import delete, select

from social_reply.application.account_management.access import (
    lock_session_authorities,
    lock_user_authority,
)
from social_reply.application.account_management.auth import (
    Principal,
    _credential_fingerprint,
    _token_digest,
    authenticate,
    current_principal,
    hash_password,
    principal_from_session_row,
    revoke_session,
    verify_password,
)
from social_reply.application.account_management.channel_management import (
    ChannelActor,
    ChannelConflictError,
    ChannelManagementError,
    ChannelPermissionError,
    build_provisioning_command,
    submit_channel_provisioning,
)
from social_reply.application.account_management.jobs import (
    requires_secret_resubmission,
    retry_provisioning_job,
)
from social_reply.application.account_management.saas_ui import (
    NavigationGroup,
    NavigationItem,
    _tenant_navigation_groups,
    navigation_icon,
    render_shared_page,
)
from social_reply.application.account_management.ui_i18n import translate
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.queue.dispatch import dispatch_actor
from social_reply.shared.config import DEFAULT_TENANT_ID, get_settings

router = APIRouter(prefix="/admin", tags=["admin-web"])
auth_router = APIRouter(prefix="/auth", tags=["auth-web"])
_SESSION_COOKIE = "reply_admin_session"
_CSRF_COOKIE = "reply_admin_csrf"
_SESSION_TTL_SECONDS = 8 * 60 * 60
_SAFE_NEXT_PATHS = {"/admin/accounts", "/admin/integrations/accounts"}
_SAFE_NEXT_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MIXED_ADMIN_PATHS = frozenset({"/admin/killswitch/toggle"})
_CURRENT_WEB_PRINCIPAL: ContextVar[Principal | None] = ContextVar(
    "reply_current_web_principal",
    default=None,
)


def set_web_principal_context(
    principal: Principal | None,
) -> Token[Principal | None]:
    return _CURRENT_WEB_PRINCIPAL.set(principal)


def reset_web_principal_context(token: Token[Principal | None]) -> None:
    _CURRENT_WEB_PRINCIPAL.reset(token)


def _safe_auth_next(value: object) -> str | None:
    candidate = str(value or "")
    if not candidate or len(candidate) > 2048 or "\\" in candidate:
        return None
    parsed = urlsplit(candidate)
    if parsed.scheme or parsed.netloc or parsed.fragment:
        return None
    if parsed.path == "/app" or parsed.path.startswith("/app/"):
        return parsed.path + (f"?{parsed.query}" if parsed.query else "")
    if parsed.path not in _SAFE_NEXT_PATHS:
        return None
    query = parse_qs(parsed.query, keep_blank_values=True)
    if set(query) - {"provider", "status", "code"}:
        return None
    normalized: dict[str, str] = {}
    if "provider" in query:
        if query["provider"] != ["x"]:
            return None
        normalized["provider"] = "x"
    if "status" in query:
        if len(query["status"]) != 1 or query["status"][0] not in {
            "connected",
            "error",
            "processing",
        }:
            return None
        normalized["status"] = query["status"][0]
    if "code" in query:
        if len(query["code"]) != 1 or not _SAFE_NEXT_CODE_RE.fullmatch(query["code"][0]):
            return None
        normalized["code"] = query["code"][0]
    return parsed.path + (f"?{urlencode(normalized)}" if normalized else "")


async def _web_principal(
    request: Request,
    *,
    allow_password_change: bool = False,
    require_admin: bool = True,
) -> Principal | Response:
    principal = await current_principal(request)
    if principal is None:
        return RedirectResponse("/auth/login", status_code=status.HTTP_303_SEE_OTHER)
    if principal.must_change_password and not allow_password_change:
        return RedirectResponse("/auth/change-password", status_code=status.HTTP_303_SEE_OTHER)
    if request.url.path.startswith("/admin/system/"):
        principal.require_superadmin()
    elif request.url.path in _MIXED_ADMIN_PATHS:
        if not principal.is_admin and not principal.is_superadmin:
            principal.require_admin()
    elif require_admin:
        principal.require_admin()
    if (
        not request.url.path.startswith("/admin/system/")
        and principal.is_admin
        and not principal.is_superadmin
        and principal.tenant_id != DEFAULT_TENANT_ID
    ):
        raise HTTPException(status_code=404, detail="tenant_workspace_not_found")
    set_web_principal_context(principal)
    return principal


def _csrf(request: Request) -> str:
    token = request.cookies.get(_CSRF_COOKIE)
    return token or secrets.token_urlsafe(24)


def _secure_cookie(request: Request) -> bool:
    return request.url.scheme == "https" or (
        not get_settings().testing and get_settings().public_base_url.startswith("https://")
    )


def _ensure_csrf(response: Response, request: Request, csrf: str) -> Response:
    if not request.cookies.get(_CSRF_COOKIE):
        response.set_cookie(
            _CSRF_COOKIE, csrf, httponly=False, samesite="lax", secure=_secure_cookie(request)
        )
    return response


async def _form(request: Request) -> dict[str, str]:
    body = (await request.body()).decode()
    return {key: values[-1] for key, values in parse_qs(body, keep_blank_values=True).items()}


def _require_csrf(request: Request, form: dict[str, str]) -> None:
    cookie = request.cookies.get(_CSRF_COOKIE)
    submitted = form.get("csrf_token")
    if not cookie or not submitted or not secrets.compare_digest(cookie, submitted):
        raise HTTPException(status_code=403, detail="invalid_csrf_token")


def tenant_id_or_default(principal: Principal, requested: str) -> str:
    tenant = (requested or "").strip() or principal.tenant_id or ""
    if not tenant:
        tenant = sorted(principal.allowed_tenants)[0]
    if tenant not in principal.allowed_tenants:
        raise HTTPException(status_code=403, detail="tenant_access_denied")
    if tenant != DEFAULT_TENANT_ID:
        raise HTTPException(status_code=404, detail="tenant_workspace_not_found")
    return tenant


def _admin_navigation_groups(
    show_system_controls: bool,
    *,
    principal: Principal | None = None,
    tenant_id: str | None = None,
) -> tuple[NavigationGroup, ...]:
    if show_system_controls:
        return (
            NavigationGroup(
                translate("nav.group.system"),
                (
                    NavigationItem(
                        "system-overview",
                        "/admin/system/overview",
                        translate("nav.system_overview"),
                    ),
                    NavigationItem(
                        "users", "/admin/system/users", translate("nav.user_management")
                    ),
                    NavigationItem(
                        "safety", "/admin/system/safety", translate("nav.security_controls")
                    ),
                    NavigationItem(
                        "audit", "/admin/system/audit", translate("nav.cross_tenant_audit")
                    ),
                ),
            ),
        )
    if principal is not None and principal.is_admin and tenant_id:
        return _tenant_navigation_groups(principal, tenant_id, 0)
    return ()


def _page(
    title: str,
    body: str,
    *,
    show_logout: bool = True,
    refresh_seconds: int = 0,
    active: str = "",
    show_users: bool = False,
    principal: Principal | None = None,
) -> str:
    current_principal = principal or _CURRENT_WEB_PRINCIPAL.get()
    footer_link_html = (
        '<a class="saas-nav-item" href="/app">'
        f'<span class="saas-nav-item-content">{navigation_icon("home")}'
        f'<span class="saas-nav-text">{html.escape(translate("nav.open_tenant_workspace"))}'
        "</span></span></a>"
        if show_logout and current_principal is not None
        else ""
    )
    return render_shared_page(
        title=title,
        body=body,
        surface="admin" if show_logout else "auth",
        navigation_groups=(
            _admin_navigation_groups(
                bool(current_principal is not None and current_principal.is_superadmin),
                principal=current_principal,
                tenant_id=current_principal.tenant_id if current_principal else None,
            )
            if show_logout
            else ()
        ),
        active_navigation=active,
        principal=current_principal,
        tenant_id=current_principal.tenant_id if current_principal else None,
        refresh_seconds=refresh_seconds,
        legacy_content=True,
        footer_link_html=footer_link_html,
    )


def _login_failure_page(retry_link: str) -> str:
    page_title = translate("auth.login_failed")
    return _page(
        page_title,
        f"""<div class="login-wrap"><section class="card login-card"><h1>{page_title}</h1>
<p class="hint">{translate("auth.invalid_credentials")}</p>
<p><a href="{retry_link}">{translate("auth.retry")}</a></p></section></div>""",
        show_logout=False,
    )


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
@auth_router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    csrf = _csrf(request)
    next_target = _safe_auth_next(request.query_params.get("next"))
    next_field = (
        f'<input type="hidden" name="next" value="{html.escape(next_target, quote=True)}">'
        if next_target
        else ""
    )
    page_title = translate("auth.title")
    response = HTMLResponse(
        _page(
            page_title,
            f"""<div class="login-wrap"><section class="card login-card"><h1>{page_title}</h1>
<p class="hint">{translate("auth.login_description")}</p>
<form method="post" action="/auth/login"><input type="hidden" name="csrf_token" value="{csrf}">{next_field}
<label for="f-username">{translate("auth.username")}</label><input id="f-username" name="username" autocomplete="username" required>
<label for="f-password">{translate("auth.password")}</label><input id="f-password" name="password" type="password" autocomplete="current-password" required>
<button type="submit" class="btn-block">{translate("auth.submit")}</button></form></section></div>""",
            show_logout=False,
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


@router.post("/login", include_in_schema=False)
@auth_router.post("/login")
async def login(request: Request) -> Response:
    form = await _form(request)
    _require_csrf(request, form)
    next_target = _safe_auth_next(form.get("next"))
    username = (form.get("username") or "").strip()
    password = form.get("password") or ""
    retry_target = "/auth/login"
    if next_target:
        retry_target = f"{retry_target}?{urlencode({'next': next_target})}"
    retry_link = html.escape(retry_target, quote=True)
    if len(username) > 128 or len(password) > 128:
        return HTMLResponse(
            _login_failure_page(retry_link),
            401,
        )
    result = await authenticate(username, password)
    if result is None:
        return HTMLResponse(
            _login_failure_page(retry_link),
            401,
        )
    principal, raw_token = result
    if principal.must_change_password:
        target = "/auth/change-password"
        if next_target:
            target = f"{target}?{urlencode({'next': next_target})}"
    elif principal.is_superadmin:
        target = "/admin/system/overview"
    else:
        target = next_target or "/app"
    response = RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        _SESSION_COOKIE,
        raw_token,
        httponly=True,
        samesite="lax",
        secure=_secure_cookie(request),
        max_age=_SESSION_TTL_SECONDS,
    )
    return response


@router.get("/logout", response_class=HTMLResponse, include_in_schema=False)
@auth_router.get("/logout", response_class=HTMLResponse)
async def logout_page(request: Request) -> Response:
    principal = await _web_principal(
        request,
        allow_password_change=True,
        require_admin=False,
    )
    if isinstance(principal, Response):
        return principal
    csrf = _csrf(request)
    page_title = translate("auth.confirm_sign_out")
    response = HTMLResponse(
        _page(
            page_title,
            f"""<div class="login-wrap"><section class="card login-card"><h1>{page_title}</h1>
<p class="hint">{translate("auth.sign_out_description")}</p>
<form method="post" action="/auth/logout"><input type="hidden" name="csrf_token" value="{csrf}">
<button type="submit" class="btn-block">{translate("auth.sign_out")}</button></form></section></div>""",
            show_logout=False,
            principal=principal,
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


@router.post("/logout", include_in_schema=False)
@auth_router.post("/logout")
async def logout(request: Request) -> Response:
    form = await _form(request)
    _require_csrf(request, form)
    await revoke_session(request.cookies.get(_SESSION_COOKIE, ""))
    response = RedirectResponse("/auth/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(_SESSION_COOKIE)
    return response


@router.get("/change-password", response_class=HTMLResponse, include_in_schema=False)
@auth_router.get("/change-password", response_class=HTMLResponse)
async def change_password_page(request: Request) -> Response:
    principal = await _web_principal(
        request,
        allow_password_change=True,
        require_admin=False,
    )
    if isinstance(principal, Response):
        return principal
    next_target = _safe_auth_next(request.query_params.get("next"))
    if principal.is_superadmin:
        return RedirectResponse(
            "/admin/system/overview",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    page_title = translate(
        "auth.first_password_title"
        if principal.must_change_password
        else "auth.change_password_title"
    )
    password_hint = (
        translate("auth.first_password_hint")
        if principal.must_change_password
        else translate("auth.change_password_hint")
    )
    csrf = _csrf(request)
    next_field = (
        f'<input type="hidden" name="next" value="{html.escape(next_target, quote=True)}">'
        if next_target
        else ""
    )
    response = HTMLResponse(
        _page(
            page_title,
            f"""<div class="login-wrap"><section class="card login-card"><h1>{page_title}</h1>
<p class="hint">{password_hint}</p>
<form method="post" action="/auth/change-password"><input type="hidden" name="csrf_token" value="{csrf}">{next_field}
<label for="f-current-password">{translate("auth.initial_password")}</label><input id="f-current-password" name="current_password" type="password" autocomplete="current-password" required>
<label for="f-new-password">{translate("auth.new_password")}</label><input id="f-new-password" name="new_password" type="password" autocomplete="new-password" minlength="12" maxlength="128" required>
<label for="f-confirm-password">{translate("auth.confirm_password")}</label><input id="f-confirm-password" name="confirm_password" type="password" autocomplete="new-password" minlength="12" maxlength="128" required>
<button type="submit" class="btn-block">{translate("auth.save_new_password")}</button></form></section></div>""",
            show_logout=False,
            principal=principal,
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


@router.post("/change-password", include_in_schema=False)
@auth_router.post("/change-password")
async def change_password(request: Request) -> Response:
    principal = await _web_principal(
        request,
        allow_password_change=True,
        require_admin=False,
    )
    if isinstance(principal, Response):
        return principal
    if principal.is_superadmin or principal.user_id is None:
        raise HTTPException(status_code=403, detail="password_change_not_available")
    form = await _form(request)
    _require_csrf(request, form)
    next_target = _safe_auth_next(form.get("next"))
    new_password = form.get("new_password") or ""
    if new_password != (form.get("confirm_password") or ""):
        raise HTTPException(status_code=422, detail="password_confirmation_mismatch")
    async with get_session_factory()() as session:
        await lock_user_authority(session, principal.user_id)
        session_ids = set(
            await session.scalars(
                select(models.AdminSession.id).where(
                    models.AdminSession.user_id == principal.user_id
                )
            )
        )
        session_ids.add(principal.session_id)
        await lock_session_authorities(session, session_ids)
        current = await principal_from_session_row(session, principal.session_id, for_update=True)
        if current is None or current.user_id != principal.user_id or current.is_feishu_action:
            raise HTTPException(status_code=401, detail="password_change_session_revoked")
        principal = current
        user = (
            await session.execute(
                select(models.AdminUser)
                .where(
                    models.AdminUser.id == principal.user_id,
                    models.AdminUser.tenant_id == principal.tenant_id,
                    models.AdminUser.status == "active",
                )
                .execution_options(populate_existing=True)
                .with_for_update()
            )
        ).scalar_one_or_none()
        current_password = form.get("current_password") or ""
        if user is None or not await verify_password(user.password_hash, current_password):
            raise HTTPException(status_code=401, detail="current_password_invalid")
        if secrets.compare_digest(new_password.encode(), current_password.encode()):
            raise HTTPException(status_code=422, detail="new_password_must_be_different")
        try:
            user.password_hash = await hash_password(new_password)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        user.must_change_password = False
        user.password_changed_at = datetime.now(UTC)
        await session.execute(
            delete(models.AdminSession).where(models.AdminSession.user_id == user.id)
        )
        raw_token = secrets.token_urlsafe(32)
        new_session_id = uuid.uuid4()
        session.add(
            models.AdminSession(
                id=new_session_id,
                token_digest=_token_digest(raw_token),
                user_id=user.id,
                bootstrap_fingerprint=None,
                credential_fingerprint=_credential_fingerprint(user.password_hash),
                expires_at=datetime.now(UTC) + timedelta(hours=8),
            )
        )
        session.add(
            models.AuditLog(
                tenant_id=user.tenant_id,
                category="user_management",
                actor=principal.actor,
                action="CHANGE_PASSWORD",
                subject_type="admin_user",
                subject_id=str(user.id),
                detail={"first_login": principal.must_change_password},
            )
        )
        await session.commit()
    default_target = "/admin" if principal.is_admin else "/app"
    response = RedirectResponse(
        next_target or default_target, status_code=status.HTTP_303_SEE_OTHER
    )
    response.set_cookie(
        _SESSION_COOKIE,
        raw_token,
        httponly=True,
        samesite="lax",
        secure=_secure_cookie(request),
        max_age=_SESSION_TTL_SECONDS,
    )
    return response


def _input(
    name: str,
    label: str,
    *,
    secret: bool = False,
    required: bool = True,
    value: str = "",
    readonly: bool = False,
    input_type: str | None = None,
    inputmode: str | None = None,
    min: int | str | None = None,
    max: int | str | None = None,
    autocomplete: str | None = None,
) -> str:
    field_id = f"f-{name}-{secrets.token_hex(3)}"  # 同名字段出现在多个表单，id 需唯一
    resolved_type = input_type or ("password" if secret else "text")
    attributes = [
        f'id="{field_id}"',
        f'type="{html.escape(resolved_type, quote=True)}"',
        f'name="{html.escape(name, quote=True)}"',
    ]
    if value:
        attributes.append(f'value="{html.escape(value, quote=True)}"')
    if required:
        attributes.append("required")
    if readonly:
        attributes.append("readonly")
    for attribute, attribute_value in (
        ("inputmode", inputmode),
        ("min", min),
        ("max", max),
        ("autocomplete", autocomplete),
    ):
        if attribute_value is not None:
            attributes.append(f'{attribute}="{html.escape(str(attribute_value), quote=True)}"')
    return f'<label for="{field_id}">{html.escape(label)}</label><input {" ".join(attributes)}>'


_STATUS_TONES = {
    "active": "ok",
    "CONNECTED": "ok",
    "COMPLETED": "ok",
    "SENT": "ok",
    "READY": "ok",
    "ACTIVE": "ok",
    "BOT_NOT_ACTIVE": "err",
    "BOT_ID_MISMATCH": "err",
    "CREDENTIAL_INVALID": "err",
    "published": "ok",
    "BOT_ACTIVE": "ok",
    "auto_reply": "ok",
    "HEALTHY": "ok",
    "WARNING": "warn",
    "ACTION": "err",
    "PENDING": "warn",
    "WAITING": "warn",
    "PROCESSING": "warn",
    "QUEUED": "warn",
    "SENDING": "warn",
    "BOT_DRAFT_ONLY": "warn",
    "HANDOFF_PENDING": "warn",
    "draft": "warn",
    "handoff": "warn",
    "FAILED": "err",
    "NEEDS_ACTION": "err",
    "NEEDS_REVIEW": "err",
    "DECISION_NEEDS_REVIEW": "err",
    "XCHAT_DECRYPTION_PENDING": "warn",
    "XCHAT_PROCESSING": "warn",
    "XCHAT_RETRYABLE_ERROR": "warn",
    "XCHAT_KEY_RECOVERY_REQUIRED": "err",
    "XCHAT_DECRYPT_FAILED": "err",
    "XCHAT_RETRY_EXHAUSTED": "err",
    "XCHAT_REAUTHORIZATION_REQUIRED": "err",
    "XCHAT_ACCESS_FORBIDDEN": "err",
    "RECOVERY_REQUIRED": "warn",
    "NOT_REGISTERED": "neutral",
    "NOT_REQUIRED": "neutral",
    "INVALID": "err",
    "ERROR": "err",
    "UNKNOWN": "neutral",
    "HUMAN_ACTIVE": "info",
    "CLAIMED": "info",
    "EDITED": "info",
    "BOT_COOLDOWN": "neutral",
    "CLOSED": "neutral",
    "CANCELLED": "neutral",
    "NONE": "neutral",
    "RESOLVED": "ok",
    "ACCEPTED": "ok",
    "REJECTED": "err",
    "ignore": "neutral",
}

_STATUS_LABEL_KEYS = {
    "PENDING": "status.pending",
    "PROCESSING": "status.processing",
    "COMPLETED": "status.completed",
    "SENT": "status.sent",
    "FAILED": "status.failed",
    "NEEDS_ACTION": "status.needs_action",
    "NEEDS_REVIEW": "status.needs_review",
    "WAITING": "status.waiting",
    "CLAIMED": "status.claimed",
    "RESOLVED": "status.resolved",
    "CANCELLED": "status.cancelled",
}


def _pill(status: str) -> str:
    tone = _STATUS_TONES.get(status, "neutral")
    translation_key = _STATUS_LABEL_KEYS.get(status)
    visible_status = translate(translation_key) if translation_key else status
    return (
        f'<span class="pill {tone}" title="{html.escape(status)}">'
        f"{html.escape(visible_status)}</span>"
    )


async def _submit_form(
    request: Request,
    platform: str,
    form: dict[str, str] | None = None,
    *,
    require_admin: bool = True,
    redirect_path: str | None = None,
) -> Response:
    principal = await _web_principal(request, require_admin=require_admin)
    if isinstance(principal, Response):
        return principal
    form = form or await _form(request)
    _require_csrf(request, form)
    tenant_id = (form.get("tenant_id") or "").strip()
    if not tenant_id:
        raise HTTPException(status_code=422, detail="tenant_id_required")
    if tenant_id != DEFAULT_TENANT_ID:
        raise HTTPException(status_code=404, detail="tenant_workspace_not_found")
    principal.require_tenant(tenant_id)
    try:
        command = build_provisioning_command(
            route_tenant_id=tenant_id,
            platform=platform,
            actor=ChannelActor(
                actor=principal.actor,
                role="ADMIN" if principal.is_admin else "USER",
                user_id=principal.user_id,
                session_id=principal.session_id,
            ),
            values=form,
        )
        job_id = await submit_channel_provisioning(command)
    except ChannelManagementError as exc:
        if isinstance(exc, ChannelConflictError):
            status_code = 409
        elif isinstance(exc, ChannelPermissionError):
            status_code = 403
        else:
            status_code = 503 if exc.code.endswith("_integration_disabled") else 422
        raise HTTPException(status_code=status_code, detail=exc.code) from exc
    target = redirect_path or f"/admin/integrations/provisioning-jobs/{job_id}"
    return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/connect/telegram")
async def admin_connect_telegram(request: Request) -> Response:
    return await _submit_form(request, "telegram")


@router.post("/connect/meta")
async def admin_connect_meta(request: Request) -> Response:
    form = await _form(request)
    platform = form.get("platform", "")
    if platform not in {"facebook", "instagram"}:
        raise HTTPException(status_code=422, detail="unsupported_meta_platform")
    return await _submit_form(request, platform, form)


@router.post("/connect/whatsapp")
async def admin_connect_whatsapp(request: Request) -> Response:
    return await _submit_form(request, "whatsapp")


@router.post("/connect/x")
async def admin_connect_x(request: Request) -> Response:
    return await _submit_form(request, "x")


@router.post("/connect/feishu")
async def admin_connect_feishu(request: Request) -> Response:
    return await _submit_form(request, "feishu")


@router.post("/connect/email")
async def admin_connect_email(request: Request) -> Response:
    return await _submit_form(request, "email")


@router.get("/integrations/provisioning-jobs/{job_id}", response_class=HTMLResponse)
@router.get("/jobs/{job_id}", response_class=HTMLResponse)
async def admin_job(request: Request, job_id: uuid.UUID) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    tenant_id_or_default(principal, "")
    return RedirectResponse(
        f"/app/t/{DEFAULT_TENANT_ID}/channels/jobs/{job_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/jobs/{job_id}/retry")
async def admin_retry_job(request: Request, job_id: uuid.UUID) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    tenant_id_or_default(principal, "")
    form = await _form(request)
    _require_csrf(request, form)
    async with get_session_factory()() as session:
        job = await session.scalar(
            select(models.ProvisioningJob).where(
                models.ProvisioningJob.id == job_id,
                models.ProvisioningJob.tenant_id == DEFAULT_TENANT_ID,
            )
        )
    if job is None:
        raise HTTPException(status_code=404, detail="provisioning_job_not_found")
    if requires_secret_resubmission(job):
        raise HTTPException(status_code=409, detail="provisioning_secret_resubmission_required")
    if not get_settings().platform_integration_enabled(job.platform):
        raise HTTPException(status_code=503, detail=f"{job.platform}_integration_disabled")
    try:
        await retry_provisioning_job(
            job_id,
            tenant_id=job.tenant_id,
            caller=principal,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="provisioning_job_not_found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    from social_reply.application.account_management.actors import process_platform_provisioning
    from social_reply.application.account_management.jobs import process_provisioning_job

    await dispatch_actor(
        process_platform_provisioning,
        str(job_id),
        inline=lambda: process_provisioning_job(str(job_id)),
    )
    return RedirectResponse(
        f"/app/t/{job.tenant_id}/channels",
        status_code=status.HTTP_303_SEE_OTHER,
    )
