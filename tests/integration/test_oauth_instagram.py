import uuid
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from apps.api.main import create_app
from social_reply.application.account_management.meta_credentials import MetaAppCredentials
from social_reply.application.account_management.oauth import instagram
from social_reply.connectors.meta.client import appsecret_proof

pytestmark = pytest.mark.integration


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    async def set(self, key, value, ex=None):
        self.values[str(key)] = str(value).encode()

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


async def test_instagram_oauth_start_rejects_disabled_platform_before_state_storage(
    migrated_db, monkeypatch
):
    settings = instagram.get_settings().model_copy(update={"instagram_messaging_enabled": False})
    monkeypatch.setattr(instagram, "get_settings", lambda: settings)

    async def unexpected_store(*_args, **_kwargs):
        raise AssertionError("disabled OAuth must not store state")

    monkeypatch.setattr(instagram, "store_oauth_state", unexpected_store)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        csrf = await _login(client)
        response = await client.post(
            "/admin/oauth/instagram/start",
            data={
                "csrf_token": csrf,
                "tenant_id": "default",
                "brand_id": "default",
            },
        )
    assert response.status_code == 503
    assert "Instagram 集成已关闭" in response.text


async def test_instagram_oauth_callback_does_not_consume_state_when_disabled(
    migrated_db, monkeypatch
):
    settings = instagram.get_settings().model_copy(update={"instagram_messaging_enabled": False})
    monkeypatch.setattr(instagram, "get_settings", lambda: settings)

    async def unexpected_take(*_args, **_kwargs):
        raise AssertionError("disabled callback must preserve OAuth state")

    monkeypatch.setattr(instagram, "take_oauth_state", unexpected_take)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        response = await client.get("/admin/oauth/instagram/callback?code=code&state=state-token")
    assert response.status_code == 503
    assert "Instagram 集成已关闭" in response.text


@pytest.fixture
def instagram_env(monkeypatch):
    app = MetaAppCredentials(
        app_id="ig-app-id",
        app_secret="ig-app-secret",
        verify_token="ig-verify-token",
        public_id="instagram_oauth",
        platform_family="instagram",
    )

    async def configured_app(_tenant_id: str):
        return app

    monkeypatch.setattr(instagram, "instagram_app_credentials", configured_app)

    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "api.instagram.com":
            return httpx.Response(
                200,
                json={
                    "access_token": "short-token",
                    "user_id": "ig-42",
                    "permissions": [
                        "instagram_business_basic",
                        "instagram_business_manage_messages",
                    ],
                },
            )
        if request.url.path == "/access_token":
            return httpx.Response(
                200,
                json={"access_token": "long-token", "token_type": "bearer", "expires_in": 5184000},
            )
        if request.url.path.endswith("/me"):
            assert request.url.params["appsecret_proof"] == appsecret_proof(
                "long-token", "ig-app-secret"
            )
            return httpx.Response(
                200,
                json={
                    "user_id": "ig-42",
                    "username": "shop42",
                    "name": "Shop 42",
                    "profile_picture_url": "https://images.test/42.jpg",
                },
            )
        return httpx.Response(404)

    monkeypatch.setattr(
        instagram,
        "_instagram_client",
        lambda **kwargs: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), timeout=15, **kwargs
        ),
    )

    submitted: dict = {}
    job_id = uuid.uuid4()

    async def fake_submit(**kwargs):
        submitted.update(kwargs)
        return job_id

    async def fake_dispatch(actor, *args, inline=None):
        submitted["dispatched"] = True

    monkeypatch.setattr(instagram, "submit_provisioning_job", fake_submit)
    monkeypatch.setattr(instagram, "dispatch_actor", fake_dispatch)
    return {"calls": calls, "submitted": submitted, "job_id": job_id}


async def test_instagram_login_oauth_submits_standalone_account(instagram_env, migrated_db):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="https://test",
        follow_redirects=False,
    ) as client:
        csrf = await _login(client)
        start = await client.post(
            "/admin/oauth/instagram/start",
            data={"csrf_token": csrf, "tenant_id": "default", "brand_id": "brand-ig"},
        )
        assert start.status_code == 303
        query = parse_qs(urlparse(start.headers["location"]).query)
        assert query["enable_fb_login"] == ["0"]
        assert query["client_id"] == ["ig-app-id"]
        assert "instagram_business_manage_messages" in query["scope"][0]
        assert "instagram_business_manage_comments" not in query["scope"][0]

        callback = await client.get(
            f"/admin/oauth/instagram/callback?code=code-42&state={query['state'][0]}"
        )

    assert callback.status_code == 303
    assert callback.headers["location"] == f"/admin/jobs/{instagram_env['job_id']}"
    submitted = instagram_env["submitted"]
    assert submitted["tenant_id"] == "default"
    assert submitted["brand_id"] == "brand-ig"
    assert submitted["platform"] == "instagram"
    assert submitted["request"]["external_account_id"] == "ig-42"
    assert submitted["request"]["name"] == "@shop42"
    assert submitted["request"]["app_id"] == "ig-app-id"
    assert submitted["request"]["app_public_id"] == "instagram_oauth"
    assert submitted["request"]["instagram_login_mode"] == "instagram_login"
    assert submitted["request"]["enable_dm"] is True
    assert submitted["request"]["enable_comments"] is False
    assert submitted["request"]["automation_default"] == "BOT_DRAFT_ONLY"
    assert submitted["secrets"] == {
        "access_token": "long-token",
        "app_secret": "ig-app-secret",
        "verify_token": "ig-verify-token",
    }
    assert instagram_env["submitted"]["dispatched"] is True
    assert [request.url.host for request in instagram_env["calls"]] == [
        "api.instagram.com",
        "graph.instagram.com",
        "graph.instagram.com",
    ]


async def test_instagram_login_defaults_to_active_comments_when_enabled(
    instagram_env, migrated_db, monkeypatch
):
    settings = instagram.get_settings().model_copy(
        update={"meta_comment_reply_enabled": True, "meta_auto_reply_enabled": True}
    )
    monkeypatch.setattr(instagram, "get_settings", lambda: settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="https://test",
        follow_redirects=False,
    ) as client:
        csrf = await _login(client)
        start = await client.post(
            "/admin/oauth/instagram/start",
            data={"csrf_token": csrf, "tenant_id": "default", "brand_id": "brand-ig"},
        )
        query = parse_qs(urlparse(start.headers["location"]).query)
        assert "instagram_business_manage_comments" in query["scope"][0]
        callback = await client.get(
            f"/admin/oauth/instagram/callback?code=code-42&state={query['state'][0]}"
        )

    assert callback.status_code == 303
    assert instagram_env["submitted"]["request"]["enable_comments"] is True
    assert instagram_env["submitted"]["request"]["automation_default"] == "BOT_ACTIVE"


async def test_instagram_callback_state_is_one_time(instagram_env, migrated_db):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="https://test",
        follow_redirects=False,
    ) as client:
        csrf = await _login(client)
        start = await client.post(
            "/admin/oauth/instagram/start",
            data={"csrf_token": csrf, "tenant_id": "default", "brand_id": "brand-ig"},
        )
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
        first = await client.get(f"/admin/oauth/instagram/callback?code=one&state={state}")
        replay = await client.get(f"/admin/oauth/instagram/callback?code=two&state={state}")

    assert first.status_code == 303
    assert replay.status_code == 400
