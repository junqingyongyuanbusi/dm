import html
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import delete, select

from social_reply.application.account_management.auth import (
    Principal,
    _credential_fingerprint,
    _token_digest,
    authenticate,
    current_principal,
    hash_password,
    revoke_session,
    verify_password,
)
from social_reply.application.account_management.jobs import (
    provisioning_job_is_in_flight,
    public_job,
    requires_secret_resubmission,
    retry_provisioning_job,
    submit_provisioning_job,
)
from social_reply.application.account_management.submissions import split_submission
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.queue.dispatch import dispatch_actor
from social_reply.shared.config import get_settings

router = APIRouter(prefix="/admin", tags=["admin-web"])
_SESSION_COOKIE = "reply_admin_session"
_CSRF_COOKIE = "reply_admin_csrf"
_SESSION_TTL_SECONDS = 8 * 60 * 60
_SAFE_NEXT_PATHS = {"/admin/accounts"}
_SAFE_NEXT_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _safe_admin_next(value: object) -> str | None:
    candidate = str(value or "")
    if not candidate:
        return None
    parsed = urlsplit(candidate)
    if parsed.scheme or parsed.netloc or parsed.fragment or parsed.path not in _SAFE_NEXT_PATHS:
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
    request: Request, *, allow_password_change: bool = False
) -> Principal | Response:
    principal = await current_principal(request)
    if principal is None:
        return RedirectResponse("/admin/login", status_code=status.HTTP_303_SEE_OTHER)
    if principal.must_change_password and not allow_password_change:
        return RedirectResponse("/admin/change-password", status_code=status.HTTP_303_SEE_OTHER)
    return principal


def _csrf(request: Request) -> str:
    token = request.cookies.get(_CSRF_COOKIE)
    return token or secrets.token_urlsafe(24)


def _secure_cookie(request: Request) -> bool:
    return request.url.scheme == "https" or (
        not get_settings().testing and get_settings().public_base_url.startswith("https://")
    )


async def _form(request: Request) -> dict[str, str]:
    body = (await request.body()).decode()
    return {key: values[-1] for key, values in parse_qs(body, keep_blank_values=True).items()}


def _require_csrf(request: Request, form: dict[str, str]) -> None:
    cookie = request.cookies.get(_CSRF_COOKIE)
    submitted = form.get("csrf_token")
    if not cookie or not submitted or not secrets.compare_digest(cookie, submitted):
        raise HTTPException(status_code=403, detail="invalid_csrf_token")


_NAV_ITEMS = (
    ("overview", "/admin", "总览"),
    ("conversations", "/admin/conversations", "对话"),
    ("decisions", "/admin/decisions", "决策"),
    ("knowledge", "/admin/knowledge", "知识库"),
    ("prompt", "/admin/prompt", "提示词"),
    ("delivery", "/admin/delivery", "投递"),
    ("accounts", "/admin/accounts", "账号"),
)


def _page(
    title: str,
    body: str,
    *,
    show_logout: bool = True,
    refresh_seconds: int = 0,
    active: str = "",
    show_users: bool = False,
) -> str:
    """Claude 风格页面外壳：暖米白底、衬线标题、赤陶橙点缀、大留白、零 JS。"""
    refresh = f'<meta http-equiv="refresh" content="{refresh_seconds}">' if refresh_seconds else ""
    logout = '<a class="nav-link" href="/admin/logout">退出</a>' if show_logout else ""
    nav_items = _NAV_ITEMS + (("users", "/admin/users", "用户"),) if show_users else _NAV_ITEMS
    tabs = (
        "".join(
            f'<a class="tab{" active" if key == active else ""}" href="{href}">{label}</a>'
            for key, href, label in nav_items
        )
        if show_logout
        else ""
    )
    nav_bar = f'<nav class="tabs" aria-label="主导航">{tabs}</nav>' if tabs else ""
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">{refresh}
<title>{html.escape(title)}</title>
<style>
:root{{
  --bg:#FAF9F5; --surface:#FFFFFF; --surface-2:#F0EEE6;
  --text:#1F1E1D; --muted:#5E5B54;
  --accent:#C15F3C; --accent-hover:#A94F2F; --accent-tint:#F6E8E1; --link:#A64B2A;
  --border:#E8E4DA; --border-strong:#DED9CC;
  --ok-bg:#EAF2EC; --ok-fg:#2F6B44; --warn-bg:#F7EFDD; --warn-fg:#8A5A12;
  --err-bg:#F8E9E5; --err-fg:#A93A2A; --neutral-bg:#F0EEE6; --neutral-fg:#5E5B54;
  --info-bg:#E9EEF4; --info-fg:#3D5A80; --danger:#BF4232; --danger-hover:#A33526;
  --r-lg:14px; --r-md:10px;
  --serif:Georgia,'Times New Roman','Songti SC','STSong',serif;
  --sans:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--text);font-family:var(--sans);font-size:15px;line-height:1.6;-webkit-font-smoothing:antialiased}}
header{{position:sticky;top:0;z-index:10;background:var(--bg);border-bottom:1px solid var(--border);padding:12px 32px;display:flex;justify-content:space-between;align-items:center;gap:18px;flex-wrap:wrap}}
.brand{{font-family:var(--serif);font-size:19px;letter-spacing:-0.01em;white-space:nowrap}}
.brand small{{font-family:var(--sans);font-size:12px;color:var(--muted);margin-left:10px;letter-spacing:.02em}}
.tabs{{display:flex;gap:4px;flex-wrap:wrap}}
.tab{{color:var(--muted);text-decoration:none;font-size:14px;padding:8px 14px;border-radius:8px;transition:color .18s,background .18s}}
.tab:hover{{color:var(--text);background:var(--surface-2)}}
.tab.active{{color:var(--accent);background:var(--accent-tint);font-weight:500}}
.nav-link{{color:var(--muted);text-decoration:none;font-size:14px;padding:8px 12px;border-radius:8px;transition:color .18s,background .18s}}
.nav-link:hover{{color:var(--text);background:var(--surface-2)}}
main{{max-width:1100px;margin:0 auto;padding:36px 24px 72px}}
h1{{font-family:var(--serif);font-weight:600;font-size:30px;letter-spacing:-0.015em;margin:0 0 6px}}
h2{{font-family:var(--serif);font-weight:600;font-size:21px;letter-spacing:-0.01em;margin:0 0 4px}}
h3{{font-family:var(--serif);font-weight:600;font-size:17px;margin:0 0 2px}}
.lede{{color:var(--muted);margin:0 0 26px}}
.card{{background:var(--surface);border:1px solid var(--border);border-radius:var(--r-lg);padding:22px 24px;margin-bottom:22px}}
.card .hint{{color:var(--muted);font-size:13.5px;margin:2px 0 14px}}
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:16px;margin-bottom:22px}}
.stat{{background:var(--surface);border:1px solid var(--border);border-radius:var(--r-lg);padding:16px 20px}}
.stat .num{{font-family:var(--serif);font-size:30px;line-height:1.2;letter-spacing:-0.02em}}
.stat .lbl{{color:var(--muted);font-size:13px;margin-top:2px}}
.bar-row{{display:flex;align-items:center;gap:12px;margin:8px 0;font-size:13.5px}}
.bar-label{{width:88px;color:var(--muted);flex-shrink:0}}
.bar-track{{flex:1;background:var(--surface-2);border-radius:5px;height:10px;overflow:hidden}}
.bar{{height:100%;border-radius:5px}}
.bar.ok{{background:#7FA37A}} .bar.warn{{background:#C9A05A}} .bar.err{{background:#C97A6A}} .bar.neutral{{background:#B8B3A7}}
.bar-count{{width:52px;text-align:right;font-variant-numeric:tabular-nums;color:var(--text)}}
.tablewrap{{overflow-x:auto;margin:0 -4px}}
table{{width:100%;border-collapse:collapse}}
th{{font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);text-align:left;padding:10px 12px;border-bottom:1px solid var(--border-strong)}}
td{{padding:11px 12px;border-bottom:1px solid var(--border);font-size:14px;vertical-align:middle}}
tbody tr:last-child td{{border-bottom:0}}
tbody tr{{transition:background .15s}}
tbody tr:hover{{background:#F7F5EF}}
.pill{{display:inline-block;padding:3px 11px;border-radius:999px;font-size:12.5px;font-weight:500;white-space:nowrap}}
.pill.ok{{background:var(--ok-bg);color:var(--ok-fg)}} .pill.warn{{background:var(--warn-bg);color:var(--warn-fg)}}
.pill.err{{background:var(--err-bg);color:var(--err-fg)}} .pill.neutral{{background:var(--neutral-bg);color:var(--neutral-fg)}}
.pill.info{{background:var(--info-bg);color:var(--info-fg)}}
.chips{{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 16px}}
.chip{{font-size:13px;color:var(--muted);text-decoration:none;padding:6px 14px;border:1px solid var(--border-strong);border-radius:999px;transition:all .18s}}
.chip:hover{{color:var(--text);background:var(--surface-2)}}
.chip.active{{background:var(--accent-tint);border-color:var(--accent);color:var(--accent);font-weight:500}}
.thread{{display:flex;flex-direction:column;gap:10px;margin:14px 0}}
.msg{{max-width:74%;padding:10px 14px;border-radius:14px;font-size:14.5px;line-height:1.55}}
.msg.in{{align-self:flex-start;background:var(--surface-2);border-bottom-left-radius:4px}}
.msg.out{{align-self:flex-end;background:var(--accent-tint);border-bottom-right-radius:4px}}
.msg .meta{{font-size:11.5px;color:var(--muted);margin-top:4px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:20px}}
.channel-section{{margin:0 0 24px}}
.channel-heading{{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;margin-bottom:12px}}
.channel-heading p{{margin:0;color:var(--muted);font-size:13.5px}}
.channel-grid{{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px}}
.channel-tile{{min-width:0;min-height:112px;padding:14px 10px;border:1px solid var(--border);border-radius:8px;background:var(--surface);color:var(--text);text-decoration:none;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:9px;text-align:center;transition:border-color .18s,background .18s,box-shadow .18s,transform .18s}}
.channel-tile:hover{{color:var(--text);border-color:var(--border-strong);background:#F7F5EF;transform:translateY(-1px)}}
.channel-tile[aria-current="page"]{{border-color:var(--accent);box-shadow:0 0 0 2px rgba(193,95,60,.12);background:var(--accent-tint)}}
.channel-tile.disabled{{opacity:.56;cursor:not-allowed;background:var(--surface-2);transform:none}}
.channel-icon{{width:42px;height:42px;display:grid;place-items:center}}
.channel-icon img{{display:block;width:36px;height:36px;object-fit:contain}}
.channel-name{{font-size:14px;font-weight:600;line-height:1.25;overflow-wrap:anywhere}}
.channel-kind{{color:var(--muted);font-size:11.5px;line-height:1.2}}
.channel-setup{{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:22px 24px;margin:0 0 24px;scroll-margin-top:84px}}
.channel-setup-head{{display:flex;align-items:center;gap:12px;padding-bottom:16px;border-bottom:1px solid var(--border)}}
.channel-setup-head .channel-icon{{width:36px;height:36px}}
.channel-setup-head .channel-icon img{{width:32px;height:32px}}
.channel-setup-head h2{{font-family:var(--sans);font-size:18px;margin:0}}
.channel-setup-head p{{color:var(--muted);font-size:13px;margin:1px 0 0}}
.channel-form{{max-width:680px;padding-top:4px}}
.channel-meta{{display:grid;grid-template-columns:140px minmax(0,1fr);gap:7px 12px;margin:16px 0 4px;font-size:13px}}
.channel-meta dt{{color:var(--muted)}}
.channel-meta dd{{margin:0;min-width:0;overflow-wrap:anywhere}}
.channel-mode-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0;margin-top:16px}}
.channel-mode{{min-width:0;padding:0 22px 4px}}
.channel-mode:first-child{{padding-left:0}}
.channel-mode+ .channel-mode{{border-left:1px solid var(--border)}}
.channel-mode h3{{font-family:var(--sans);font-size:15px}}
.channel-mode .hint{{min-height:42px}}
.advanced-connect{{border-top:1px solid var(--border);margin-top:20px;padding-top:4px}}
.advanced-connect>summary{{cursor:pointer;color:var(--muted);font-size:13.5px;padding:12px 0;list-style:none}}
.advanced-connect>summary::before{{content:"＋";display:inline-block;width:20px;color:var(--muted)}}
.advanced-connect[open]>summary::before{{content:"－"}}
.advanced-connect .advanced-body{{max-width:680px;padding:0 0 8px}}
form.card{{margin-bottom:0}}
label{{display:block;font-size:13px;font-weight:500;color:var(--text);margin:14px 0 5px}}
input,select,textarea{{width:100%;padding:10px 12px;font-size:16px;font-family:var(--sans);color:var(--text);background:var(--surface);border:1px solid var(--border-strong);border-radius:var(--r-md);min-height:44px;transition:border-color .18s,box-shadow .18s}}
textarea{{min-height:88px;resize:vertical}}
input:focus,select:focus,textarea:focus{{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px rgba(193,95,60,.14)}}
button{{display:inline-block;padding:10px 18px;min-height:42px;font-size:14.5px;font-weight:500;font-family:var(--sans);color:#fff;background:var(--accent);border:0;border-radius:var(--r-md);cursor:pointer;transition:background .18s}}
button:hover{{background:var(--accent-hover)}}
button.btn-block{{width:100%;margin-top:20px;min-height:44px;font-size:15px}}
button.btn-sm{{padding:6px 13px;min-height:34px;font-size:13px}}
button.btn-ghost{{background:var(--surface);color:var(--text);border:1px solid var(--border-strong)}}
button.btn-ghost:hover{{background:var(--surface-2)}}
button.btn-danger{{background:var(--danger)}}
button.btn-danger:hover{{background:var(--danger-hover)}}
form.inline{{display:inline-block;margin:0 4px 0 0}}
details.collapse{{border:1px solid var(--border);border-radius:var(--r-lg);background:var(--surface);margin-bottom:22px}}
details.collapse>summary{{cursor:pointer;padding:18px 24px;font-family:var(--serif);font-size:17px;font-weight:600;list-style:none;transition:background .18s}}
details.collapse>summary:hover{{background:var(--surface-2)}}
details.collapse>summary::after{{content:"＋";float:right;color:var(--muted)}}
details.collapse[open]>summary::after{{content:"－"}}
details.collapse>.inner{{padding:0 24px 24px}}
.banner{{padding:12px 16px;border-radius:var(--r-md);font-size:14px;margin-bottom:18px}}
.banner.err{{background:var(--err-bg);color:var(--err-fg)}}
.banner.ok{{background:var(--ok-bg);color:var(--ok-fg)}}
.banner.info{{background:var(--info-bg);color:var(--info-fg)}}
a{{color:var(--link)}}
:focus-visible{{outline:2px solid var(--accent);outline-offset:2px}}
.muted{{color:var(--muted);font-size:13.5px}}
code{{font-family:var(--mono);font-size:12.5px;background:var(--surface-2);padding:2px 7px;border-radius:6px}}
.back{{display:inline-block;margin-bottom:18px;font-size:14px;text-decoration:none}}
.back:hover{{text-decoration:underline}}
.kv th{{width:220px;text-transform:none;letter-spacing:0;font-size:13px;color:var(--muted);font-weight:500;vertical-align:top}}
.kv td,.kv th{{border-bottom:1px solid var(--border)}}
.kv tr:last-child td,.kv tr:last-child th{{border-bottom:0}}
.login-wrap{{min-height:calc(100vh - 120px);display:flex;align-items:center;justify-content:center}}
.login-card{{width:100%;max-width:400px}}
@media (prefers-reduced-motion:reduce){{*{{transition:none!important}}}}
@media (max-width:820px){{.channel-grid{{grid-template-columns:repeat(3,minmax(0,1fr))}}}}
@media (max-width:720px){{main{{padding:22px 14px 48px}} header{{padding:10px 14px}} .card{{padding:16px}} .msg{{max-width:88%}} .channel-setup{{padding:18px 16px}} .channel-mode-grid{{grid-template-columns:1fr}} .channel-mode{{padding:16px 0 4px}} .channel-mode:first-child{{padding-top:0}} .channel-mode+ .channel-mode{{border-left:0;border-top:1px solid var(--border)}} .channel-mode .hint{{min-height:0}}}}
@media (max-width:520px){{.channel-grid{{grid-template-columns:repeat(2,minmax(0,1fr))}} .channel-tile{{min-height:104px}} .channel-heading{{align-items:flex-start;flex-direction:column;gap:2px}} .channel-meta{{grid-template-columns:1fr;gap:1px}} .channel-meta dd{{margin-bottom:7px}}}}
</style></head><body>
<header><span class="brand">Reply Core<small>Control Plane</small></span>{nav_bar}<nav>{logout}</nav></header>
<main>{body}</main></body></html>"""


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    csrf = _csrf(request)
    next_target = _safe_admin_next(request.query_params.get("next"))
    next_field = (
        f'<input type="hidden" name="next" value="{html.escape(next_target, quote=True)}">'
        if next_target
        else ""
    )
    response = HTMLResponse(
        _page(
            "管理员登录",
            f"""<div class="login-wrap"><section class="card login-card"><h1>管理员登录</h1>
<p class="hint">生产环境建议由身份感知代理或 OIDC/MFA 保护该入口。</p>
<form method="post" action="/admin/login"><input type="hidden" name="csrf_token" value="{csrf}">{next_field}
<label for="f-username">用户名</label><input id="f-username" name="username" autocomplete="username" required>
<label for="f-password">密码</label><input id="f-password" name="password" type="password" autocomplete="current-password" required>
<button type="submit" class="btn-block">登录</button></form></section></div>""",
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


@router.post("/login")
async def login(request: Request) -> Response:
    form = await _form(request)
    _require_csrf(request, form)
    next_target = _safe_admin_next(form.get("next"))
    username = (form.get("username") or "").strip()
    password = form.get("password") or ""
    retry_target = "/admin/login"
    if next_target:
        retry_target = f"{retry_target}?{urlencode({'next': next_target})}"
    retry_link = html.escape(retry_target, quote=True)
    if len(username) > 128 or len(password) > 128:
        return HTMLResponse(
            _page(
                "登录失败",
                f"""<div class="login-wrap"><section class="card login-card"><h1>登录失败</h1>
<p class="hint">用户名或密码错误。</p><p><a href="{retry_link}">返回重试</a></p></section></div>""",
                show_logout=False,
            ),
            401,
        )
    result = await authenticate(username, password)
    if result is None:
        return HTMLResponse(
            _page(
                "登录失败",
                f"""<div class="login-wrap"><section class="card login-card"><h1>登录失败</h1>
<p class="hint">用户名或密码错误。</p><p><a href="{retry_link}">返回重试</a></p></section></div>""",
                show_logout=False,
            ),
            401,
        )
    principal, raw_token = result
    if principal.must_change_password:
        target = "/admin/change-password"
        if next_target:
            target = f"{target}?{urlencode({'next': next_target})}"
    else:
        target = next_target or "/admin"
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


@router.get("/logout", response_class=HTMLResponse)
async def logout_page(request: Request) -> Response:
    principal = await _web_principal(request, allow_password_change=True)
    if isinstance(principal, Response):
        return principal
    csrf = _csrf(request)
    response = HTMLResponse(
        _page(
            "确认退出",
            f"""<div class="login-wrap"><section class="card login-card"><h1>确认退出</h1>
<p class="hint">退出将撤销当前后台会话。</p>
<form method="post" action="/admin/logout"><input type="hidden" name="csrf_token" value="{csrf}">
<button type="submit" class="btn-block">退出</button></form></section></div>""",
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


@router.post("/logout")
async def logout(request: Request) -> Response:
    form = await _form(request)
    _require_csrf(request, form)
    await revoke_session(request.cookies.get(_SESSION_COOKIE, ""))
    response = RedirectResponse("/admin/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(_SESSION_COOKIE)
    return response


@router.get("/change-password", response_class=HTMLResponse)
async def change_password_page(request: Request) -> Response:
    principal = await _web_principal(request, allow_password_change=True)
    if isinstance(principal, Response):
        return principal
    next_target = _safe_admin_next(request.query_params.get("next"))
    if principal.is_superadmin or not principal.must_change_password:
        return RedirectResponse(next_target or "/admin", status_code=status.HTTP_303_SEE_OTHER)
    csrf = _csrf(request)
    next_field = (
        f'<input type="hidden" name="next" value="{html.escape(next_target, quote=True)}">'
        if next_target
        else ""
    )
    response = HTMLResponse(
        _page(
            "首次修改密码",
            f"""<div class="login-wrap"><section class="card login-card"><h1>首次修改密码</h1>
<p class="hint">为保护账号，首次登录必须设置个人密码（12–128 个字符）。</p>
<form method="post" action="/admin/change-password"><input type="hidden" name="csrf_token" value="{csrf}">{next_field}
<label for="f-current-password">初始密码</label><input id="f-current-password" name="current_password" type="password" autocomplete="current-password" required>
<label for="f-new-password">新密码</label><input id="f-new-password" name="new_password" type="password" autocomplete="new-password" minlength="12" maxlength="128" required>
<label for="f-confirm-password">确认新密码</label><input id="f-confirm-password" name="confirm_password" type="password" autocomplete="new-password" minlength="12" maxlength="128" required>
<button type="submit" class="btn-block">保存新密码</button></form></section></div>""",
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


@router.post("/change-password")
async def change_password(request: Request) -> Response:
    principal = await _web_principal(request, allow_password_change=True)
    if isinstance(principal, Response):
        return principal
    if principal.is_superadmin or principal.user_id is None:
        raise HTTPException(status_code=403, detail="password_change_not_available")
    form = await _form(request)
    _require_csrf(request, form)
    next_target = _safe_admin_next(form.get("next"))
    new_password = form.get("new_password") or ""
    if new_password != (form.get("confirm_password") or ""):
        raise HTTPException(status_code=422, detail="password_confirmation_mismatch")
    async with get_session_factory()() as session:
        user = (
            await session.execute(
                select(models.AdminUser)
                .where(models.AdminUser.id == principal.user_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        current_password = form.get("current_password") or ""
        if user is None or not await verify_password(user.password_hash, current_password):
            raise HTTPException(status_code=401, detail="current_password_invalid")
        if secrets.compare_digest(new_password, current_password):
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
                detail={"first_login": True},
            )
        )
        await session.commit()
    response = RedirectResponse(next_target or "/admin", status_code=status.HTTP_303_SEE_OTHER)
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
) -> str:
    field_id = f"f-{name}-{secrets.token_hex(3)}"  # 同名字段出现在多个表单，id 需唯一
    required_attr = "required" if required else ""
    input_type = "password" if secret else "text"
    value_attr = f' value="{html.escape(value, quote=True)}"' if value else ""
    readonly_attr = "readonly" if readonly else ""
    return (
        f'<label for="{field_id}">{html.escape(label)}</label>'
        f'<input id="{field_id}" type="{input_type}" name="{name}"{value_attr} '
        f"{required_attr} {readonly_attr}>"
    )


_STATUS_TONES = {
    "active": "ok",
    "CONNECTED": "ok",
    "COMPLETED": "ok",
    "SENT": "ok",
    "READY": "ok",
    "ACTIVE": "ok",
    "published": "ok",
    "BOT_ACTIVE": "ok",
    "auto_reply": "ok",
    "HEALTHY": "ok",
    "WARNING": "warn",
    "ACTION": "err",
    "PENDING": "warn",
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
    "BOT_COOLDOWN": "neutral",
    "CLOSED": "neutral",
    "ignore": "neutral",
}


def _pill(status: str) -> str:
    tone = _STATUS_TONES.get(status, "neutral")
    return f'<span class="pill {tone}">{html.escape(status)}</span>'


async def _submit_form(
    request: Request, platform: str, form: dict[str, str] | None = None
) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    form = form or await _form(request)
    _require_csrf(request, form)
    tenant_id = (form.get("tenant_id") or "").strip()
    if not tenant_id:
        raise HTTPException(status_code=422, detail="tenant_id_required")
    principal.require_tenant(tenant_id)
    settings = get_settings()
    if not settings.platform_integration_enabled(platform):
        raise HTTPException(status_code=503, detail=f"{platform}_integration_disabled")
    if platform == "x" and (form.get("xchat_pin") or "").strip() and not settings.xchat_enabled:
        raise HTTPException(status_code=422, detail="xchat_disabled")
    brand_id = form.get("brand_id", "default") or "default"
    request_data, secrets_data = split_submission(platform, form)
    job_id = await submit_provisioning_job(
        tenant_id=tenant_id,
        brand_id=brand_id,
        platform=platform,
        actor=principal.actor,
        request=request_data,
        secrets=secrets_data,
        admin_session_id=principal.session_id,
    )
    from social_reply.application.account_management.actors import process_platform_provisioning
    from social_reply.application.account_management.jobs import process_provisioning_job

    await dispatch_actor(
        process_platform_provisioning,
        str(job_id),
        inline=lambda: process_provisioning_job(str(job_id)),
    )
    return RedirectResponse(f"/admin/jobs/{job_id}", status_code=status.HTTP_303_SEE_OTHER)


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


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
async def admin_job(request: Request, job_id: uuid.UUID) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    async with get_session_factory()() as session:
        job = await session.get(models.ProvisioningJob, job_id)
    if job is None or job.tenant_id not in principal.allowed_tenants:
        raise HTTPException(status_code=404, detail="provisioning_job_not_found")
    data = public_job(job)
    csrf = _csrf(request)
    in_flight = provisioning_job_is_in_flight(job)
    auto_retry = job.status == "FAILED" and in_flight
    if auto_retry:
        data["status"] = "PROCESSING"
    retry = ""
    if job.status in {"FAILED", "NEEDS_ACTION"} and requires_secret_resubmission(job):
        retry = (
            '<p class="muted">该任务的一次性凭证已清除。'
            '<a href="/admin/accounts">返回账号页重新提交 PIN 或凭证</a>。</p>'
        )
    elif job.status in {"FAILED", "NEEDS_ACTION"} and not auto_retry:
        retry = f"""<form method="post" action="/admin/jobs/{job.id}/retry" style="max-width:200px"><input type="hidden" name="csrf_token" value="{csrf}"><button>重试任务</button></form>"""
    refresh_note = '<p class="muted">任务运行中，页面每 4 秒自动刷新。</p>' if in_flight else ""
    rows = "".join(
        f"<tr><th scope='row'>{html.escape(str(key))}</th>"
        + (
            f"<td>{_pill(str(value))}</td>"
            if key == "status"
            else f"<td><code>{html.escape(str(value))}</code></td>"
        )
        + "</tr>"
        for key, value in data.items()
    )
    return HTMLResponse(
        _page(
            "Provisioning Job",
            f"""<a class="back" href="/admin/accounts">← 返回账号页</a>
<section class="card"><h1 style="font-size:24px">Provisioning Job</h1>{refresh_note}
<div class="tablewrap"><table class="kv">{rows}</table></div>{retry}</section>""",
            refresh_seconds=4 if in_flight else 0,
            active="accounts",
            show_users=principal.is_superadmin,
        )
    )


@router.post("/jobs/{job_id}/retry")
async def admin_retry_job(request: Request, job_id: uuid.UUID) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    async with get_session_factory()() as session:
        job = await session.get(models.ProvisioningJob, job_id)
    if job is None or job.tenant_id not in principal.allowed_tenants:
        raise HTTPException(status_code=404, detail="provisioning_job_not_found")
    if not get_settings().platform_integration_enabled(job.platform):
        raise HTTPException(status_code=503, detail=f"{job.platform}_integration_disabled")
    try:
        await retry_provisioning_job(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    from social_reply.application.account_management.actors import process_platform_provisioning
    from social_reply.application.account_management.jobs import process_provisioning_job

    await dispatch_actor(
        process_platform_provisioning,
        str(job_id),
        inline=lambda: process_provisioning_job(str(job_id)),
    )
    return RedirectResponse(f"/admin/jobs/{job_id}", status_code=status.HTTP_303_SEE_OTHER)
