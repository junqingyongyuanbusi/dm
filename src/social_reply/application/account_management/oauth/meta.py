"""Meta(Facebook Page / Instagram 专业账号)OAuth 2.0 授权接入。

一条 Facebook Login 流覆盖两个平台:dialog 授权 → code 换短期 user token →
fb_exchange_token 换长期 user token → GET /me/accounts 一次取回 Page id/name/
Page access token/关联 IG 账号。platform=facebook 落 Page 本身;
platform=instagram 只保留关联了 IG 专业账号的 Page,并以 IG 账号 id 落库
(入站 webhook entry.id 与发送端点用的都是它)。长期 user token 换出的
Page token 无固定过期时间,无需刷新任务。

App 凭证(app_id=external_app_id、app_secret、verify_token)取自 platform_apps
(platform_family=meta,与手工接入创建的是同一行),OAuth 账号与手工账号共享
webhook challenge 验证(verify_token 是 App 级的)。

多 Page 时渲染选择页:候选(含 Page token)Fernet 加密存 10 分钟一次性
cookie,浏览器端只见 Page 名称,token 不出服务端信任边界。

前提(Meta App 后台一次性):Facebook Login 产品已添加,Valid OAuth Redirect
URIs 登记 {PUBLIC_BASE_URL}/admin/oauth/meta/callback;App 未过审时仅
App 角色(管理员/开发者/测试员)可完成授权。
"""

import hashlib
import hmac
import json
import logging
import secrets as py_secrets
from dataclasses import dataclass
from urllib.parse import quote, urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from social_reply.application.account_management.admin import (
    _CSRF_COOKIE,
    _csrf,
    _current_actor,
    _form,
    _page,
    _require_csrf,
    _secure_cookie,
    html,
)
from social_reply.application.account_management.jobs import submit_provisioning_job
from social_reply.application.account_management.oauth.common import (
    admin_callback_url,
    notice,
    read_state,
    write_state,
)
from social_reply.application.account_management.submissions import split_submission
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.queue.dispatch import dispatch_actor
from social_reply.infrastructure.secret_crypto import decrypt_secret_bundle
from social_reply.shared.config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin-oauth"])

_STATE_COOKIE = "reply_meta_oauth_state"
_PICK_COOKIE = "reply_meta_oauth_pick"
_API_VERSION = "v23.0"
_DIALOG_URL = f"https://www.facebook.com/{_API_VERSION}/dialog/oauth"
_GRAPH_BASE = f"https://graph.facebook.com/{_API_VERSION}"
_SCOPES = {
    "facebook": "pages_show_list,pages_messaging,pages_manage_metadata,pages_read_engagement",
    "instagram": "pages_show_list,pages_manage_metadata,instagram_basic,instagram_manage_messages",
}


def _graph_client(**kwargs) -> httpx.AsyncClient:
    """工厂:测试经 monkeypatch 注入 MockTransport。"""
    return httpx.AsyncClient(base_url=_GRAPH_BASE, timeout=15, **kwargs)


@dataclass(frozen=True)
class _MetaApp:
    external_app_id: str
    app_secret: str
    verify_token: str
    public_id: str


async def _meta_app() -> _MetaApp | None:
    """取 Meta App 凭证:与手工接入共用 platform_apps(family=meta)同一行。"""
    async with get_session_factory()() as session:
        apps = (
            (
                await session.execute(
                    select(models.PlatformApp).where(
                        models.PlatformApp.platform_family == "meta",
                        models.PlatformApp.status == "active",
                    )
                )
            )
            .scalars()
            .all()
        )
    for app in apps:
        try:
            bundle = decrypt_secret_bundle(app.credential_bundle)
        except ValueError:
            logger.warning("platform_app %s has undecryptable credential bundle", app.id)
            continue
        app_secret = bundle.get("app_secret")
        verify_token = bundle.get("verify_token")
        if app.external_app_id and app_secret and verify_token:
            return _MetaApp(app.external_app_id, app_secret, verify_token, app.public_id)
    return None


def _proof(token: str, app_secret: str) -> str:
    """appsecret_proof:服务端 Graph 调用防 token 挪用(App 开启强制校验时必需)。"""
    return hmac.new(app_secret.encode(), token.encode(), hashlib.sha256).hexdigest()


@router.post("/oauth/meta/start")
async def meta_oauth_start(request: Request) -> Response:
    actor = _current_actor(request)
    if actor is None:
        return RedirectResponse("/admin/login", status_code=status.HTTP_303_SEE_OTHER)
    form = await _form(request)
    _require_csrf(request, form)
    platform = form.get("platform", "")
    if platform not in _SCOPES:
        raise HTTPException(status_code=422, detail="platform_must_be_facebook_or_instagram")
    tenant_id = (form.get("tenant_id") or "").strip()
    if not tenant_id:
        raise HTTPException(status_code=422, detail="tenant_id_required")
    if tenant_id not in get_settings().allowed_admin_tenants:
        raise HTTPException(status_code=403, detail="tenant_access_denied")
    brand_id = form.get("brand_id", "default") or "default"

    app = await _meta_app()
    if app is None:
        return notice(
            "无法发起授权",
            "未找到 Meta App 凭证:请先用手工表单接入一次(会创建 platform_apps 记录,"
            "含 App ID / App Secret / Verify Token),之后即可 OAuth 一键接入。",
            status_code=422,
        )
    state_token = py_secrets.token_urlsafe(24)
    dialog_url = (
        _DIALOG_URL
        + "?"
        + urlencode(
            {
                "client_id": app.external_app_id,
                "redirect_uri": admin_callback_url("/admin/oauth/meta/callback"),
                "state": state_token,
                "response_type": "code",
                "scope": _SCOPES[platform],
            },
            quote_via=quote,
        )
    )
    response = RedirectResponse(dialog_url, status_code=status.HTTP_303_SEE_OTHER)
    write_state(
        response,
        request,
        _STATE_COOKIE,
        {
            "state": state_token,
            "platform": platform,
            "tenant_id": tenant_id,
            "brand_id": brand_id,
            "actor": actor,
        },
    )
    return response


@router.get("/oauth/meta/callback")
async def meta_oauth_callback(request: Request) -> Response:
    if request.query_params.get("error"):
        return notice(
            "授权已取消",
            f"Meta 返回:{request.query_params.get('error_description') or '用户取消了授权'}。"
            "未做任何变更。",
        )
    state = read_state(request, _STATE_COOKIE)
    if state is None:
        return notice(
            "授权会话无效",
            "发起记录缺失、已过期(10 分钟)或校验失败,请回到账号页重新发起。",
            status_code=400,
        )
    code = request.query_params.get("code", "")
    query_state = request.query_params.get("state", "")
    if not code or not query_state or query_state != state.get("state"):
        return notice(
            "授权参数不匹配", "回调参数与发起记录不一致,请重新发起授权。", status_code=400
        )
    app = await _meta_app()
    if app is None:
        return notice("无法完成授权", "Meta App 凭证不可用。", status_code=422)

    redirect_uri = admin_callback_url("/admin/oauth/meta/callback")
    try:
        async with _graph_client() as client:
            short_response = await client.get(
                "/oauth/access_token",
                params={
                    "client_id": app.external_app_id,
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
                    "client_id": app.external_app_id,
                    "client_secret": app.app_secret,
                    "fb_exchange_token": short_token,
                },
            )
            long_response.raise_for_status()
            long_token = long_response.json()["access_token"]
            pages_response = await client.get(
                "/me/accounts",
                params={
                    "fields": "id,name,access_token,instagram_business_account{id,username}",
                    "access_token": long_token,
                    "appsecret_proof": _proof(long_token, app.app_secret),
                    "limit": "100",
                },
            )
            pages_response.raise_for_status()
            pages = pages_response.json().get("data", [])
    except Exception as exc:  # noqa: BLE001 - code 一次性,失败只能重新发起
        logger.warning("meta oauth exchange failed: %s", exc)
        return notice(
            "换取凭证失败",
            f"与 Meta 交换 token 失败({exc.__class__.__name__})。常见原因:App 后台"
            f"未登记回调 URL {redirect_uri},或授权账号不是 App 角色(开发模式限制)。",
            status_code=502,
        )

    platform = state["platform"]
    candidates = [
        {
            "id": str(page.get("id", "")),
            "name": str(page.get("name", "")),
            "access_token": str(page.get("access_token", "")),
            "ig_id": str((page.get("instagram_business_account") or {}).get("id", "")),
            "ig_username": str((page.get("instagram_business_account") or {}).get("username", "")),
        }
        for page in pages
        if page.get("id") and page.get("access_token")
    ]
    if platform == "instagram":
        candidates = [candidate for candidate in candidates if candidate["ig_id"]]
    if not candidates:
        return notice(
            "没有可接入的目标",
            (
                "该 Meta 账号名下没有关联 Instagram 专业账号的 Page。请先在 Instagram "
                "把账号转为专业账号并关联 Facebook Page。"
                if platform == "instagram"
                else "该 Meta 账号名下没有可管理的 Facebook Page。"
            ),
            status_code=422,
        )
    if len(candidates) == 1:
        return await _finalize(candidates[0], state, app)

    # 多个候选:渲染选择页;候选(含 Page token)加密进一次性 cookie,不出服务端
    csrf = _csrf(request)
    rows = "".join(
        f'<label style="display:block;margin:8px 0"><input type="radio" name="choice" '
        f'value="{index}" required> {html.escape(candidate["name"])} '
        f'<span class="muted">(Page {html.escape(candidate["id"])}'
        + (f" · IG @{html.escape(candidate['ig_username'])}" if candidate["ig_id"] else "")
        + ")</span></label>"
        for index, candidate in enumerate(candidates)
    )
    target_label = "Instagram 账号" if platform == "instagram" else "Facebook Page"
    body = f"""<a class="back" href="/admin/accounts">← 返回账号页</a>
<section class="card"><h1 style="font-size:24px">选择要接入的 {target_label}</h1>
<form method="post" action="/admin/oauth/meta/select">
<input type="hidden" name="csrf_token" value="{csrf}">{rows}
<button class="btn-block">接入所选</button></form></section>"""
    response = HTMLResponse(_page("选择接入目标", body, active="accounts"))
    write_state(
        response,
        request,
        _PICK_COOKIE,
        {
            "candidates": json.dumps(candidates, ensure_ascii=False),
            "platform": platform,
            "tenant_id": state["tenant_id"],
            "brand_id": state.get("brand_id", "default"),
            "actor": state.get("actor", ""),
        },
    )
    if not request.cookies.get(_CSRF_COOKIE):
        response.set_cookie(
            _CSRF_COOKIE, csrf, httponly=False, samesite="strict", secure=_secure_cookie(request)
        )
    response.delete_cookie(_STATE_COOKIE)
    return response


@router.post("/oauth/meta/select")
async def meta_oauth_select(request: Request) -> Response:
    if _current_actor(request) is None:
        return RedirectResponse("/admin/login", status_code=status.HTTP_303_SEE_OTHER)
    form = await _form(request)
    _require_csrf(request, form)
    pick = read_state(request, _PICK_COOKIE)
    if pick is None:
        return notice(
            "选择会话无效",
            "候选记录缺失或已过期(10 分钟),请回到账号页重新发起授权。",
            status_code=400,
        )
    try:
        candidates = json.loads(pick["candidates"])
        candidate = candidates[int(form.get("choice", ""))]
    except (KeyError, ValueError, IndexError):
        return notice("选择无效", "所选目标不存在,请重新发起授权。", status_code=400)
    app = await _meta_app()
    if app is None:
        return notice("无法完成接入", "Meta App 凭证不可用。", status_code=422)
    return await _finalize(candidate, pick, app)


async def _finalize(candidate: dict, context: dict[str, str], app: _MetaApp) -> Response:
    """组装与手工 Meta 表单同构的提交,复用 provisioning 管道落库。"""
    platform = context["platform"]
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
        "app_id": app.external_app_id,
        "app_public_id": app.public_id,
        "api_version": _API_VERSION,
        "access_token": candidate["access_token"],
        "app_secret": app.app_secret,
        "verify_token": app.verify_token,
    }
    request_data, secrets_data = split_submission(platform, submission)
    job_id = await submit_provisioning_job(
        tenant_id=context["tenant_id"],
        brand_id=context.get("brand_id", "default"),
        platform=platform,
        actor=context.get("actor") or "user:oauth",
        request=request_data,
        secrets=secrets_data,
    )
    from social_reply.application.account_management.actors import process_platform_provisioning
    from social_reply.application.account_management.jobs import process_provisioning_job

    await dispatch_actor(
        process_platform_provisioning,
        str(job_id),
        inline=lambda: process_provisioning_job(str(job_id)),
    )
    response = RedirectResponse(f"/admin/jobs/{job_id}", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(_STATE_COOKIE)
    response.delete_cookie(_PICK_COOKIE)
    return response
