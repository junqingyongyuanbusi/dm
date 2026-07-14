from fastapi import FastAPI

from social_reply.application.event_ingestion.router import router as ingestion_router


def create_app() -> FastAPI:
    app = FastAPI(title="Reply Core")
    app.include_router(ingestion_router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
