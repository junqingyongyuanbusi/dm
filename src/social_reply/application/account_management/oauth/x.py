"""Connect X accounts with the same OAuth 1.0a model used by Postiz.

``X_API_KEY`` and ``X_API_SECRET`` identify one deployment-level X App. Each
successful three-legged OAuth flow yields a different user access-token pair,
which is validated and encrypted through the normal provisioning pipeline.
Repeating the flow connects another X account; reconnecting the same account
updates the existing database row by external account ID.
"""

import logging
import re
from urllib.parse import parse_qsl, urlencode

import httpx
from authlib.oauth1 import ClientAuth
from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from redis.exceptions import RedisError

from social_reply.application.account_management.admin import (
    _form,
    _require_csrf,
    _web_principal,
)
from social_reply.application.account_management.jobs import submit_provisioning_job
from social_reply.application.account_management.oauth.common import (
    admin_callback_url,
    notice,
    principal_from_oauth_context,
    store_oauth_state,
    take_oauth_state,
)
from social_reply.application.account_management.submissions import split_submission
from social_reply.application.account_management.x_app import x_app_credentials
from social_reply.infrastructure.queue.dispatch import dispatch_actor

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin-oauth"])

_X_OAUTH_BASE = "https://api.x.com"


def _oauth1_auth(**kwargs) -> ClientAuth:
    return ClientAuth(signature_type="HEADER", **kwargs)


def _http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=15)


def _x_error_detail(exc: Exception) -> str:
    if not isinstance(exc, httpx.HTTPStatusError):
        return ""
    body = exc.response.text.strip()
    match = re.search(r"<error\b[^>]*>(.*?)</error>", body, flags=re.DOTALL)
    return (match.group(1) if match else body)[:300]


async def _request_token(
    *, consumer_key: str, consumer_secret: str, callback_url: str
) -> dict[str, str]:
    auth = _oauth1_auth(
        client_id=consumer_key,
        client_secret=consumer_secret,
        redirect_uri=callback_url,
    )
    body = urlencode({"x_auth_access_type": "write"})
    signed_url, headers, signed_body = auth.prepare(
        "POST",
        f"{_X_OAUTH_BASE}/oauth/request_token",
        {"Content-Type": "application/x-www-form-urlencoded"},
        body,
    )
    async with _http_client() as client:
        response = await client.post(
            signed_url,
            content=signed_body,
            headers=headers,
        )
    response.raise_for_status()
    result = dict(parse_qsl(response.text, keep_blank_values=True))
    if result.get("oauth_callback_confirmed") != "true":
        raise ValueError("x_oauth_callback_not_confirmed")
    if not result.get("oauth_token") or not result.get("oauth_token_secret"):
        raise ValueError("x_oauth_request_token_incomplete")
    return result


async def _access_token(
    *,
    consumer_key: str,
    consumer_secret: str,
    request_token: str,
    request_token_secret: str,
    verifier: str,
) -> dict[str, str]:
    auth = _oauth1_auth(
        client_id=consumer_key,
        client_secret=consumer_secret,
        token=request_token,
        token_secret=request_token_secret,
        verifier=verifier,
    )
    _url, headers, _body = auth.prepare(
        "POST",
        f"{_X_OAUTH_BASE}/oauth/access_token",
        {},
        None,
    )
    async with _http_client() as client:
        response = await client.post(
            f"{_X_OAUTH_BASE}/oauth/access_token",
            headers=headers,
        )
    response.raise_for_status()
    result = dict(parse_qsl(response.text, keep_blank_values=True))
    if not result.get("oauth_token") or not result.get("oauth_token_secret"):
        raise ValueError("x_oauth_access_token_incomplete")
    return result


@router.post("/oauth/x/start")
async def x_oauth_start(request: Request) -> Response:
    principal = await _web_principal(request)
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    tenant_id = (form.get("tenant_id") or "").strip()
    if not tenant_id:
        raise HTTPException(status_code=422, detail="tenant_id_required")
    principal.require_tenant(tenant_id)

    credentials = x_app_credentials()
    if credentials is None:
        return notice(
            "无法发起授权",
            "请先为 API、Worker 和 Scheduler 配置 X_API_KEY 与 X_API_SECRET。",
            status_code=422,
        )
    consumer_key, consumer_secret = credentials
    callback_url = admin_callback_url("/admin/oauth/x/callback")
    try:
        token = await _request_token(
            consumer_key=consumer_key,
            consumer_secret=consumer_secret,
            callback_url=callback_url,
        )
    except (httpx.HTTPError, ValueError) as exc:
        detail = _x_error_detail(exc)
        logger.warning("x oauth request token failed: %s body=%s", exc, detail)
        return notice(
            "发起授权失败",
            f"X OAuth request token 失败（{exc.__class__.__name__}"
            f"{f': {detail}' if detail else ''}）。请检查 X App 的 Read and Write 权限、"
            f"Web App 类型及回调地址 {callback_url}。",
            status_code=502,
        )
    try:
        await store_oauth_state(
            "x",
            token["oauth_token"],
            {
                "request_token_secret": token["oauth_token_secret"],
                "tenant_id": tenant_id,
                "brand_id": (form.get("brand_id") or "default").strip() or "default",
                "session_id": str(principal.session_id),
                "xchat_pin": form.get("xchat_pin", ""),
            },
        )
    except (OSError, RedisError) as exc:
        logger.warning("x oauth state storage failed: %s", exc)
        return notice("发起授权失败", "OAuth 临时状态存储不可用，请稍后重试。", status_code=503)

    return RedirectResponse(
        f"{_X_OAUTH_BASE}/oauth/authorize?{urlencode({'oauth_token': token['oauth_token']})}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/oauth/x/callback")
async def x_oauth_callback(request: Request) -> Response:
    denied = request.query_params.get("denied", "")
    if denied:
        await take_oauth_state("x", denied)
        return notice("授权已取消", "你在 X 上取消了授权，未做任何变更。")

    oauth_token = request.query_params.get("oauth_token", "")
    verifier = request.query_params.get("oauth_verifier", "")
    if not oauth_token or not verifier:
        return notice("授权参数不完整", "请回到账号页重新发起授权。", status_code=400)
    state = await take_oauth_state("x", oauth_token)
    if state is None:
        return notice(
            "授权会话无效",
            "发起记录缺失、已使用或已过期，请回到账号页重新发起。",
            status_code=400,
        )
    principal = await principal_from_oauth_context(state)
    if principal is None:
        return notice(
            "授权会话已失效",
            "管理员会话已退出、过期或失去 Tenant 权限，请重新登录并发起授权。",
            status_code=403,
        )

    credentials = x_app_credentials()
    if credentials is None:
        return notice("无法完成授权", "X_API_KEY/X_API_SECRET 当前不可用。", status_code=422)
    consumer_key, consumer_secret = credentials
    try:
        token = await _access_token(
            consumer_key=consumer_key,
            consumer_secret=consumer_secret,
            request_token=oauth_token,
            request_token_secret=state["request_token_secret"],
            verifier=verifier,
        )
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        status_code = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
        logger.warning(
            "x oauth access token failed type=%s status=%s",
            exc.__class__.__name__,
            status_code,
        )
        return notice(
            "换取凭证失败",
            f"X OAuth access token 失败（{exc.__class__.__name__}），请重新授权。",
            status_code=502,
        )

    principal = await principal_from_oauth_context(state)
    if principal is None:
        return notice(
            "授权会话已失效",
            "管理员会话在授权期间已退出、过期或失去 Tenant 权限，请重新发起。",
            status_code=403,
        )

    screen_name = token.get("screen_name", "")
    submission = {
        "name": f"@{screen_name}" if screen_name else "x-oauth",
        "environment": "oauth",
        "consumer_key": consumer_key,
        "consumer_secret": consumer_secret,
        "access_token": token["oauth_token"],
        "access_token_secret": token["oauth_token_secret"],
    }
    if state.get("xchat_pin"):
        submission["xchat_pin"] = state["xchat_pin"]
    request_data, secrets_data = split_submission("x", submission)
    job_id = await submit_provisioning_job(
        tenant_id=state["tenant_id"],
        brand_id=state.get("brand_id", "default"),
        platform="x",
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
