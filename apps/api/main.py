import logging
from importlib.resources import files

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from social_reply.application.account_management.admin import router as admin_router
from social_reply.application.account_management.admin_console import router as admin_console_router
from social_reply.application.account_management.oauth import router as oauth_router
from social_reply.application.account_management.router import router as account_management_router
from social_reply.application.account_management.users import router as admin_users_router
from social_reply.connectors.meta.router import router as meta_router
from social_reply.connectors.telegram.router import router as telegram_router
from social_reply.shared.config import Settings, get_settings

_X_OAUTH_CALLBACK_PATH = "/admin/oauth/x/callback"
_OAUTH_CALLBACK_PATHS = {
    _X_OAUTH_CALLBACK_PATH,
    "/admin/oauth/meta/callback",
    "/admin/oauth/instagram/callback",
}
_X_OAUTH_CALLBACK_PATHS = {_X_OAUTH_CALLBACK_PATH, f"{_X_OAUTH_CALLBACK_PATH}/"}


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
    _install_access_log_redaction()
    _install_application_logging()
    app = FastAPI(title="Reply Core")
    app.mount(
        "/static",
        StaticFiles(directory=str(files("social_reply").joinpath("static"))),
        name="static",
    )

    @app.middleware("http")
    async def x_oauth_callback_security(request: Request, call_next):
        if request.url.path not in _X_OAUTH_CALLBACK_PATHS:
            return await call_next(request)
        if request.url.path != _X_OAUTH_CALLBACK_PATH:
            response = PlainTextResponse("Invalid OAuth callback path", status_code=400)
        else:
            try:
                response = await call_next(request)
            except Exception:  # noqa: BLE001 - never expose callback internals
                request_id = (
                    request.headers.get("x-request-id")
                    or request.headers.get("x-railway-request-id")
                    or request.headers.get("cf-ray")
                    or "-"
                )
                logging.getLogger("social_reply.application.account_management.oauth.x").error(
                    "x oauth callback request_id=%s stage=unhandled provider=x http_status=500 "
                    "code=callback_internal_error token_hash=-",
                    request_id,
                )
                response = PlainTextResponse("OAuth callback failed", status_code=500)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    app.include_router(admin_router)
    app.include_router(admin_console_router)
    app.include_router(admin_users_router)
    app.include_router(oauth_router)
    app.include_router(account_management_router)
    if settings.chatwoot_enabled:
        from social_reply.application.event_ingestion.router import router as ingestion_router

        app.include_router(ingestion_router)
    app.include_router(telegram_router)
    app.include_router(meta_router)
    if settings.x_activity_enabled:
        from social_reply.connectors.x.router import router as x_router

        app.include_router(x_router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
