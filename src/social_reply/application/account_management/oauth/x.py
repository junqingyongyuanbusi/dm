"""Connect X accounts with the same OAuth 1.0a model used by Postiz.

``X_API_KEY`` and ``X_API_SECRET`` identify one deployment-level X App. Each
successful three-legged OAuth flow yields a different user access-token pair,
which is validated and encrypted through the normal provisioning pipeline.
Repeating the flow connects another X account; reconnecting the same account
updates the existing database row by external account ID.
"""

import json
import logging
from urllib.parse import parse_qsl, urlencode

import httpx
import redis.asyncio as aioredis
from authlib.oauth1 import ClientAuth
from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from redis.exceptions import RedisError

from social_reply.application.account_management.admin import (
    _current_actor,
    _form,
    _require_csrf,
)
from social_reply.application.account_management.jobs import submit_provisioning_job
from social_reply.application.account_management.oauth.common import (
    STATE_TTL_SECONDS,
    admin_callback_url,
    notice,
)
from social_reply.application.account_management.submissions import split_submission
from social_reply.application.account_management.x_app import x_app_credentials
from social_reply.infrastructure.queue.dispatch import dispatch_actor
from social_reply.shared.config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin-oauth"])

_X_OAUTH_BASE = "https://api.x.com"
_STATE_PREFIX = "oauth:x:"


def _oauth1_auth(**kwargs) -> ClientAuth:
    return ClientAuth(signature_type="HEADER", **kwargs)


def _redis():
    return aioredis.from_url(get_settings().redis_url)


def _http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=15)


async def _request_token(
    *, consumer_key: str, consumer_secret: str, callback_url: str
) -> dict[str, str]:
    auth = _oauth1_auth(
        client_id=consumer_key,
        client_secret=consumer_secret,
        redirect_uri=callback_url,
    )
    body = urlencode({"x_auth_access_type": "write"})
    _url, headers, _body = auth.prepare(
        "POST",
        f"{_X_OAUTH_BASE}/oauth/request_token",
        {"Content-Type": "application/x-www-form-urlencoded"},
        body,
    )
    async with _http_client() as client:
        response = await client.post(
            f"{_X_OAUTH_BASE}/oauth/request_token",
            content=body,
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


async def _store_state(oauth_token: str, state: dict[str, str]) -> None:
    redis = _redis()
    try:
        await redis.set(
            f"{_STATE_PREFIX}{oauth_token}",
            json.dumps(state, separators=(",", ":")),
            ex=STATE_TTL_SECONDS,
        )
    finally:
        await redis.aclose()


async def _take_state(oauth_token: str) -> dict[str, str] | None:
    redis = _redis()
    key = f"{_STATE_PREFIX}{oauth_token}"
    try:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.get(key)
            pipe.delete(key)
            value, _deleted = await pipe.execute()
    finally:
        await redis.aclose()
    if value is None:
        return None
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


@router.post("/oauth/x/start")
async def x_oauth_start(request: Request) -> Response:
    actor = _current_actor(request)
    if actor is None:
        return RedirectResponse("/admin/login", status_code=status.HTTP_303_SEE_OTHER)
    form = await _form(request)
    _require_csrf(request, form)
    tenant_id = (form.get("tenant_id") or "").strip()
    if not tenant_id:
        raise HTTPException(status_code=422, detail="tenant_id_required")
    if tenant_id not in get_settings().allowed_admin_tenants:
        raise HTTPException(status_code=403, detail="tenant_access_denied")

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
        logger.warning("x oauth request token failed: %s", exc)
        return notice(
            "发起授权失败",
            f"X OAuth request token 失败（{exc.__class__.__name__}）。请检查 X App 的 "
            f"Read and Write 权限、Native App 类型及回调地址 {callback_url}。",
            status_code=502,
        )
    try:
        await _store_state(
            token["oauth_token"],
            {
                "request_token_secret": token["oauth_token_secret"],
                "tenant_id": tenant_id,
                "brand_id": (form.get("brand_id") or "default").strip() or "default",
                "actor": actor,
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
        await _take_state(denied)
        return notice("授权已取消", "你在 X 上取消了授权，未做任何变更。")

    oauth_token = request.query_params.get("oauth_token", "")
    verifier = request.query_params.get("oauth_verifier", "")
    if not oauth_token or not verifier:
        return notice("授权参数不完整", "请回到账号页重新发起授权。", status_code=400)
    state = await _take_state(oauth_token)
    if state is None:
        return notice(
            "授权会话无效",
            "发起记录缺失、已使用或已过期，请回到账号页重新发起。",
            status_code=400,
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
        logger.warning("x oauth access token failed: %s", exc)
        return notice(
            "换取凭证失败",
            f"X OAuth access token 失败（{exc.__class__.__name__}），请重新授权。",
            status_code=502,
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
        actor=state.get("actor") or "user:oauth",
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
    return RedirectResponse(f"/admin/jobs/{job_id}", status_code=status.HTTP_303_SEE_OTHER)
