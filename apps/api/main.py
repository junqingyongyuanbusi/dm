from fastapi import FastAPI


def create_app() -> FastAPI:
    app = FastAPI(title="Reply Core")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
