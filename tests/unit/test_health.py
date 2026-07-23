import httpx

from apps.api.main import create_app
from social_reply.shared.config import Settings


def _settings(*, chatwoot_enabled: bool) -> Settings:
    return Settings(
        _env_file=None,
        testing=True,
        chatwoot_enabled=chatwoot_enabled,
        platform_secret_keys="Wm5wbamjBFvTmkGIU2NskIKCrJfsb4AdUBDZR-m1-CM=",
    )


async def test_healthz_returns_ok():
    app = create_app(_settings(chatwoot_enabled=False))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_chatwoot_router_follows_feature_flag():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(_settings(chatwoot_enabled=False))),
        base_url="http://test",
    ) as client:
        disabled = await client.post("/webhooks/chatwoot", content=b"{}")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(_settings(chatwoot_enabled=True))),
        base_url="http://test",
    ) as client:
        enabled = await client.post("/webhooks/chatwoot", content=b"{}")

    assert disabled.status_code == 404
    assert enabled.status_code != 404
