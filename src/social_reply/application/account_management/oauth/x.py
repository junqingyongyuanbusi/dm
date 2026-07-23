"""Connect X accounts with the same OAuth 1.0a model used by Postiz.

``X_API_KEY`` and ``X_API_SECRET`` identify one deployment-level X App. Each
successful three-legged OAuth flow yields a different user access-token pair,
which is validated and encrypted through the normal provisioning pipeline.
Repeating the flow connects another X account; reconnecting the same account
updates the existing database row by external account ID.
"""

import asyncio
import hashlib
import logging
import re
import secrets
from datetime import UTC, datetime, timedelta
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
from social_reply.application.account_management.auth import current_principal
from social_reply.application.account_management.jobs import (
    process_provisioning_job,
    submit_provisioning_job,
)
from social_reply.application.account_management.oauth.common import (
    admin_callback_url,
    notice,
    principal_from_oauth_context,
    store_oauth_state,
    take_oauth_state,
)
from social_reply.application.account_management.submissions import split_submission
from social_reply.application.account_management.x_app import x_app_credentials
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.queue.dispatch import dispatch_actor

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin-oauth"])

_X_OAUTH_BASE = "https://api.x.com"
_X_RETURN_TO = "/admin/accounts"
_X_TRANSACTION_MAX_AGE = timedelta(minutes=10)
_PROVISIONING_WAIT_SECONDS = 30.0
_RESULT_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


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


def _oauth_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _safe_return_to(value: object) -> str:
    candidate = str(value or "")
    return candidate if candidate in {_X_RETURN_TO} else _X_RETURN_TO


def _safe_result_code(value: object, fallback: str) -> str:
    candidate = str(value or "")
    return candidate if _RESULT_CODE_RE.fullmatch(candidate) else fallback


def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _request_id(request: Request) -> str:
    return (
        request.headers.get("x-request-id")
        or request.headers.get("x-railway-request-id")
        or request.headers.get("cf-ray")
        or "-"
    )


def _log_callback(
    request: Request,
    *,
    stage: str,
    oauth_token: str = "",
    http_status: int,
    code: str,
) -> None:
    logger.info(
        "x oauth callback request_id=%s stage=%s provider=x http_status=%s code=%s token_hash=%s",
        _request_id(request),
        stage,
        http_status,
        code,
        _oauth_token_hash(oauth_token)[:12] if oauth_token else "-",
    )


async def _result_redirect(
    request: Request,
    *,
    status_value: str,
    code: str | None = None,
    return_to: object = _X_RETURN_TO,
) -> Response:
    params = {"provider": "x", "status": status_value}
    if code:
        params["code"] = _safe_result_code(code, "oauth_failed")
    result_path = f"{_safe_return_to(return_to)}?{urlencode(params)}"
    try:
        principal = await current_principal(request)
    except Exception:  # noqa: BLE001 - result still safely returns through login
        principal = None
    target = (
        result_path
        if principal is not None and not principal.must_change_password
        else f"/admin/login?{urlencode({'next': result_path})}"
    )
    return _no_store(RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER))


def _valid_transaction(state: dict, oauth_token: str) -> bool:
    if state.get("status") not in {None, "pending"}:
        return False
    if state.get("organization_id") not in {None, state.get("tenant_id")}:
        return False
    expected_hash = state.get("oauth_token_hash")
    if expected_hash and not secrets.compare_digest(
        str(expected_hash), _oauth_token_hash(oauth_token)
    ):
        return False
    created_at = state.get("created_at")
    if not created_at:
        return True
    try:
        created = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
    except ValueError:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    age = datetime.now(UTC) - created
    return timedelta(seconds=-60) <= age <= _X_TRANSACTION_MAX_AGE


async def _load_provisioning_job(job_id):
    async with get_session_factory()() as session:
        return await session.get(models.ProvisioningJob, job_id)


async def _resolve_provisioning_result(
    job_id,
    initial_result: str | None,
    *,
    timeout_seconds: float = _PROVISIONING_WAIT_SECONDS,
) -> str:
    if initial_result in {"COMPLETED", "NEEDS_ACTION"}:
        return initial_result
    if initial_result == "FAILED":
        return "PROCESSING"
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return "PROCESSING"
        try:
            job = await asyncio.wait_for(_load_provisioning_job(job_id), timeout=remaining)
        except TimeoutError:
            return "PROCESSING"
        if job is None:
            return "MISSING"
        if job.status in {"COMPLETED", "NEEDS_ACTION"}:
            return job.status
        if job.status == "FAILED":
            return "PROCESSING"
        sleep_for = min(0.1, deadline - loop.time())
        if sleep_for <= 0:
            return "PROCESSING"
        await asyncio.sleep(sleep_for)


async def _provisioning_error_code(job_id) -> str:
    try:
        job = await _load_provisioning_job(job_id)
    except Exception:  # noqa: BLE001 - callback error codes stay redacted
        return "provisioning_result_unavailable"
    if job is None:
        return "provisioning_result_missing"
    return _safe_result_code(job.last_error_code, "provisioning_failed")


async def _request_token(
    *, consumer_key: str, consumer_secret: str, callback_url: str
) -> dict[str, str]:
    auth = _oauth1_auth(
        client_id=consumer_key,
        client_secret=consumer_secret,
        redirect_uri=callback_url,
    )
    # App-level OAuth 1.0a permissions define the consent scope. Sending the
    # legacy x_auth_access_type=write parameter downgrades an RW+DM App to plain
    # read/write, so request only the standard oauth_callback carried by auth.
    signed_url, headers, signed_body = auth.prepare(
        "POST",
        f"{_X_OAUTH_BASE}/oauth/request_token",
        {},
        None,
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
                "oauth_token_hash": _oauth_token_hash(token["oauth_token"]),
                "admin_id": str(principal.user_id or principal.session_id),
                "admin_session_id": str(principal.session_id),
                "session_id": str(principal.session_id),
                "organization_id": tenant_id,
                "tenant_id": tenant_id,
                "brand_id": (form.get("brand_id") or "default").strip() or "default",
                "return_to": _X_RETURN_TO,
                "created_at": datetime.now(UTC).isoformat(),
                "status": "pending",
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
        try:
            state = await take_oauth_state("x", denied)
        except (OSError, RedisError):
            state = None
            code = "oauth_state_unavailable"
        else:
            code = "access_denied" if state is not None else "oauth_state_missing"
        _log_callback(
            request,
            stage="denied",
            oauth_token=denied,
            http_status=303,
            code=code,
        )
        return await _result_redirect(
            request,
            status_value="error",
            code=code,
            return_to=(state or {}).get("return_to"),
        )

    oauth_token = request.query_params.get("oauth_token", "")
    verifier = request.query_params.get("oauth_verifier", "")
    if not oauth_token or not verifier:
        _log_callback(
            request,
            stage="validate_callback",
            http_status=400,
            code="oauth_callback_parameters_missing",
        )
        return _no_store(
            notice(
                "授权参数不完整",
                "回调缺少必要参数，请从后台账号页重新发起授权。",
                status_code=400,
            )
        )

    try:
        state = await take_oauth_state("x", oauth_token)
    except (OSError, RedisError):
        _log_callback(
            request,
            stage="load_transaction",
            oauth_token=oauth_token,
            http_status=303,
            code="oauth_state_unavailable",
        )
        return await _result_redirect(
            request,
            status_value="error",
            code="oauth_state_unavailable",
        )
    if state is None:
        _log_callback(
            request,
            stage="load_transaction",
            oauth_token=oauth_token,
            http_status=303,
            code="oauth_state_missing",
        )
        return await _result_redirect(
            request,
            status_value="error",
            code="oauth_state_missing",
        )
    if not _valid_transaction(state, oauth_token):
        _log_callback(
            request,
            stage="validate_transaction",
            oauth_token=oauth_token,
            http_status=303,
            code="oauth_transaction_invalid",
        )
        return await _result_redirect(
            request,
            status_value="error",
            code="oauth_transaction_invalid",
            return_to=state.get("return_to"),
        )

    try:
        principal = await principal_from_oauth_context(state)
    except Exception:  # noqa: BLE001 - return a redacted callback result
        _log_callback(
            request,
            stage="validate_admin",
            oauth_token=oauth_token,
            http_status=303,
            code="admin_session_unavailable",
        )
        return await _result_redirect(
            request,
            status_value="error",
            code="admin_session_unavailable",
            return_to=state.get("return_to"),
        )
    if principal is None:
        _log_callback(
            request,
            stage="validate_admin",
            oauth_token=oauth_token,
            http_status=303,
            code="admin_session_invalid",
        )
        return await _result_redirect(
            request,
            status_value="error",
            code="admin_session_invalid",
            return_to=state.get("return_to"),
        )

    credentials = x_app_credentials()
    if credentials is None:
        _log_callback(
            request,
            stage="load_app_credentials",
            oauth_token=oauth_token,
            http_status=303,
            code="x_oauth_app_not_configured",
        )
        return await _result_redirect(
            request,
            status_value="error",
            code="x_oauth_app_not_configured",
            return_to=state.get("return_to"),
        )
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
        exchange_status = (
            exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else 502
        )
        code = (
            "x_token_exchange_rejected"
            if exchange_status in {401, 403}
            else "x_token_exchange_failed"
        )
        _log_callback(
            request,
            stage="exchange_access_token",
            oauth_token=oauth_token,
            http_status=303,
            code=code,
        )
        return await _result_redirect(
            request,
            status_value="error",
            code=code,
            return_to=state.get("return_to"),
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
    try:
        job_id = await submit_provisioning_job(
            tenant_id=state["tenant_id"],
            brand_id=state.get("brand_id", "default"),
            platform="x",
            actor=principal.actor,
            request=request_data,
            secrets=secrets_data,
            admin_session_id=principal.session_id,
        )
    except Exception:  # noqa: BLE001 - callback must return a redacted result
        logger.error(
            "x oauth callback request_id=%s stage=submit provider=x http_status=303 "
            "code=%s token_hash=%s",
            _request_id(request),
            "provisioning_submit_failed",
            _oauth_token_hash(oauth_token)[:12],
        )
        return await _result_redirect(
            request,
            status_value="error",
            code="provisioning_submit_failed",
            return_to=state.get("return_to"),
        )

    from social_reply.application.account_management.actors import (
        process_platform_provisioning,
    )

    loop = asyncio.get_running_loop()
    deadline = loop.time() + _PROVISIONING_WAIT_SECONDS
    try:
        initial_result = await dispatch_actor(
            process_platform_provisioning,
            str(job_id),
            inline=lambda: process_provisioning_job(str(job_id)),
            timeout_seconds=max(0.001, deadline - loop.time()),
        )
        result = await _resolve_provisioning_result(
            job_id,
            initial_result,
            timeout_seconds=max(0.0, deadline - loop.time()),
        )
    except Exception:  # noqa: BLE001 - the durable job remains recoverable
        logger.error(
            "x oauth callback request_id=%s stage=observe provider=x http_status=303 "
            "code=%s token_hash=%s job_id=%s",
            _request_id(request),
            "provisioning_in_progress",
            _oauth_token_hash(oauth_token)[:12],
            job_id,
        )
        result = "PROCESSING"
    if result == "PROCESSING":
        _log_callback(
            request,
            stage="provision",
            oauth_token=oauth_token,
            http_status=303,
            code="provisioning_in_progress",
        )
        return await _result_redirect(
            request,
            status_value="processing",
            code="provisioning_in_progress",
            return_to=state.get("return_to"),
        )
    if result != "COMPLETED":
        code = await _provisioning_error_code(job_id)
        _log_callback(
            request,
            stage="provision",
            oauth_token=oauth_token,
            http_status=303,
            code=code,
        )
        return await _result_redirect(
            request,
            status_value="error",
            code=code,
            return_to=state.get("return_to"),
        )

    _log_callback(
        request,
        stage="completed",
        oauth_token=oauth_token,
        http_status=303,
        code="connected",
    )
    return await _result_redirect(
        request,
        status_value="connected",
        return_to=state.get("return_to"),
    )
