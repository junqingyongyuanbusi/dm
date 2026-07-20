"""X OAuth 1.0a 三步授权接入:后台一键连接新 X 账号,凭证不经人手。

流程:账号页发起(POST /admin/oauth/x/start)→ 跳转 X 授权页(用要接入的账号
登录并授权)→ X 回调(GET /admin/oauth/x/callback)→ 换取该账号的 access token
四元组 → 组装成与手工表单同构的提交,复用 provisioning 管道(凭证校验、加密
落库、webhook 订阅、审计与任务进度页全部共用,OAuth 层只负责"授权换凭证")。

中间态(request token secret + 发起上下文)经 Fernet 加密存 10 分钟 cookie:
callback 由 X 302 跳回,属跨站顶级导航,admin 会话 cookie(SameSite=Strict)
不随行,因此 callback 的授权凭据是这枚加密 state cookie 本身(内含发起者身份
与租户上下文),SameSite=Lax、一次性、绑定 oauth_token,不依赖登录会话。

前提(需在 X 开发者后台完成一次):App 开启 User authentication,登记回调
URL {PUBLIC_BASE_URL}/admin/oauth/x/callback;App 类型选 Native App(选
Web App/Automated App or Bot 会导致 OAuth1 签名校验失败 error 32)。
"""

import logging
import time

from authlib.integrations.httpx_client import AsyncOAuth1Client
from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from social_reply.application.account_management.admin import (
    _current_actor,
    _form,
    _page,
    _require_csrf,
    _secure_cookie,
    html,
)
from social_reply.application.account_management.jobs import submit_provisioning_job
from social_reply.application.account_management.submissions import split_submission
from social_reply.application.platform_accounts import list_active_accounts_by_platform
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.queue.dispatch import dispatch_actor
from social_reply.infrastructure.secret_crypto import decrypt_secret_bundle, encrypt_secret_bundle
from social_reply.shared.config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin-oauth"])

_STATE_COOKIE = "reply_x_oauth_state"
_STATE_TTL_SECONDS = 600
_X_OAUTH_BASE = "https://api.x.com"


def _oauth1_client(**kwargs) -> AsyncOAuth1Client:
    """工厂:测试经 monkeypatch 注入 MockTransport。"""
    return AsyncOAuth1Client(timeout=15, **kwargs)


def _callback_url() -> str:
    return f"{get_settings().public_base_url.rstrip('/')}/admin/oauth/x/callback"


async def _x_consumer_credentials() -> tuple[str, str] | None:
    """取 X App 级 consumer 凭证:优先 platform_apps,回退任一活跃 X 账号的四元组。"""
    async with get_session_factory()() as session:
        apps = (
            (
                await session.execute(
                    select(models.PlatformApp).where(
                        models.PlatformApp.platform_family == "x",
                        models.PlatformApp.status == "active",
                    )
                )
            )
            .scalars()
            .all()
        )
    candidates = []
    for app in apps:
        try:
            candidates.append(decrypt_secret_bundle(app.credential_bundle))
        except ValueError:
            logger.warning("platform_app %s has undecryptable credential bundle", app.id)
    candidates.extend(
        account.credential_bundle for account in await list_active_accounts_by_platform("x")
    )
    for bundle in candidates:
        consumer_key = bundle.get("consumer_key")
        consumer_secret = bundle.get("consumer_secret")
        if consumer_key and consumer_secret:
            return consumer_key, consumer_secret
    return None


def _notice(title: str, message: str, *, status_code: int = 200) -> HTMLResponse:
    body = f"""<a class="back" href="/admin/accounts">← 返回账号页</a>
<section class="card"><h1 style="font-size:24px">{html.escape(title)}</h1>
<p>{html.escape(message)}</p></section>"""
    return HTMLResponse(_page(title, body, active="accounts"), status_code=status_code)


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
    brand_id = form.get("brand_id", "default") or "default"

    credentials = await _x_consumer_credentials()
    if credentials is None:
        return _notice(
            "无法发起授权",
            "未找到 X App 的 consumer 凭证:请先用手工表单接入至少一个 X 账号,"
            "或在 platform_apps 中配置 App 凭证。",
            status_code=422,
        )
    consumer_key, consumer_secret = credentials
    callback_url = _callback_url()
    try:
        async with _oauth1_client(
            client_id=consumer_key, client_secret=consumer_secret, redirect_uri=callback_url
        ) as client:
            request_token = await client.fetch_request_token(f"{_X_OAUTH_BASE}/oauth/request_token")
            # authorize(而非 authenticate):每次都显示账号选择/授权页,支持接入多个不同账号
            authorize_url = client.create_authorization_url(
                f"{_X_OAUTH_BASE}/oauth/authorize", request_token["oauth_token"]
            )
    except Exception as exc:  # noqa: BLE001 - 面向运营的失败提示,不区分底层异常类型
        logger.warning("x oauth request_token failed: %s", exc)
        return _notice(
            "发起授权失败",
            f"向 X 换取 request token 失败({exc.__class__.__name__})。最常见原因:"
            f"该 App 未开启 User authentication,或未登记回调 URL {callback_url},"
            "或 App 类型不是 Native App。",
            status_code=502,
        )

    state = encrypt_secret_bundle(
        {
            "oauth_token": request_token["oauth_token"],
            "oauth_token_secret": request_token["oauth_token_secret"],
            "tenant_id": tenant_id,
            "brand_id": brand_id,
            "actor": actor,
            "xchat_pin": form.get("xchat_pin", ""),
            "ts": str(int(time.time())),
        }
    )
    response = RedirectResponse(authorize_url, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        _STATE_COOKIE,
        state["__encrypted__"],
        max_age=_STATE_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=_secure_cookie(request),
    )
    return response


@router.get("/oauth/x/callback")
async def x_oauth_callback(request: Request) -> Response:
    if request.query_params.get("denied"):
        return _notice("授权已取消", "你在 X 上取消了授权,未做任何变更。")
    cookie = request.cookies.get(_STATE_COOKIE)
    if not cookie:
        return _notice(
            "授权会话缺失",
            "找不到发起记录(已超时或浏览器拦截了 Cookie),请回到账号页重新发起。",
            status_code=400,
        )
    try:
        state = decrypt_secret_bundle({"__encrypted__": cookie})
    except ValueError:
        return _notice("授权状态无效", "状态校验失败,请重新发起授权。", status_code=400)
    issued_at = int(state.get("ts") or 0)
    if issued_at + _STATE_TTL_SECONDS < int(time.time()):
        return _notice("授权会话过期", "发起已超过 10 分钟,请重新发起授权。", status_code=400)
    oauth_token = request.query_params.get("oauth_token", "")
    verifier = request.query_params.get("oauth_verifier", "")
    if not oauth_token or not verifier or oauth_token != state.get("oauth_token"):
        return _notice(
            "授权参数不匹配", "回调参数与发起记录不一致,请重新发起授权。", status_code=400
        )

    credentials = await _x_consumer_credentials()
    if credentials is None:
        return _notice("无法完成授权", "X App consumer 凭证不可用。", status_code=422)
    consumer_key, consumer_secret = credentials
    try:
        async with _oauth1_client(
            client_id=consumer_key,
            client_secret=consumer_secret,
            token=oauth_token,
            token_secret=state["oauth_token_secret"],
        ) as client:
            token = await client.fetch_access_token(f"{_X_OAUTH_BASE}/oauth/access_token", verifier)
    except Exception as exc:  # noqa: BLE001 - request token 一次性,失败只能重新发起
        logger.warning("x oauth access_token failed: %s", exc)
        return _notice(
            "换取凭证失败",
            f"向 X 换取 access token 失败({exc.__class__.__name__})。"
            "request token 已失效,请重新发起授权。",
            status_code=502,
        )

    screen_name = token.get("screen_name", "")
    submission = {
        "tenant_id": state["tenant_id"],
        "brand_id": state.get("brand_id", "default"),
        "name": f"@{screen_name}" if screen_name else "x-oauth",
        "environment": "",
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
    response = RedirectResponse(f"/admin/jobs/{job_id}", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(_STATE_COOKIE)
    return response
