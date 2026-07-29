"""Instagram Login OAuth for professional accounts without a Facebook Page."""

import logging
import secrets
from urllib.parse import quote, urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from redis.exceptions import RedisError

from social_reply.application.account_management.admin import (
    _form,
    _require_csrf,
    _web_principal,
)
from social_reply.application.account_management.jobs import submit_provisioning_job
from social_reply.application.account_management.meta_credentials import (
    instagram_app_credentials,
)
from social_reply.application.account_management.oauth.common import (
    admin_callback_url,
    notice,
    principal_from_oauth_context,
    store_oauth_state,
    take_oauth_state,
)
from social_reply.application.account_management.submissions import split_submission
from social_reply.connectors.meta.client import appsecret_proof
from social_reply.infrastructure.queue.dispatch import dispatch_actor
from social_reply.shared.config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin-oauth"])

_API_VERSION = "v23.0"
_BASE_SCOPES = "instagram_business_basic,instagram_business_manage_messages"
_COMMENT_SCOPE = "instagram_business_manage_comments"


def _oauth_scopes() -> str:
    scopes = _BASE_SCOPES.split(",")
    if get_settings().meta_comment_reply_enabled:
        scopes.append(_COMMENT_SCOPE)
    return ",".join(scopes)


def _instagram_client(**kwargs) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=15, **kwargs)


@router.post("/oauth/instagram/start")
async def instagram_oauth_start(request: Request) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    if not get_settings().instagram_messaging_enabled:
        return notice(
            "Instagram 集成已关闭", "当前环境未启用 Instagram Messaging。", status_code=503
        )
    tenant_id = (form.get("tenant_id") or "").strip()
    if not tenant_id:
        raise HTTPException(status_code=422, detail="tenant_id_required")
    principal.require_tenant(tenant_id)
    app = await instagram_app_credentials(tenant_id)
    if app is None:
        return notice(
            "无法发起授权",
            "请配置 INSTAGRAM_APP_ID、INSTAGRAM_APP_SECRET，以及 "
            "INSTAGRAM_VERIFY_TOKEN 或 META_VERIFY_TOKEN。",
            status_code=422,
        )

    state_token = secrets.token_urlsafe(32)
    try:
        await store_oauth_state(
            "instagram",
            state_token,
            {
                "tenant_id": tenant_id,
                "brand_id": (form.get("brand_id") or "default").strip() or "default",
                "session_id": str(principal.session_id),
            },
        )
    except (OSError, RedisError) as exc:
        logger.warning("instagram oauth state storage failed: %s", exc)
        return notice("发起授权失败", "OAuth 临时状态存储不可用。", status_code=503)

    url = "https://www.instagram.com/oauth/authorize?" + urlencode(
        {
            "enable_fb_login": "0",
            "client_id": app.app_id,
            "redirect_uri": admin_callback_url("/admin/oauth/instagram/callback"),
            "response_type": "code",
            "scope": _oauth_scopes(),
            "state": state_token,
        },
        quote_via=quote,
    )
    return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)


@router.get("/oauth/instagram/callback")
async def instagram_oauth_callback(request: Request) -> Response:
    state_token = request.query_params.get("state", "")
    if request.query_params.get("error"):
        if state_token:
            await take_oauth_state("instagram", state_token)
        return notice(
            "授权已取消",
            request.query_params.get("error_description") or "用户取消了授权。",
        )
    if not get_settings().instagram_messaging_enabled:
        return notice("Instagram 集成已关闭", "授权期间 Instagram 已被关闭。", status_code=503)
    context = await take_oauth_state("instagram", state_token) if state_token else None
    if context is None:
        return notice(
            "授权会话无效",
            "发起记录缺失、已使用或已过期，请重新发起。",
            status_code=400,
        )
    principal = await principal_from_oauth_context(context)
    if principal is None:
        return notice(
            "授权会话已失效",
            "管理员会话已退出、过期或失去 Tenant 权限，请重新登录并发起授权。",
            status_code=403,
        )
    code = request.query_params.get("code", "").removesuffix("#_")
    if not code:
        return notice("授权参数不完整", "请重新发起授权。", status_code=400)
    app = await instagram_app_credentials(context["tenant_id"])
    if app is None:
        return notice("无法完成授权", "Instagram App 凭证当前不可用。", status_code=422)

    redirect_uri = admin_callback_url("/admin/oauth/instagram/callback")
    try:
        async with _instagram_client() as client:
            short_response = await client.post(
                "https://api.instagram.com/oauth/access_token",
                data={
                    "client_id": app.app_id,
                    "client_secret": app.app_secret,
                    "grant_type": "authorization_code",
                    "redirect_uri": redirect_uri,
                    "code": code,
                },
            )
            short_response.raise_for_status()
            short = short_response.json()
            long_response = await client.get(
                "https://graph.instagram.com/access_token",
                params={
                    "grant_type": "ig_exchange_token",
                    "client_id": app.app_id,
                    "client_secret": app.app_secret,
                    "access_token": short["access_token"],
                },
            )
            long_response.raise_for_status()
            long_token = long_response.json()["access_token"]
            profile_response = await client.get(
                f"https://graph.instagram.com/{_API_VERSION}/me",
                params={
                    "fields": "user_id,username,name,profile_picture_url",
                    "access_token": long_token,
                    "appsecret_proof": appsecret_proof(long_token, app.app_secret),
                },
            )
            profile_response.raise_for_status()
            profile = profile_response.json()
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        status_code = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
        logger.warning(
            "instagram oauth exchange failed type=%s status=%s",
            exc.__class__.__name__,
            status_code,
        )
        return notice(
            "换取凭证失败",
            f"Instagram OAuth 交换失败（{exc.__class__.__name__}）。请检查回调地址、"
            "专业账号类型和 App 权限。",
            status_code=502,
        )

    principal = await principal_from_oauth_context(context)
    if principal is None:
        return notice(
            "授权会话已失效",
            "管理员会话在授权期间已退出、过期或失去 Tenant 权限，请重新发起。",
            status_code=403,
        )

    if not get_settings().instagram_messaging_enabled:
        return notice("Instagram 集成已关闭", "提交接入前 Instagram 已被关闭。", status_code=503)
    external_account_id = str(profile.get("user_id") or profile.get("id") or "")
    if not external_account_id:
        return notice("账号识别失败", "Instagram 未返回专业账号 ID。", status_code=502)
    username = str(profile.get("username") or "")
    settings = get_settings()
    enable_comments = settings.meta_comment_reply_enabled
    submission = {
        "name": f"@{username}" if username else str(profile.get("name") or external_account_id),
        "external_account_id": external_account_id,
        "app_id": app.app_id,
        "app_public_id": app.public_id,
        "api_version": _API_VERSION,
        "instagram_login_mode": "instagram_login",
        "enable_dm": True,
        "enable_comments": enable_comments,
        "automation_default": (
            "BOT_ACTIVE"
            if enable_comments and settings.meta_auto_reply_enabled
            else "BOT_DRAFT_ONLY"
        ),
        "access_token": long_token,
        "app_secret": app.app_secret,
        "verify_token": app.verify_token,
    }
    request_data, secrets_data = split_submission("instagram", submission)
    job_id = await submit_provisioning_job(
        tenant_id=context["tenant_id"],
        brand_id=context.get("brand_id", "default"),
        platform="instagram",
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
