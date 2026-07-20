"""X OAuth 一键授权接入流的集成测试:start → X 授权 → callback → provisioning 提交。

X 侧三个 OAuth 端点用 MockTransport 模拟;provisioning 提交用 spy 捕获,
断言 OAuth 层组装出与手工表单同构的数据(OAuth 层单一职责:授权换凭证)。
"""

import uuid

import httpx
import pytest
from authlib.integrations.httpx_client import AsyncOAuth1Client
from sqlalchemy import insert

from apps.api.main import create_app
from social_reply.application.account_management import oauth_connect
from social_reply.infrastructure.database import models
from social_reply.infrastructure.secret_crypto import encrypt_secret_bundle

pytestmark = pytest.mark.integration

_REQ_TOKEN = "req-token-1"
_REQ_SECRET = "req-secret-1"
_ACCESS_TOKEN = "acc-token-9"
_ACCESS_SECRET = "acc-secret-9"


def _x_oauth_transport(calls: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        form_type = {"Content-Type": "application/x-www-form-urlencoded"}
        if request.url.path == "/oauth/request_token":
            return httpx.Response(
                200,
                text=(
                    f"oauth_token={_REQ_TOKEN}&oauth_token_secret={_REQ_SECRET}"
                    "&oauth_callback_confirmed=true"
                ),
                headers=form_type,
            )
        if request.url.path == "/oauth/access_token":
            return httpx.Response(
                200,
                text=(
                    f"oauth_token={_ACCESS_TOKEN}&oauth_token_secret={_ACCESS_SECRET}"
                    "&user_id=987654321&screen_name=newbot"
                ),
                headers=form_type,
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


async def _seed_x_account(session) -> None:
    """现有 X 账号提供 consumer 凭证回退来源。"""
    await session.execute(
        insert(models.PlatformAccount).values(
            id=uuid.uuid4(),
            brand_id="b1",
            platform="x",
            name="existing",
            external_account_id="111",
            public_id="primary",
            credential_bundle=encrypt_secret_bundle(
                {
                    "consumer_key": "ck-app",
                    "consumer_secret": "cs-app",
                    "access_token": "at-old",
                    "access_token_secret": "ats-old",
                }
            ),
            status="active",
        )
    )
    await session.commit()


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
    calls: list[str] = []

    def factory(**kwargs):
        return AsyncOAuth1Client(transport=_x_oauth_transport(calls), **kwargs)

    monkeypatch.setattr(oauth_connect, "_oauth1_client", factory)

    submitted: dict = {}
    job_id = uuid.uuid4()

    async def fake_submit(**kwargs):
        submitted.update(kwargs)
        return job_id

    async def fake_dispatch(actor, *args, inline=None):
        submitted["dispatched"] = True

    monkeypatch.setattr(oauth_connect, "submit_provisioning_job", fake_submit)
    monkeypatch.setattr(oauth_connect, "dispatch_actor", fake_dispatch)
    return {"calls": calls, "submitted": submitted, "job_id": job_id}


async def test_full_oauth_flow_submits_provisioning(session, oauth_env):
    await _seed_x_account(session)
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
        assert start.headers["location"].startswith(
            f"https://api.x.com/oauth/authorize?oauth_token={_REQ_TOKEN}"
        )
        assert oauth_connect._STATE_COOKIE in client.cookies

        callback = await client.get(
            f"/admin/oauth/x/callback?oauth_token={_REQ_TOKEN}&oauth_verifier=verifier-7"
        )
    assert callback.status_code == 303
    assert callback.headers["location"] == f"/admin/jobs/{oauth_env['job_id']}"

    submitted = oauth_env["submitted"]
    assert submitted["platform"] == "x"
    assert submitted["tenant_id"] == "default"
    assert submitted["brand_id"] == "brand-x"
    assert submitted["actor"] == "user:admin"
    assert submitted["dispatched"] is True
    # 与手工表单同构:新账号 token 来自 OAuth 交换,consumer 沿用 App 凭证
    assert submitted["secrets"]["access_token"] == _ACCESS_TOKEN
    assert submitted["secrets"]["access_token_secret"] == _ACCESS_SECRET
    assert submitted["secrets"]["consumer_key"] == "ck-app"
    assert submitted["secrets"]["consumer_secret"] == "cs-app"
    assert submitted["request"]["name"] == "@newbot"
    # X 侧恰好两次调用:request_token + access_token
    assert oauth_env["calls"] == ["POST /oauth/request_token", "POST /oauth/access_token"]


async def test_start_requires_login_and_csrf(session, oauth_env):
    await _seed_x_account(session)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="https://test",
        follow_redirects=False,
    ) as client:
        anonymous = await client.post(
            "/admin/oauth/x/start", data={"tenant_id": "default", "brand_id": "b"}
        )
        assert anonymous.status_code == 303
        assert anonymous.headers["location"] == "/admin/login"

        await _login(client)
        no_csrf = await client.post(
            "/admin/oauth/x/start", data={"tenant_id": "default", "brand_id": "b"}
        )
        assert no_csrf.status_code == 403


async def test_start_without_consumer_credentials_shows_guidance(session, oauth_env):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="https://test",
        follow_redirects=False,
    ) as client:
        csrf = await _login(client)
        response = await client.post(
            "/admin/oauth/x/start",
            data={"csrf_token": csrf, "tenant_id": "default", "brand_id": "b"},
        )
    assert response.status_code == 422
    assert "consumer" in response.text


async def test_callback_rejects_token_mismatch_and_missing_state(session, oauth_env):
    await _seed_x_account(session)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="https://test",
        follow_redirects=False,
    ) as client:
        no_state = await client.get("/admin/oauth/x/callback?oauth_token=whatever&oauth_verifier=v")
        assert no_state.status_code == 400

        csrf = await _login(client)
        await client.post(
            "/admin/oauth/x/start",
            data={"csrf_token": csrf, "tenant_id": "default", "brand_id": "b"},
        )
        mismatch = await client.get(
            "/admin/oauth/x/callback?oauth_token=forged-token&oauth_verifier=v"
        )
        assert mismatch.status_code == 400
    assert "submitted" not in oauth_env or not oauth_env["submitted"].get("dispatched")


async def test_callback_denied_makes_no_changes(session, oauth_env):
    await _seed_x_account(session)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="https://test",
        follow_redirects=False,
    ) as client:
        response = await client.get("/admin/oauth/x/callback?denied=req-token-1")
    assert response.status_code == 200
    assert not oauth_env["submitted"]
