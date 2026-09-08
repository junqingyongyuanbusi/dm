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
    build_oauth_context,
    notice,
    oauth_error_response,
    oauth_provisioning_error_response,
    oauth_result_response,
    peek_oauth_state,
    principal_from_oauth_context,
    resolve_oauth_target,
    store_oauth_state,
    take_oauth_state,
)
from social_reply.application.account_management.saas_ui import render_saas_page
from social_reply.application.account_management.submissions import split_submission
from social_reply.application.account_management.ui_i18n import translate
from social_reply.infrastructure.queue.dispatch import dispatch_actor
from social_reply.shared.config import DEFAULT_TENANT_ID, get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin-oauth"])
channels_router = APIRouter(tags=["channels-oauth"])

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
_FACEBOOK_COMMENT_SCOPES = (
    "pages_read_engagement",
    "pages_read_user_content",
    "pages_manage_engagement",
)
_INSTAGRAM_COMMENT_SCOPES = (
    "pages_read_engagement",
    "instagram_manage_comments",
)


def _oauth_scopes(platform: str) -> str:
    scopes = _SCOPES[platform].split(",")
    if platform == "facebook" and get_settings().meta_comment_reply_enabled:
        scopes.extend(_FACEBOOK_COMMENT_SCOPES)
    if platform == "instagram" and get_settings().meta_comment_reply_enabled:
        scopes.extend(_INSTAGRAM_COMMENT_SCOPES)
    return ",".join(scopes)


def _graph_client(**kwargs) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=_GRAPH_BASE, timeout=15, **kwargs)


def _proof(token: str, app_secret: str) -> str:
    return hmac.new(app_secret.encode(), token.encode(), hashlib.sha256).hexdigest()


def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


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
        "fields": (
            "id,name,picture.type(large),access_token,"
            "instagram_business_account{id,username,profile_picture_url}"
        ),
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
    return await _start_meta_oauth(request, surface="admin")


@channels_router.post("/app/t/{tenant_id}/channels/oauth/meta/start")
async def channels_meta_oauth_start(request: Request, tenant_id: str) -> Response:
    return await _start_meta_oauth(
        request,
        surface="channels",
        route_tenant_id=tenant_id,
    )


async def _start_meta_oauth(
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
    platform = form.get("platform", "")
    if platform not in _SCOPES:
        raise HTTPException(status_code=422, detail="platform_must_be_facebook_or_instagram")
    tenant_id = route_tenant_id or (form.get("tenant_id") or "").strip()
    if not tenant_id:
        raise HTTPException(status_code=422, detail="tenant_id_required")
    if tenant_id != DEFAULT_TENANT_ID:
        raise HTTPException(status_code=404, detail="tenant_workspace_not_found")
    principal.require_tenant(tenant_id)
    target_binding = await resolve_oauth_target(
        request,
        principal=principal,
        provider=platform,
        tenant_id=tenant_id,
    )
    if not get_settings().platform_integration_enabled(platform):
        return oauth_error_response(
            surface=surface,
            tenant_id=tenant_id,
            provider=platform,
            code="platform_integration_disabled",
            title=translate("oauth.integration_disabled.title"),
            message=translate("oauth.integration_disabled.current", provider=platform),
            status_code=503,
        )

    app = await facebook_app_credentials(tenant_id)
    if app is None:
        return oauth_error_response(
            surface=surface,
            tenant_id=tenant_id,
            provider=platform,
            code="meta_app_not_configured",
            title=translate("oauth.cannot_start.title"),
            message=translate("oauth.meta.credentials_missing"),
            status_code=422,
        )
    state_token = secrets.token_urlsafe(32)
    try:
        await store_oauth_state(
            "meta",
            state_token,
            build_oauth_context(
                principal=principal,
                provider=platform,
                tenant_id=tenant_id,
                surface=surface,
                return_to=(
                    f"/app/t/{tenant_id}/channels" if surface == "channels" else "/admin/accounts"
                ),
                operation=target_binding["operation"],
                target_account_id=target_binding["target_account_id"],
                expected_config_version=target_binding["expected_config_version"],
                target_external_account_id=target_binding["target_external_account_id"],
                extra={
                    "brand_id": (
                        target_binding["brand_id"]
                        or ((form.get("brand_id") or "default").strip() or "default")
                    ),
                },
            ),
        )
    except (OSError, RedisError) as exc:
        logger.warning("meta oauth state storage failed: %s", exc)
        return oauth_error_response(
            surface=surface,
            tenant_id=tenant_id,
            provider=platform,
            code="oauth_state_unavailable",
            title=translate("oauth.start_failed.title"),
            message=translate("oauth.start_unavailable"),
            status_code=503,
        )

    dialog_url = (
        _DIALOG_URL
        + "?"
        + urlencode(
            {
                "client_id": app.app_id,
                "redirect_uri": admin_callback_url("/admin/oauth/meta/callback"),
                "state": state_token,
                "response_type": "code",
                "scope": _oauth_scopes(platform),
                **({"auth_type": "rerequest"} if get_settings().meta_comment_reply_enabled else {}),
            },
            quote_via=quote,
        )
    )
    return RedirectResponse(dialog_url, status_code=status.HTTP_303_SEE_OTHER)


@router.get("/oauth/meta/callback")
async def meta_oauth_callback(request: Request) -> Response:
    return _no_store(await _handle_meta_oauth_callback(request))


async def _handle_meta_oauth_callback(request: Request) -> Response:
    state_token = request.query_params.get("state", "")
    if request.query_params.get("error"):
        cancelled_state = await take_oauth_state("meta", state_token) if state_token else None
        if cancelled_state is not None and cancelled_state.get("surface") == "channels":
            return oauth_result_response(
                cancelled_state,
                provider=str(cancelled_state.get("platform") or "facebook"),
                status_value="error",
                code="access_denied",
            )
        return notice(
            translate("oauth.cancelled.title"),
            translate(
                "oauth.cancelled.provider_detail",
                provider="Meta",
                detail=request.query_params.get("error_description")
                or translate("oauth.cancelled.default_detail"),
            ),
        )
    state = await take_oauth_state("meta", state_token) if state_token else None
    if state is None:
        return notice(
            translate("oauth.session_invalid.title"),
            translate("oauth.session_missing"),
            status_code=400,
        )
    if not get_settings().platform_integration_enabled(str(state.get("platform") or "")):
        if state.get("surface") == "channels":
            return oauth_result_response(
                state,
                provider=str(state.get("platform") or "facebook"),
                status_value="error",
                code="platform_integration_disabled",
            )
        return notice(
            translate("oauth.integration_disabled.title"),
            translate("oauth.integration_disabled.during_flow"),
            status_code=503,
        )
    principal = await principal_from_oauth_context(state)
    if principal is None:
        if state.get("surface") == "channels":
            return oauth_result_response(
                state,
                provider=str(state.get("platform") or "facebook"),
                status_value="error",
                code="initiator_session_invalid",
            )
        return notice(
            translate("oauth.session_expired.title"),
            translate("oauth.session_expired.admin"),
            status_code=403,
        )
    code = request.query_params.get("code", "")
    if not code:
        if state.get("surface") == "channels":
            return oauth_result_response(
                state,
                provider=str(state.get("platform") or "facebook"),
                status_value="error",
                code="oauth_callback_parameters_missing",
            )
        return notice(
            translate("oauth.parameters_missing.title"),
            translate("oauth.parameters_missing.retry"),
            status_code=400,
        )
    app = await facebook_app_credentials(state["tenant_id"])
    if app is None:
        if state.get("surface") == "channels":
            return oauth_result_response(
                state,
                provider=str(state.get("platform") or "facebook"),
                status_value="error",
                code="meta_app_not_configured",
            )
        return notice(
            translate("oauth.cannot_complete.title"),
            translate("oauth.meta.app_unavailable"),
            status_code=422,
        )

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
        if state.get("surface") == "channels":
            return oauth_result_response(
                state,
                provider=str(state.get("platform") or "facebook"),
                status_value="error",
                code="token_exchange_failed",
            )
        return notice(
            translate("oauth.exchange_failed.title"),
            translate(
                "oauth.meta.exchange_failed",
                error_type=exc.__class__.__name__,
            ),
            status_code=502,
        )

    principal = await principal_from_oauth_context(state)
    if principal is None:
        if state.get("surface") == "channels":
            return oauth_result_response(
                state,
                provider=str(state.get("platform") or "facebook"),
                status_value="error",
                code="initiator_session_invalid",
            )
        return notice(
            translate("oauth.session_expired.title"),
            translate("oauth.session_expired.during_flow"),
            status_code=403,
        )

    candidates = _candidates(pages, state["platform"])
    if not candidates:
        if state.get("surface") == "channels":
            return oauth_result_response(
                state,
                provider=str(state.get("platform") or "facebook"),
                status_value="error",
                code="no_authorized_accounts",
            )
        return notice(
            translate("oauth.no_targets.title"),
            (
                translate("oauth.no_targets.instagram")
                if state["platform"] == "instagram"
                else translate("oauth.no_targets.facebook")
            ),
            status_code=422,
        )
    target_external_id = state.get("target_external_account_id")
    if target_external_id:
        matching_candidates = [
            candidate
            for candidate in candidates
            if (
                candidate["ig_id"] if state["platform"] == "instagram" else candidate["id"]
            )
            == str(target_external_id)
        ]
        if len(matching_candidates) != 1:
            return oauth_error_response(
                surface=str(state.get("surface") or "admin"),
                tenant_id=str(state.get("tenant_id") or ""),
                provider=str(state.get("platform") or "facebook"),
                code="oauth_target_identity_mismatch",
                title=translate("oauth.cannot_complete.title"),
                message="授权账号与目标平台账号不一致。",
                status_code=409,
            )
        return await _finalize(matching_candidates[0], state, app, principal)
    if len(candidates) == 1:
        return await _finalize(candidates[0], state, app, principal)
    return await _picker(request, candidates, state, principal)


def _candidates(pages: list[dict], platform: str) -> list[dict[str, str]]:
    candidates = [
        {
            "id": str(page.get("id") or ""),
            "name": str(page.get("name") or ""),
            "access_token": str(page.get("access_token") or ""),
            "page_avatar_url": str(
                ((page.get("picture") or {}).get("data") or {}).get("url") or ""
            ),
            "ig_id": str((page.get("instagram_business_account") or {}).get("id") or ""),
            "ig_username": str(
                (page.get("instagram_business_account") or {}).get("username") or ""
            ),
            "ig_avatar_url": str(
                (page.get("instagram_business_account") or {}).get("profile_picture_url") or ""
            ),
        }
        for page in pages
        if page.get("id") and page.get("access_token")
    ]
    if platform == "instagram":
        return [candidate for candidate in candidates if candidate["ig_id"]]
    return candidates


async def _picker(
    request: Request,
    candidates: list[dict],
    context: dict,
    principal: Principal,
) -> Response:
    pick_token = secrets.token_urlsafe(32)
    try:
        await store_oauth_state(
            "meta-pick",
            pick_token,
            {"candidates": candidates, **context},
        )
    except (OSError, RedisError) as exc:
        logger.warning("meta picker state storage failed: %s", exc)
        return oauth_error_response(
            surface=str(context.get("surface") or "admin"),
            tenant_id=str(context.get("tenant_id") or ""),
            provider=str(context.get("platform") or "facebook"),
            code="oauth_state_unavailable",
            title=translate("oauth.picker.unavailable_title"),
            message=translate("oauth.start_unavailable_short"),
            status_code=503,
        )

    csrf = _csrf(request)
    rows = "".join(
        f'<label class="saas-channel-choice"><input type="radio" name="choice" '
        f'value="{index}" required> {html.escape(candidate["name"])} '
        f'<span class="muted">(Page {html.escape(candidate["id"])}'
        + (f" · IG @{html.escape(candidate['ig_username'])}" if candidate["ig_id"] else "")
        + ")</span></label>"
        for index, candidate in enumerate(candidates)
    )
    label = (
        translate("oauth.picker.target.instagram")
        if context["platform"] == "instagram"
        else translate("oauth.picker.target.facebook")
    )
    channels_surface = context.get("surface") == "channels"
    return_to = str(context.get("return_to") or "/admin/integrations/accounts")
    select_action = (
        f"/app/t/{context['tenant_id']}/channels/oauth/meta/select"
        if channels_surface
        else "/admin/oauth/meta/select"
    )
    back_label = (
        translate("oauth.picker.back_channels")
        if channels_surface
        else translate("oauth.back_to_accounts")
    )
    card_class = "saas-card" if channels_surface else "card"
    form_class = "saas-form" if channels_surface else ""
    button_class = "saas-button primary" if channels_surface else "btn-block"
    card_body_start = '<div class="saas-card-body">' if channels_surface else ""
    card_body_end = "</div>" if channels_surface else ""
    picker_heading = translate("oauth.picker.select_target", target=label)
    connect_selected_label = translate("oauth.picker.connect_selected")
    body = f"""<a class="back" href="{html.escape(return_to)}">← {back_label}</a>
<section class="{card_class}">{card_body_start}<h2>{picker_heading}</h2>
<form class="{form_class}" method="post" action="{html.escape(select_action)}">
<input type="hidden" name="csrf_token" value="{csrf}">
<input type="hidden" name="pick_token" value="{pick_token}">{rows}
<button class="{button_class}">{connect_selected_label}</button></form>{card_body_end}</section>"""
    if channels_surface:
        page_html = render_saas_page(
            principal=principal,
            title=translate("oauth.picker.title"),
            description=translate("oauth.picker.description"),
            body=body,
            active_navigation="channels",
            tenant_id=str(context["tenant_id"]),
        )
    else:
        page_html = _page(
            translate("oauth.picker.title"),
            body,
            active="accounts",
            principal=principal,
        )
    response = HTMLResponse(page_html)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
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
    return await _select_meta_account(request, surface="admin")


@channels_router.post("/app/t/{tenant_id}/channels/oauth/meta/select")
async def channels_meta_oauth_select(
    request: Request,
    tenant_id: str,
) -> Response:
    return await _select_meta_account(
        request,
        surface="channels",
        route_tenant_id=tenant_id,
    )


async def _select_meta_account(
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
    if route_tenant_id is not None and route_tenant_id != DEFAULT_TENANT_ID:
        raise HTTPException(status_code=404, detail="tenant_workspace_not_found")
    form = await _form(request)
    _require_csrf(request, form)
    pick_token = form.get("pick_token", "") or request.cookies.get(_PICK_COOKIE, "")
    pending_pick = await peek_oauth_state("meta-pick", pick_token)
    if pending_pick is None:
        return oauth_error_response(
            surface=surface,
            tenant_id=route_tenant_id or "",
            provider="facebook",
            code="oauth_picker_expired",
            title=translate("oauth.picker.invalid_title"),
            message=translate("oauth.picker.expired"),
            status_code=400,
        )
    if pending_pick.get("surface", "admin") != surface:
        raise HTTPException(status_code=403, detail="oauth_surface_mismatch")
    context_principal = await principal_from_oauth_context(pending_pick)
    if context_principal is None or context_principal.session_id != principal.session_id:
        return oauth_error_response(
            surface=surface,
            tenant_id=str(pending_pick.get("tenant_id") or route_tenant_id or ""),
            provider=str(pending_pick.get("platform") or "facebook"),
            code="initiator_session_invalid",
            title=translate("oauth.picker.invalid_title"),
            message=translate("oauth.picker.same_session"),
            status_code=403,
        )
    if route_tenant_id is not None and pending_pick.get("tenant_id") != route_tenant_id:
        raise HTTPException(status_code=403, detail="tenant_access_denied")
    pending_tenant_id = str(pending_pick.get("tenant_id") or "")
    if pending_tenant_id != DEFAULT_TENANT_ID:
        raise HTTPException(status_code=404, detail="tenant_workspace_not_found")
    principal.require_tenant(pending_tenant_id)
    if not get_settings().platform_integration_enabled(str(pending_pick.get("platform") or "")):
        return oauth_error_response(
            surface=surface,
            tenant_id=str(pending_pick.get("tenant_id") or ""),
            provider=str(pending_pick.get("platform") or "facebook"),
            code="platform_integration_disabled",
            title=translate("oauth.integration_disabled.title"),
            message=translate("oauth.picker.platform_disabled"),
            status_code=503,
        )
    try:
        choice = int(form.get("choice", ""))
        pending_pick["candidates"][choice]
    except (KeyError, TypeError, ValueError, IndexError):
        return oauth_error_response(
            surface=surface,
            tenant_id=str(pending_pick.get("tenant_id") or ""),
            provider=str(pending_pick.get("platform") or "facebook"),
            code="oauth_picker_choice_invalid",
            title=translate("oauth.picker.choice_invalid_title"),
            message=translate("oauth.picker.choice_invalid"),
            status_code=400,
        )
    app = await facebook_app_credentials(pending_pick["tenant_id"])
    if app is None:
        return oauth_error_response(
            surface=surface,
            tenant_id=str(pending_pick.get("tenant_id") or ""),
            provider=str(pending_pick.get("platform") or "facebook"),
            code="meta_app_not_configured",
            title=translate("oauth.cannot_complete.title"),
            message=translate("oauth.meta.app_unavailable"),
            status_code=422,
        )
    pick = await take_oauth_state("meta-pick", pick_token)
    if pick is None:
        return oauth_error_response(
            surface=surface,
            tenant_id=str(pending_pick.get("tenant_id") or ""),
            provider=str(pending_pick.get("platform") or "facebook"),
            code="oauth_picker_consumed",
            title=translate("oauth.picker.invalid_title"),
            message=translate("oauth.picker.consumed"),
            status_code=400,
        )
    try:
        candidate = pick["candidates"][choice]
    except (KeyError, TypeError, IndexError):
        return oauth_error_response(
            surface=surface,
            tenant_id=str(pick.get("tenant_id") or ""),
            provider=str(pick.get("platform") or "facebook"),
            code="oauth_picker_payload_invalid",
            title=translate("oauth.picker.invalid_title"),
            message=translate("oauth.picker.changed"),
            status_code=400,
        )
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
    settings = get_settings()
    if not settings.platform_integration_enabled(platform):
        return oauth_error_response(
            surface=str(context.get("surface") or "admin"),
            tenant_id=str(context.get("tenant_id") or ""),
            provider=platform,
            code="platform_integration_disabled",
            title=translate("oauth.integration_disabled.title"),
            message=translate("oauth.integration_disabled.before_submit"),
            status_code=503,
        )
    if platform == "instagram":
        external_account_id = candidate["ig_id"]
        display_name = (
            f"@{candidate['ig_username']}" if candidate["ig_username"] else candidate["name"]
        )
    else:
        external_account_id = candidate["id"]
        display_name = candidate["name"]
    target_external_id = context.get("target_external_account_id")
    if target_external_id and str(target_external_id) != str(
        candidate["ig_id"] if platform == "instagram" else candidate["id"]
    ):
        return oauth_error_response(
            surface=str(context.get("surface") or "admin"),
            tenant_id=str(context.get("tenant_id") or ""),
            provider=platform,
            code="oauth_target_identity_mismatch",
            title=translate("oauth.cannot_complete.title"),
            message="授权账号与目标平台账号不一致。",
            status_code=409,
        )
    if platform == "instagram":
        external_account_id = candidate["ig_id"]
    enable_comments = settings.meta_comment_reply_enabled
    submission = {
        "name": display_name,
        "external_account_id": external_account_id,
        "app_id": app.app_id,
        "app_public_id": app.public_id,
        "api_version": _API_VERSION,
        "instagram_login_mode": "facebook_login",
        "page_id": candidate["id"],
        "enable_dm": True,
        "enable_comments": enable_comments,
        "automation_default": "BOT_DRAFT_ONLY",
        "access_token": candidate["access_token"],
        "app_secret": app.app_secret,
        "verify_token": app.verify_token,
    }
    request_data, secrets_data = split_submission(platform, submission)
    try:
        job_id = await submit_provisioning_job(
            tenant_id=context["tenant_id"],
            brand_id=context.get("brand_id", "default"),
            platform=platform,
            actor=principal.actor,
            operation=context.get("operation", "CONNECT_ACCOUNT"),
            target_account_id=context.get("target_account_id"),
            expected_config_version=context.get("expected_config_version"),
            request=request_data,
            secrets=secrets_data,
            admin_session_id=principal.session_id,
        )
    except (LookupError, PermissionError, ValueError) as exc:
        return oauth_provisioning_error_response(context, exc)
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
