"""Facebook Page and Facebook-connected Instagram OAuth account connection."""

import hashlib
import hmac
import logging
import secrets
from urllib.parse import quote, urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from redis.exceptions import RedisError

from social_reply.application.account_management.admin import (
    _CSRF_COOKIE,
    _csrf,
    _form,
    _page,
    _require_csrf,
    _secure_cookie,
    _web_principal,
    html,
)
from social_reply.application.account_management.auth import Principal
from social_reply.application.account_management.jobs import submit_provisioning_job
from social_reply.application.account_management.meta_credentials import (
    MetaAppCredentials,
    facebook_app_credentials,
)
from social_reply.application.account_management.oauth.common import (
    admin_callback_url,
    notice,
    peek_oauth_state,
    principal_from_oauth_context,
    store_oauth_state,
    take_oauth_state,
)
from social_reply.application.account_management.submissions import split_submission
from social_reply.infrastructure.queue.dispatch import dispatch_actor
from social_reply.shared.config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin-oauth"])

_STATE_COOKIE = "reply_meta_oauth_state"
_PICK_COOKIE = "reply_meta_oauth_pick"
_API_VERSION = "v23.0"
_DIALOG_URL = f"https://www.facebook.com/{_API_VERSION}/dialog/oauth"
_GRAPH_BASE = f"https://graph.facebook.com/{_API_VERSION}"
_SCOPES = {
    "facebook": "pages_show_list,pages_messaging,pages_manage_metadata",
    "instagram": (
        "pages_show_list,pages_manage_metadata,instagram_basic,instagram_manage_messages"
    ),
}


def _graph_client(**kwargs) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=_GRAPH_BASE, timeout=15, **kwargs)


def _proof(token: str, app_secret: str) -> str:
    return hmac.new(app_secret.encode(), token.encode(), hashlib.sha256).hexdigest()


async def _exchange_code(*, app: MetaAppCredentials, code: str, redirect_uri: str) -> list[dict]:
    async with _graph_client() as client:
        short_response = await client.get(
            "/oauth/access_token",
            params={
                "client_id": app.app_id,
                "client_secret": app.app_secret,
                "redirect_uri": redirect_uri,
                "code": code,
            },
        )
        short_response.raise_for_status()
        short_token = short_response.json()["access_token"]
        long_response = await client.get(
            "/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": app.app_id,
                "client_secret": app.app_secret,
                "fb_exchange_token": short_token,
            },
        )
        long_response.raise_for_status()
        long_token = long_response.json()["access_token"]
        return await _fetch_pages(client, long_token, app.app_secret)


async def _fetch_pages(client: httpx.AsyncClient, user_token: str, app_secret: str) -> list[dict]:
    params = {
        "fields": "id,name,access_token,instagram_business_account{id,username}",
        "access_token": user_token,
        "appsecret_proof": _proof(user_token, app_secret),
        "limit": "100",
    }
    response = await client.get("/me/accounts", params=params)
    response.raise_for_status()
    payload = response.json()
    pages = list(payload.get("data") or [])
    next_url = (payload.get("paging") or {}).get("next")
    while next_url:
        response = await client.get(next_url)
        response.raise_for_status()
        payload = response.json()
        pages.extend(payload.get("data") or [])
        next_url = (payload.get("paging") or {}).get("next")
    return pages


@router.post("/oauth/meta/start")
async def meta_oauth_start(request: Request) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    platform = form.get("platform", "")
    if platform not in _SCOPES:
        raise HTTPException(status_code=422, detail="platform_must_be_facebook_or_instagram")
    if not get_settings().platform_integration_enabled(platform):
        return notice("平台集成已关闭", f"当前环境未启用 {platform}。", status_code=503)
    tenant_id = (form.get("tenant_id") or "").strip()
    if not tenant_id:
        raise HTTPException(status_code=422, detail="tenant_id_required")
    principal.require_tenant(tenant_id)

    app = await facebook_app_credentials(tenant_id)
    if app is None:
        return notice(
            "无法发起授权",
            "未找到 Meta App 凭证。请配置 FACEBOOK_APP_ID、FACEBOOK_APP_SECRET 和 "
            "META_VERIFY_TOKEN，或保留一个旧版 Meta PlatformApp。",
            status_code=422,
        )
    state_token = secrets.token_urlsafe(32)
    try:
        await store_oauth_state(
            "meta",
            state_token,
            {
                "platform": platform,
                "tenant_id": tenant_id,
                "brand_id": (form.get("brand_id") or "default").strip() or "default",
                "session_id": str(principal.session_id),
            },
        )
    except (OSError, RedisError) as exc:
        logger.warning("meta oauth state storage failed: %s", exc)
        return notice("发起授权失败", "OAuth 临时状态存储不可用，请稍后重试。", status_code=503)

    dialog_url = (
        _DIALOG_URL
        + "?"
        + urlencode(
            {
                "client_id": app.app_id,
                "redirect_uri": admin_callback_url("/admin/oauth/meta/callback"),
                "state": state_token,
                "response_type": "code",
                "scope": _SCOPES[platform],
            },
            quote_via=quote,
        )
    )
    return RedirectResponse(dialog_url, status_code=status.HTTP_303_SEE_OTHER)


@router.get("/oauth/meta/callback")
async def meta_oauth_callback(request: Request) -> Response:
    state_token = request.query_params.get("state", "")
    if request.query_params.get("error"):
        if state_token:
            await take_oauth_state("meta", state_token)
        return notice(
            "授权已取消",
            f"Meta 返回：{request.query_params.get('error_description') or '用户取消了授权'}。",
        )
    pending_state = await peek_oauth_state("meta", state_token) if state_token else None
    if pending_state is None:
        return notice(
            "授权会话无效",
            "发起记录缺失、已使用或已过期，请回到账号页重新发起。",
            status_code=400,
        )
    if not get_settings().platform_integration_enabled(str(pending_state.get("platform") or "")):
        return notice("平台集成已关闭", "授权期间该平台已被关闭，请稍后重试。", status_code=503)
    state = await take_oauth_state("meta", state_token)
    if state is None:
        return notice(
            "授权会话无效",
            "发起记录已由另一请求使用，请回到账号页重新发起。",
            status_code=400,
        )
    principal = await principal_from_oauth_context(state)
    if principal is None:
        return notice(
            "授权会话已失效",
            "管理员会话已退出、过期或失去 Tenant 权限，请重新登录并发起授权。",
            status_code=403,
        )
    code = request.query_params.get("code", "")
    if not code:
        return notice("授权参数不完整", "请重新发起授权。", status_code=400)
    app = await facebook_app_credentials(state["tenant_id"])
    if app is None:
        return notice("无法完成授权", "Facebook App 凭证当前不可用。", status_code=422)

    redirect_uri = admin_callback_url("/admin/oauth/meta/callback")
    try:
        pages = await _exchange_code(
            app=app,
            code=code,
            redirect_uri=redirect_uri,
        )
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        status_code = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
        logger.warning(
            "meta oauth exchange failed type=%s status=%s",
            exc.__class__.__name__,
            status_code,
        )
        return notice(
            "换取凭证失败",
            f"与 Meta 交换 Token 失败（{exc.__class__.__name__}）。请检查回调地址、"
            "权限和 App Review 状态。",
            status_code=502,
        )

    principal = await principal_from_oauth_context(state)
    if principal is None:
        return notice(
            "授权会话已失效",
            "管理员会话在授权期间已退出、过期或失去 Tenant 权限，请重新发起。",
            status_code=403,
        )

    candidates = _candidates(pages, state["platform"])
    if not candidates:
        return notice(
            "没有可接入的目标",
            (
                "没有找到关联 Facebook Page 的 Instagram 专业账号。"
                if state["platform"] == "instagram"
                else "没有找到当前用户可管理的 Facebook Page。"
            ),
            status_code=422,
        )
    if len(candidates) == 1:
        return await _finalize(candidates[0], state, app, principal)
    return await _picker(request, candidates, state)


def _candidates(pages: list[dict], platform: str) -> list[dict[str, str]]:
    candidates = [
        {
            "id": str(page.get("id") or ""),
            "name": str(page.get("name") or ""),
            "access_token": str(page.get("access_token") or ""),
            "ig_id": str((page.get("instagram_business_account") or {}).get("id") or ""),
            "ig_username": str(
                (page.get("instagram_business_account") or {}).get("username") or ""
            ),
        }
        for page in pages
        if page.get("id") and page.get("access_token")
    ]
    if platform == "instagram":
        return [candidate for candidate in candidates if candidate["ig_id"]]
    return candidates


async def _picker(request: Request, candidates: list[dict], context: dict) -> Response:
    pick_token = secrets.token_urlsafe(32)
    try:
        await store_oauth_state(
            "meta-pick",
            pick_token,
            {"candidates": candidates, **context},
        )
    except (OSError, RedisError) as exc:
        logger.warning("meta picker state storage failed: %s", exc)
        return notice("无法显示账号列表", "OAuth 临时状态存储不可用。", status_code=503)

    csrf = _csrf(request)
    rows = "".join(
        f'<label style="display:block;margin:8px 0"><input type="radio" name="choice" '
        f'value="{index}" required> {html.escape(candidate["name"])} '
        f'<span class="muted">(Page {html.escape(candidate["id"])}'
        + (f" · IG @{html.escape(candidate['ig_username'])}" if candidate["ig_id"] else "")
        + ")</span></label>"
        for index, candidate in enumerate(candidates)
    )
    label = "Instagram 账号" if context["platform"] == "instagram" else "Facebook Page"
    body = f"""<a class="back" href="/admin/accounts">← 返回账号页</a>
<section class="card"><h1 style="font-size:24px">选择要接入的 {label}</h1>
<form method="post" action="/admin/oauth/meta/select">
<input type="hidden" name="csrf_token" value="{csrf}">
<input type="hidden" name="pick_token" value="{pick_token}">{rows}
<button class="btn-block">接入所选</button></form></section>"""
    response = HTMLResponse(_page("选择接入目标", body, active="accounts"))
    response.set_cookie(
        _PICK_COOKIE,
        pick_token,
        max_age=600,
        httponly=True,
        samesite="strict",
        secure=_secure_cookie(request),
    )
    if not request.cookies.get(_CSRF_COOKIE):
        response.set_cookie(
            _CSRF_COOKIE,
            csrf,
            httponly=False,
            samesite="strict",
            secure=_secure_cookie(request),
        )
    return response


@router.post("/oauth/meta/select")
async def meta_oauth_select(request: Request) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    pick_token = form.get("pick_token", "") or request.cookies.get(_PICK_COOKIE, "")
    pending_pick = await peek_oauth_state("meta-pick", pick_token)
    if pending_pick is None:
        return notice("选择会话无效", "候选记录已使用或过期，请重新授权。", status_code=400)
    if str(principal.session_id) != str(pending_pick.get("session_id")):
        return notice("选择会话无效", "请使用发起授权的同一登录会话完成选择。", status_code=403)
    principal.require_tenant(str(pending_pick.get("tenant_id") or ""))
    if not get_settings().platform_integration_enabled(str(pending_pick.get("platform") or "")):
        return notice("平台集成已关闭", "选择期间该平台已被关闭，请稍后重试。", status_code=503)
    try:
        choice = int(form.get("choice", ""))
        pending_pick["candidates"][choice]
    except (KeyError, TypeError, ValueError, IndexError):
        return notice("选择无效", "所选目标不存在，请重新选择。", status_code=400)
    app = await facebook_app_credentials(pending_pick["tenant_id"])
    if app is None:
        return notice("无法完成接入", "Facebook App 凭证当前不可用。", status_code=422)
    pick = await take_oauth_state("meta-pick", pick_token)
    if pick is None:
        return notice("选择会话无效", "候选记录已由另一请求使用，请重新授权。", status_code=400)
    try:
        candidate = pick["candidates"][choice]
    except (KeyError, TypeError, IndexError):
        return notice("选择会话无效", "候选记录发生变化，请重新授权。", status_code=400)
    response = await _finalize(candidate, pick, app, principal)
    response.delete_cookie(_PICK_COOKIE)
    return response


async def _finalize(
    candidate: dict,
    context: dict,
    app: MetaAppCredentials,
    principal: Principal,
) -> Response:
    platform = context["platform"]
    if not get_settings().platform_integration_enabled(platform):
        return notice("平台集成已关闭", "提交接入前该平台已被关闭。", status_code=503)
    if platform == "instagram":
        external_account_id = candidate["ig_id"]
        display_name = (
            f"@{candidate['ig_username']}" if candidate["ig_username"] else candidate["name"]
        )
    else:
        external_account_id = candidate["id"]
        display_name = candidate["name"]
    submission = {
        "name": display_name,
        "external_account_id": external_account_id,
        "app_id": app.app_id,
        "app_public_id": app.public_id,
        "api_version": _API_VERSION,
        "instagram_login_mode": "facebook_login",
        "page_id": candidate["id"],
        "enable_dm": True,
        "enable_comments": False,
        "automation_default": "BOT_DRAFT_ONLY",
        "access_token": candidate["access_token"],
        "app_secret": app.app_secret,
        "verify_token": app.verify_token,
    }
    request_data, secrets_data = split_submission(platform, submission)
    job_id = await submit_provisioning_job(
        tenant_id=context["tenant_id"],
        brand_id=context.get("brand_id", "default"),
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
