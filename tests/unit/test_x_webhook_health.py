import httpx
import pytest

from social_reply.application.event_ingestion import x_webhook_health


def _transport(webhooks: list[dict], calls: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": "bearer-x"})
        if request.method == "GET" and request.url.path == "/2/webhooks":
            return httpx.Response(200, json={"data": webhooks})
        if request.method == "PUT" and request.url.path.startswith("/2/webhooks/"):
            return httpx.Response(204)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


async def test_valid_webhook_is_left_alone():
    calls: list[str] = []
    result = await x_webhook_health._check_app(
        "ck",
        "cs",
        api_base_url="https://api.x.test",
        transport=_transport([{"id": "w1", "valid": True, "url": "https://cb"}], calls),
    )
    assert result == []
    assert "PUT /2/webhooks/w1" not in calls


async def test_invalid_webhook_triggers_crc_revalidation():
    calls: list[str] = []
    result = await x_webhook_health._check_app(
        "ck",
        "cs",
        api_base_url="https://api.x.test",
        transport=_transport([{"id": "w1", "valid": False, "url": "https://cb"}], calls),
    )
    assert result == ["w1"]
    assert calls == ["POST /oauth2/token", "GET /2/webhooks", "PUT /2/webhooks/w1"]


async def test_revalidation_failure_does_not_raise():
    """PUT 失败(CRC 端点不可达)只记日志留给下轮,不得中断 sweep。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": "bearer-x"})
        if request.method == "GET" and request.url.path == "/2/webhooks":
            return httpx.Response(200, json={"data": [{"id": "w1", "valid": False}]})
        return httpx.Response(400, json={"errors": [{"message": "CRC failed"}]})

    result = await x_webhook_health._check_app(
        "ck", "cs", api_base_url="https://api.x.test", transport=httpx.MockTransport(handler)
    )
    assert result == []


async def test_sweep_throttles_and_dedupes_by_consumer_key(monkeypatch):
    class _Account:
        id = "acc-1"
        credential_bundle = {"consumer_key": "ck", "consumer_secret": "cs"}
        config = {"api_base_url": "https://api.x.test"}

    checked: list[str] = []

    async def fake_list(platform):
        return [_Account(), _Account()]  # 同 app 两账号

    async def fake_check(consumer_key, consumer_secret, *, api_base_url, transport=None):
        checked.append(consumer_key)
        return []

    monkeypatch.setattr(x_webhook_health, "list_active_accounts_by_platform", fake_list)
    monkeypatch.setattr(x_webhook_health, "_check_app", fake_check)
    monkeypatch.setattr(
        x_webhook_health,
        "get_settings",
        lambda: type(
            "Settings",
            (),
            {"x_activity_enabled": True, "x_webhook_check_interval_seconds": 3600},
        )(),
    )

    x_webhook_health._last_check_at = None
    await x_webhook_health.ensure_x_webhooks_valid()
    assert checked == ["ck"]  # 同 consumer key 的账号只查一次

    await x_webhook_health.ensure_x_webhooks_valid()  # 间隔内:节流跳过
    assert checked == ["ck"]


@pytest.fixture(autouse=True)
def _reset_throttle():
    x_webhook_health._last_check_at = None
    yield
    x_webhook_health._last_check_at = None
