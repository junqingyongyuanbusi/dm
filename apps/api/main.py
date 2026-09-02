import logging
from importlib.resources import files
from urllib.parse import quote, urlencode

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from social_reply.application.account_management.admin import (
    auth_router,
    reset_web_principal_context,
    set_web_principal_context,
)
from social_reply.application.account_management.admin import (
    router as admin_router,
)
from social_reply.application.account_management.admin_console import router as admin_console_router
from social_reply.application.account_management.feishu_handoff_admin import (
    router as feishu_handoff_admin_router,
)
from social_reply.application.account_management.oauth import router as oauth_router
from social_reply.application.account_management.router import router as account_management_router
from social_reply.application.account_management.saas_console import router as saas_console_router
from social_reply.application.account_management.ui_i18n import (
    LOCALE_COOKIE_NAME,
    SUPPORTED_LOCALES,
    normalize_locale,
    reset_locale,
    reset_request_location,
    set_locale,
    set_request_location,
)
from social_reply.application.account_management.users import router as admin_users_router
from social_reply.connectors.feishu.router import router as feishu_router
from social_reply.connectors.meta.router import router as meta_router
from social_reply.connectors.telegram.router import router as telegram_router
from social_reply.shared.config import Settings, get_settings
from social_reply.shared.logging import configure_safe_http_client_logging

_X_OAUTH_CALLBACK_PATH = "/admin/oauth/x/callback"
_OAUTH_CALLBACK_PATHS = {
    _X_OAUTH_CALLBACK_PATH,
    "/admin/oauth/meta/callback",
    "/admin/oauth/instagram/callback",
}
_OAUTH_CALLBACK_REQUEST_PATHS = frozenset(
    callback_request_path
    for callback_path in _OAUTH_CALLBACK_PATHS
    for callback_request_path in (callback_path, f"{callback_path}/")
)
_SUPPORTED_LOCALE_INPUTS = frozenset(locale.casefold() for locale in SUPPORTED_LOCALES)


def _same_origin_redirect_path(request: Request) -> str:
    redirect_path = quote(request.url.path, safe="/:@-._~!$&'()*+,;=")
    if not redirect_path.startswith("/"):
        redirect_path = f"/{redirect_path}"
    if redirect_path.startswith("//"):
        redirect_path = f"/%2F{redirect_path[2:]}"
    return redirect_path


class OAuthCallbackAccessLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            path = args[2]
            for callback_path in _OAUTH_CALLBACK_PATHS:
                if (
                    path == callback_path
                    or path == f"{callback_path}/"
                    or path.startswith(f"{callback_path}?")
                    or path.startswith(f"{callback_path}/?")
                ):
                    redacted = list(args)
                    redacted[2] = callback_path
                    record.args = tuple(redacted)
                    break
        return True


def _install_access_log_redaction() -> None:
    access_logger = logging.getLogger("uvicorn.access")
    if not any(
        isinstance(log_filter, OAuthCallbackAccessLogFilter) for log_filter in access_logger.filters
    ):
        access_logger.addFilter(OAuthCallbackAccessLogFilter())


def _install_application_logging() -> None:
    app_logger = logging.getLogger("social_reply")
    app_logger.setLevel(logging.INFO)
    if app_logger.handlers:
        return
    for logger_name in ("uvicorn.error", "uvicorn"):
        source_logger = logging.getLogger(logger_name)
        if source_logger.handlers:
            for handler in source_logger.handlers:
                app_logger.addHandler(handler)
            app_logger.propagate = False
            return


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_safe_http_client_logging()
    _install_access_log_redaction()
    _install_application_logging()
    app = FastAPI(title="Reply Core")
    app.state.settings = settings
    app.mount(
        "/static",
        StaticFiles(directory=str(files("social_reply").joinpath("static"))),
        name="static",
    )

    # Register this first so OAuth callback security remains the outermost middleware.
    @app.middleware("http")
    async def ui_locale(request: Request, call_next):
        locale_token = set_locale(request.cookies.get(LOCALE_COOKIE_NAME, ""))
        request_location_token = set_request_location(
            _same_origin_redirect_path(request),
            tuple(request.query_params.multi_items()),
        )
        principal_token = set_web_principal_context(None)
        try:
            requested_locales = request.query_params.getlist("ui_lang")
            should_redirect = request.method in {"GET", "HEAD"} and bool(requested_locales)
            if should_redirect:
                remaining_query_items = [
                    (key, value)
                    for key, value in request.query_params.multi_items()
                    if key != "ui_lang"
                ]
                redirect_path = _same_origin_redirect_path(request)
                redirect_query = urlencode(remaining_query_items)
                redirect_target = (
                    f"{redirect_path}?{redirect_query}" if redirect_query else redirect_path
                )
                response = RedirectResponse(redirect_target, status_code=303)

                requested_locale = requested_locales[-1].strip()
                normalized_locale = normalize_locale(requested_locale)
                if requested_locale.casefold() in _SUPPORTED_LOCALE_INPUTS:
                    response.set_cookie(
                        LOCALE_COOKIE_NAME,
                        normalized_locale,
                        httponly=True,
                        samesite="lax",
                        path="/",
                        secure=request.url.scheme == "https",
                    )
                return response

            return await call_next(request)
        finally:
            reset_web_principal_context(principal_token)
            reset_request_location(request_location_token)
            reset_locale(locale_token)

    @app.middleware("http")
    async def oauth_callback_security(request: Request, call_next):
        if request.url.path not in _OAUTH_CALLBACK_REQUEST_PATHS:
            return await call_next(request)
        is_x_callback = request.url.path in {
            _X_OAUTH_CALLBACK_PATH,
            f"{_X_OAUTH_CALLBACK_PATH}/",
        }
        if is_x_callback and request.url.path != _X_OAUTH_CALLBACK_PATH:
            response = PlainTextResponse("Invalid OAuth callback path", status_code=400)
        else:
            try:
                response = await call_next(request)
            except Exception:  # noqa: BLE001 - never expose OAuth callback internals
                request_id = (
                    request.headers.get("x-request-id")
                    or request.headers.get("x-railway-request-id")
                    or request.headers.get("cf-ray")
                    or "-"
                )
                callback_provider = {
                    "/admin/oauth/meta/callback": "meta",
                    "/admin/oauth/instagram/callback": "instagram",
                }.get(request.url.path.rstrip("/"), "x")
                logging.getLogger("social_reply.application.account_management.oauth").error(
                    "oauth callback request_id=%s stage=unhandled provider=%s http_status=500 "
                    "code=callback_internal_error",
                    request_id,
                    callback_provider,
                )
                response = PlainTextResponse("OAuth callback failed", status_code=500)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    app.include_router(auth_router)
    app.include_router(admin_router)
    app.include_router(admin_console_router)
    app.include_router(feishu_handoff_admin_router)
    app.include_router(admin_users_router)
    app.include_router(oauth_router)
    app.include_router(saas_console_router)
    app.include_router(account_management_router)
    if settings.chatwoot_enabled:
        from social_reply.application.event_ingestion.router import router as ingestion_router

        app.include_router(ingestion_router)
    app.include_router(telegram_router)
    app.include_router(meta_router)
    app.include_router(feishu_router)
    if settings.x_activity_enabled:
        from social_reply.connectors.x.router import router as x_router

        app.include_router(x_router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
