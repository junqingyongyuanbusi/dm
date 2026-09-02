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
    build_oauth_context,
    notice,
    oauth_error_response,
    oauth_result_response,
    principal_from_oauth_context,
    store_oauth_state,
    take_oauth_state,
)
from social_reply.application.account_management.submissions import split_submission
from social_reply.application.account_management.ui_i18n import translate
from social_reply.connectors.meta.client import appsecret_proof
from social_reply.infrastructure.queue.dispatch import dispatch_actor
from social_reply.shared.config import DEFAULT_TENANT_ID, get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin-oauth"])
channels_router = APIRouter(tags=["channels-oauth"])

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


def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.post("/oauth/instagram/start")
async def instagram_oauth_start(request: Request) -> Response:
    return await _start_instagram_oauth(request, surface="admin")


@channels_router.post("/app/t/{tenant_id}/channels/oauth/instagram/start")
async def channels_instagram_oauth_start(
    request: Request,
    tenant_id: str,
) -> Response:
    return await _start_instagram_oauth(
        request,
        surface="channels",
        route_tenant_id=tenant_id,
    )


async def _start_instagram_oauth(
    request: Request,
    *,
    surface: str,
    route_tenant_id: str | None = None,
) -> Response:
    principal = await _web_principal(
        request,
        require_admin=surface == "admin",
    )
    if isinstance(principal, Response):
        return principal
    form = await _form(request)
    _require_csrf(request, form)
    tenant_id = route_tenant_id or (form.get("tenant_id") or "").strip()
    if not tenant_id:
        raise HTTPException(status_code=422, detail="tenant_id_required")
    if tenant_id != DEFAULT_TENANT_ID:
        raise HTTPException(status_code=404, detail="tenant_workspace_not_found")
    principal.require_tenant(tenant_id)
    if not get_settings().instagram_messaging_enabled:
        return oauth_error_response(
            surface=surface,
            tenant_id=tenant_id,
            provider="instagram",
            code="platform_integration_disabled",
            title=translate("oauth.instagram.disabled_title"),
            message=translate("oauth.instagram.current_disabled"),
            status_code=503,
        )
    app = await instagram_app_credentials(tenant_id)
    if app is None:
        return oauth_error_response(
            surface=surface,
            tenant_id=tenant_id,
            provider="instagram",
            code="instagram_app_not_configured",
            title=translate("oauth.cannot_start.title"),
            message=translate("oauth.instagram.credentials_missing"),
            status_code=422,
        )

    state_token = secrets.token_urlsafe(32)
    try:
        await store_oauth_state(
            "instagram",
            state_token,
            build_oauth_context(
                principal=principal,
                provider="instagram",
                tenant_id=tenant_id,
                surface=surface,
                return_to=(
                    f"/app/t/{tenant_id}/channels" if surface == "channels" else "/admin/accounts"
                ),
                extra={
                    "brand_id": ((form.get("brand_id") or "default").strip() or "default"),
                },
            ),
        )
    except (OSError, RedisError) as exc:
        logger.warning("instagram oauth state storage failed: %s", exc)
        return oauth_error_response(
            surface=surface,
            tenant_id=tenant_id,
            provider="instagram",
            code="oauth_state_unavailable",
            title=translate("oauth.start_failed.title"),
            message=translate("oauth.start_unavailable_short"),
            status_code=503,
        )

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
    return _no_store(await _handle_instagram_oauth_callback(request))


async def _handle_instagram_oauth_callback(request: Request) -> Response:
    state_token = request.query_params.get("state", "")
    if request.query_params.get("error"):
        cancelled_context = (
            await take_oauth_state("instagram", state_token) if state_token else None
        )
        if cancelled_context is not None and cancelled_context.get("surface") == "channels":
            return oauth_result_response(
                cancelled_context,
                provider="instagram",
                status_value="error",
                code="access_denied",
            )
        return notice(
            translate("oauth.cancelled.title"),
            request.query_params.get("error_description")
            or translate("oauth.cancelled.default_detail"),
        )
    context = await take_oauth_state("instagram", state_token) if state_token else None
    if context is None:
        return notice(
            translate("oauth.session_invalid.title"),
            translate("oauth.session_missing"),
            status_code=400,
        )
    if not get_settings().instagram_messaging_enabled:
        if context.get("surface") == "channels":
            return oauth_result_response(
                context,
                provider="instagram",
                status_value="error",
                code="platform_integration_disabled",
            )
        return notice(
            translate("oauth.instagram.disabled_title"),
            translate("oauth.instagram.disabled_during_flow"),
            status_code=503,
        )
    principal = await principal_from_oauth_context(context)
    if principal is None:
        if context.get("surface") == "channels":
            return oauth_result_response(
                context,
                provider="instagram",
                status_value="error",
                code="initiator_session_invalid",
            )
        return notice(
            translate("oauth.session_expired.title"),
            translate("oauth.session_expired.admin"),
            status_code=403,
        )
    code = request.query_params.get("code", "").removesuffix("#_")
    if not code:
        if context.get("surface") == "channels":
            return oauth_result_response(
                context,
                provider="instagram",
                status_value="error",
                code="oauth_callback_parameters_missing",
            )
        return notice(
            translate("oauth.parameters_missing.title"),
            translate("oauth.parameters_missing.retry"),
            status_code=400,
        )
    app = await instagram_app_credentials(context["tenant_id"])
    if app is None:
        if context.get("surface") == "channels":
            return oauth_result_response(
                context,
                provider="instagram",
                status_value="error",
                code="instagram_app_not_configured",
            )
        return notice(
            translate("oauth.cannot_complete.title"),
            translate("oauth.instagram.app_unavailable"),
            status_code=422,
        )

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
        if context.get("surface") == "channels":
            return oauth_result_response(
                context,
                provider="instagram",
                status_value="error",
                code="token_exchange_failed",
            )
        return notice(
            translate("oauth.exchange_failed.title"),
            translate(
                "oauth.instagram.exchange_failed",
                error_type=exc.__class__.__name__,
            ),
            status_code=502,
        )

    principal = await principal_from_oauth_context(context)
    if principal is None:
        if context.get("surface") == "channels":
            return oauth_result_response(
                context,
                provider="instagram",
                status_value="error",
                code="initiator_session_invalid",
            )
        return notice(
            translate("oauth.session_expired.title"),
            translate("oauth.session_expired.during_flow"),
            status_code=403,
        )

    if not get_settings().instagram_messaging_enabled:
        if context.get("surface") == "channels":
            return oauth_result_response(
                context,
                provider="instagram",
                status_value="error",
                code="platform_integration_disabled",
            )
        return notice(
            translate("oauth.instagram.disabled_title"),
            translate("oauth.instagram.disabled_before_submit"),
            status_code=503,
        )
    external_account_id = str(profile.get("user_id") or profile.get("id") or "")
    if not external_account_id:
        if context.get("surface") == "channels":
            return oauth_result_response(
                context,
                provider="instagram",
                status_value="error",
                code="account_profile_missing",
            )
        return notice(
            translate("oauth.instagram.account_missing_title"),
            translate("oauth.instagram.account_missing"),
            status_code=502,
        )
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
        "automation_default": "BOT_DRAFT_ONLY",
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
    if context.get("surface") == "channels":
        target = f"{context['return_to']}?{urlencode({'status': 'processing', 'job_id': job_id})}"
    else:
        target = f"/admin/jobs/{job_id}"
    return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)
