import html
import secrets
import uuid

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from social_reply.application.account_management.admin import (
    _csrf,
    _form,
    _page,
    _require_csrf,
    _secure_cookie,
    _web_principal,
)
from social_reply.application.account_management.auth import hash_password
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.shared.config import get_settings

router = APIRouter(prefix="/admin/users", tags=["admin-users"])
_CSRF_COOKIE = "reply_admin_csrf"


def _field(name: str, label: str, *, input_type: str = "text") -> str:
    field_id = f"f-user-{name}-{secrets.token_hex(3)}"
    return (
        f'<label for="{field_id}">{html.escape(label)}</label>'
        f'<input id="{field_id}" name="{name}" type="{input_type}" required>'
    )


async def _superadmin(request: Request):
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    if not principal.is_superadmin:
        raise HTTPException(status_code=403, detail="superadmin_required")
    return principal


@router.get("", response_class=HTMLResponse)
async def users_page(request: Request, notice: str = "") -> Response:
    principal = await _superadmin(request)
    if isinstance(principal, Response):
        return principal
    async with get_session_factory()() as session:
        users = (
            (
                await session.execute(
                    select(models.AdminUser)
                    .where(models.AdminUser.tenant_id.in_(principal.allowed_tenants))
                    .order_by(models.AdminUser.created_at)
                )
            )
            .scalars()
            .all()
        )
    assigned = {user.tenant_id for user in users}
    available = sorted(principal.allowed_tenants - assigned)
    rows = (
        "".join(
            f"<tr><td>{html.escape(user.username)}</td>"
            f"<td><code>{html.escape(user.tenant_id)}</code></td>"
            f"<td>{'待首次改密' if user.must_change_password else '正常'}</td>"
            f"<td>{html.escape(user.status)}</td>"
            f"<td class='muted'>{user.created_at:%Y-%m-%d %H:%M}</td></tr>"
            for user in users
        )
        or "<tr><td colspan='5' class='muted'>暂无普通用户</td></tr>"
    )
    csrf = _csrf(request)
    tenant_options = "".join(
        f'<option value="{html.escape(tenant)}">{html.escape(tenant)}</option>'
        for tenant in available
    )
    create_form = (
        f"""<section class="card"><h2>创建普通用户</h2>
<p class="hint">用户仅能访问绑定的一个 Tenant，首次登录必须修改初始密码。不发送邮件或邀请。</p>
<form method="post" action="/admin/users"><input type="hidden" name="csrf_token" value="{csrf}">
{_field("username", "用户名")}
{_field("initial_password", "初始密码（12–128 个字符）", input_type="password")}
<label for="f-user-tenant">Tenant</label>
<select id="f-user-tenant" name="tenant_id" required>{tenant_options}</select>
<button class="btn-block">创建用户</button></form></section>"""
        if available
        else (
            '<section class="card"><h2>创建普通用户</h2>'
            '<p class="hint">所有允许的 Tenant 均已分配用户。</p></section>'
        )
    )
    banner = (
        '<div class="banner ok">用户已创建。请通过线下安全渠道交付初始密码。</div>'
        if notice == "created"
        else ""
    )
    body = f"""<h1>用户</h1><p class="lede">管理绑定到单一 Tenant 的普通后台用户。</p>{banner}
{create_form}<section class="card"><h2>用户列表</h2><div class="tablewrap"><table>
<thead><tr><th>用户名</th><th>Tenant</th><th>密码状态</th><th>账号状态</th><th>创建时间</th></tr></thead>
<tbody>{rows}</tbody></table></div></section>"""
    response = HTMLResponse(_page("用户", body, active="users", show_users=True))
    if not request.cookies.get(_CSRF_COOKIE):
        response.set_cookie(
            _CSRF_COOKIE,
            csrf,
            httponly=False,
            samesite="lax",
            secure=_secure_cookie(request),
        )
    return response


@router.post("")
async def create_user(request: Request) -> Response:
    principal = await _superadmin(request)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    username = (form.get("username") or "").strip()
    tenant_id = (form.get("tenant_id") or "").strip()
    password = form.get("initial_password") or ""
    if not username or len(username) > 128 or any(char.isspace() for char in username):
        raise HTTPException(status_code=422, detail="invalid_username")
    if secrets.compare_digest(username, get_settings().admin_username):
        raise HTTPException(status_code=409, detail="username_conflicts_with_superadmin")
    principal.require_tenant(tenant_id)
    try:
        password_hash = await hash_password(password)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    user = models.AdminUser(
        id=uuid.uuid4(),
        username=username,
        password_hash=password_hash,
        tenant_id=tenant_id,
        must_change_password=True,
        status="active",
    )
    async with get_session_factory()() as session:
        session.add(user)
        session.add(
            models.AuditLog(
                tenant_id=tenant_id,
                category="user_management",
                actor=principal.actor,
                action="CREATE_USER",
                subject_type="admin_user",
                subject_id=str(user.id),
                detail={"username": username},
            )
        )
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise HTTPException(
                status_code=409, detail="username_or_tenant_already_exists"
            ) from exc
    return RedirectResponse("/admin/users?notice=created", status_code=status.HTTP_303_SEE_OTHER)
