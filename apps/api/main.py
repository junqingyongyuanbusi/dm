from fastapi import FastAPI

from social_reply.application.account_management.admin import router as admin_router
from social_reply.application.account_management.admin_console import router as admin_console_router
from social_reply.application.account_management.oauth import router as oauth_router
from social_reply.application.account_management.router import router as account_management_router
from social_reply.application.event_ingestion.router import router as ingestion_router
from social_reply.connectors.meta.router import router as meta_router
from social_reply.connectors.telegram.router import router as telegram_router
from social_reply.connectors.x.router import router as x_router


def create_app() -> FastAPI:
    app = FastAPI(title="Reply Core")
    app.include_router(admin_router)
    app.include_router(admin_console_router)
    app.include_router(oauth_router)
    app.include_router(account_management_router)
    app.include_router(ingestion_router)
    app.include_router(telegram_router)
    app.include_router(meta_router)
    app.include_router(x_router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
