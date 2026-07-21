"""X OAuth 1.0a account connection tests using deployment-level App keys."""

import uuid

import httpx
import pytest

from apps.api.main import create_app
from social_reply.application.account_management.oauth import x as oauth_connect
from social_reply.shared.config import get_settings

pytestmark = pytest.mark.integration

_REQ_TOKEN = "req-token-1"
_REQ_SECRET = "req-secret-1"
_ACCESS_TOKEN = "acc-token-9"
_ACCESS_SECRET = "acc-secret-9"


def _x_oauth_transport(calls: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if request.url.path == "/oauth/request_token":
            return httpx.Response(
                200,
                text=(
                    f"oauth_token={_REQ_TOKEN}&oauth_token_secret={_REQ_SECRET}"
                    "&oauth_callback_confirmed=true"
                ),
                headers=headers,
            )
        if request.url.path == "/oauth/access_token":
            return httpx.Response(
                200,
                text=(
                    f"oauth_token={_ACCESS_TOKEN}&oauth_token_secret={_ACCESS_SECRET}"
                    "&user_id=987654321&screen_name=newbot"
                ),
                headers=headers,
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    async def set(self, key, value, ex=None):
        self.values[str(key)] = str(value).encode()

    async def get(self, key):
        return self.values.get(str(key))

    async def delete(self, key):
        return int(self.values.pop(str(key), None) is not None)

    def pipeline(self, transaction=True):
        return FakePipeline(self)

    async def aclose(self):
        pass


class FakePipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self.redis = redis
        self.key = ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def get(self, key):
        self.key = str(key)

    def delete(self, key):
        self.key = str(key)

    async def execute(self):
        value = self.redis.values.pop(self.key, None)
        return value, int(value is not None)


async def _login(client: httpx.AsyncClient) -> str:
    await client.get("/admin/login")
    csrf = client.cookies["reply_admin_csrf"]
    await client.post(
        "/admin/login",
        data={"csrf_token": csrf, "username": "admin", "password": "test-admin-password"},
    )
    return csrf


@pytest.fixture
def oauth_env(monkeypatch):
    calls: list[httpx.Request] = []
    transport = _x_oauth_transport(calls)
    redis = FakeRedis()

    monkeypatch.setattr(
        oauth_connect,
        "_http_client",
        lambda: httpx.AsyncClient(transport=transport, timeout=15),
    )
    monkeypatch.setattr(oauth_connect, "_redis", lambda: redis)

    submitted: dict = {}
    job_id = uuid.uuid4()

    async def fake_submit(**kwargs):
        submitted.update(kwargs)
        return job_id

    async def fake_dispatch(actor, *args, inline=None):
        submitted["dispatched"] = True

    monkeypatch.setattr(oauth_connect, "submit_provisioning_job", fake_submit)
    monkeypatch.setattr(oauth_connect, "dispatch_actor", fake_dispatch)
    return {"calls": calls, "submitted": submitted, "job_id": job_id, "redis": redis}


async def test_full_oauth_flow_uses_env_app_credentials(oauth_env):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="https://test",
        follow_redirects=False,
    ) as client:
        csrf = await _login(client)
        start = await client.post(
            "/admin/oauth/x/start",
            data={"csrf_token": csrf, "tenant_id": "default", "brand_id": "brand-x"},
        )
        assert start.status_code == 303
        assert start.headers["location"] == (
            f"https://api.x.com/oauth/authorize?oauth_token={_REQ_TOKEN}"
        )
        assert oauth_env["redis"].values

        callback = await client.get(
            f"/admin/oauth/x/callback?oauth_token={_REQ_TOKEN}&oauth_verifier=verifier-7"
        )
    assert callback.status_code == 303
    assert callback.headers["location"] == f"/admin/jobs/{oauth_env['job_id']}"

    submitted = oauth_env["submitted"]
    assert submitted["platform"] == "x"
    assert submitted["tenant_id"] == "default"
    assert submitted["brand_id"] == "brand-x"
    assert submitted["secrets"] == {
        "consumer_key": "ck-app",
        "consumer_secret": "cs-app",
        "access_token": _ACCESS_TOKEN,
        "access_token_secret": _ACCESS_SECRET,
    }
    assert submitted["request"]["name"] == "@newbot"
    assert submitted["request"]["environment"] == "oauth"
    assert submitted["dispatched"] is True
    assert oauth_env["redis"].values == {}

    assert [request.url.path for request in oauth_env["calls"]] == [
        "/oauth/request_token",
        "/oauth/access_token",
    ]
    assert oauth_env["calls"][0].content == b"x_auth_access_type=write"
    assert oauth_env["calls"][0].headers["authorization"].startswith("OAuth ")


async def test_state_is_one_time_and_supports_parallel_account_flows(oauth_env):
    await oauth_connect._store_state("token-a", {"request_token_secret": "secret-a"})
    await oauth_connect._store_state("token-b", {"request_token_secret": "secret-b"})

    assert await oauth_connect._take_state("token-a") == {
        "request_token_secret": "secret-a"
    }
    assert await oauth_connect._take_state("token-a") is None
    assert await oauth_connect._take_state("token-b") == {
        "request_token_secret": "secret-b"
    }


async def test_start_requires_login_csrf_and_config(oauth_env, monkeypatch):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="https://test",
        follow_redirects=False,
    ) as client:
        anonymous = await client.post(
            "/admin/oauth/x/start", data={"tenant_id": "default", "brand_id": "b"}
        )
        assert anonymous.headers["location"] == "/admin/login"

        await _login(client)
        no_csrf = await client.post(
            "/admin/oauth/x/start", data={"tenant_id": "default", "brand_id": "b"}
        )
        assert no_csrf.status_code == 403

        monkeypatch.setattr(oauth_connect, "x_app_credentials", lambda: None)
        csrf = client.cookies["reply_admin_csrf"]
        missing = await client.post(
            "/admin/oauth/x/start",
            data={"csrf_token": csrf, "tenant_id": "default", "brand_id": "b"},
        )
        assert missing.status_code == 422
        assert "X_API_KEY" in missing.text


async def test_callback_rejects_replay_and_handles_denial(oauth_env):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="https://test",
        follow_redirects=False,
    ) as client:
        denied = await client.get("/admin/oauth/x/callback?denied=unknown")
        assert denied.status_code == 200

        no_state = await client.get(
            "/admin/oauth/x/callback?oauth_token=missing&oauth_verifier=v"
        )
        assert no_state.status_code == 400


def test_settings_expose_postiz_style_x_app_credentials():
    settings = get_settings()
    assert settings.x_app_credentials == ("ck-app", "cs-app")
