"""Meta OAuth 接入流集成测试:Facebook Page 单选直落 + Instagram 多选页。

Graph 四个端点(code 换 token、fb_exchange_token、me/accounts)用 MockTransport
模拟;provisioning 提交用 spy 捕获,断言 OAuth 层组装出与手工 Meta 表单同构的数据。
"""

import uuid

import httpx
import pytest
from sqlalchemy import insert

from apps.api.main import create_app
from social_reply.application.account_management.meta_credentials import MetaAppCredentials
from social_reply.application.account_management.oauth import meta
from social_reply.infrastructure.database import models
from social_reply.infrastructure.secret_crypto import encrypt_secret_bundle

pytestmark = pytest.mark.integration


def _graph_transport(calls: list[str], pages: list[dict]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        if path.endswith("/oauth/access_token"):
            params = request.url.params
            if params.get("grant_type") == "fb_exchange_token":
                return httpx.Response(200, json={"access_token": "LONG-USER-TOKEN"})
            return httpx.Response(200, json={"access_token": "SHORT-USER-TOKEN"})
        if path.endswith("/me/accounts"):
            return httpx.Response(200, json={"data": pages})
        return httpx.Response(404, json={"error": {"message": "unexpected"}})

    return httpx.MockTransport(handler)


async def _seed_meta_app(session) -> None:
    await session.execute(
        insert(models.PlatformApp).values(
            id=uuid.uuid4(),
            tenant_id="default",
            platform_family="meta",
            name="Meta App",
            external_app_id="app-123",
            public_id="meta_app",
            credential_bundle=encrypt_secret_bundle(
                {"app_secret": "meta-app-secret", "verify_token": "vt-1"}
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


async def test_meta_oauth_start_rejects_disabled_platform_before_state_storage(
    migrated_db, monkeypatch
):
    settings = meta.get_settings().model_copy(update={"facebook_messenger_enabled": False})
    monkeypatch.setattr(meta, "get_settings", lambda: settings)

    async def unexpected_store(*_args, **_kwargs):
        raise AssertionError("disabled OAuth must not store state")

    monkeypatch.setattr(meta, "store_oauth_state", unexpected_store)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        csrf = await _login(client)
        response = await client.post(
            "/admin/oauth/meta/start",
            data={
                "csrf_token": csrf,
                "platform": "facebook",
                "tenant_id": "default",
                "brand_id": "default",
            },
        )
    assert response.status_code == 503
    assert "平台集成已关闭" in response.text


async def test_meta_oauth_callback_does_not_consume_state_when_target_is_disabled(
    migrated_db, monkeypatch
):
    settings = meta.get_settings().model_copy(update={"instagram_messaging_enabled": False})
    monkeypatch.setattr(meta, "get_settings", lambda: settings)

    async def pending_state(_namespace, _key):
        return {"platform": "instagram"}

    async def unexpected_take(*_args, **_kwargs):
        raise AssertionError("disabled callback must preserve OAuth state")

    monkeypatch.setattr(meta, "peek_oauth_state", pending_state)
    monkeypatch.setattr(meta, "take_oauth_state", unexpected_take)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        response = await client.get("/admin/oauth/meta/callback?code=code&state=state-token")
    assert response.status_code == 503
    assert "平台集成已关闭" in response.text


@pytest.fixture
def meta_env(monkeypatch):
    calls: list[str] = []

    async def stored_app(_tenant_id: str):
        return MetaAppCredentials(
            app_id="app-123",
            app_secret="meta-app-secret",
            verify_token="vt-1",
            public_id="meta_app",
            platform_family="meta",
        )

    monkeypatch.setattr(meta, "facebook_app_credentials", stored_app)
    pages_holder: dict = {"pages": []}

    def factory(**kwargs):
        return httpx.AsyncClient(
            base_url=meta._GRAPH_BASE,
            transport=_graph_transport(calls, pages_holder["pages"]),
            **kwargs,
        )

    monkeypatch.setattr(meta, "_graph_client", factory)

    submitted: dict = {}
    job_id = uuid.uuid4()

    async def fake_submit(**kwargs):
        submitted.update(kwargs)
        return job_id

    async def fake_dispatch(actor, *args, inline=None):
        submitted["dispatched"] = True

    monkeypatch.setattr(meta, "submit_provisioning_job", fake_submit)
    monkeypatch.setattr(meta, "dispatch_actor", fake_dispatch)
    return {"calls": calls, "submitted": submitted, "job_id": job_id, "pages": pages_holder}


async def _run_start(client: httpx.AsyncClient, csrf: str, platform: str) -> httpx.Response:
    return await client.post(
        "/admin/oauth/meta/start",
        data={
            "csrf_token": csrf,
            "platform": platform,
            "tenant_id": "default",
            "brand_id": "brand-m",
        },
    )


async def test_full_facebook_flow_submits_provisioning(session, meta_env):
    await _seed_meta_app(session)
    meta_env["pages"]["pages"] = [{"id": "page-9", "name": "Acme", "access_token": "PAGE-TOKEN-9"}]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="https://test",
        follow_redirects=False,
    ) as client:
        csrf = await _login(client)
        start = await _run_start(client, csrf, "facebook")
        assert "pages_messaging" in start.headers["location"]
        assert "pages_manage_engagement" not in start.headers["location"]
        state_token = start.headers["location"].split("state=")[1].split("&")[0]
        callback = await client.get(
            f"/admin/oauth/meta/callback?code=auth-code-9&state={state_token}"
        )
    assert callback.status_code == 303
    assert callback.headers["location"] == f"/admin/jobs/{meta_env['job_id']}"
    submitted = meta_env["submitted"]
    assert submitted["platform"] == "facebook"
    assert submitted["tenant_id"] == "default"
    assert submitted["brand_id"] == "brand-m"
    assert submitted["request"]["external_account_id"] == "page-9"
    assert submitted["request"]["app_id"] == "app-123"
    assert submitted["request"]["app_public_id"] == "meta_app"
    assert submitted["request"]["enable_dm"] is True
    assert submitted["request"]["enable_comments"] is False
    assert submitted["request"]["automation_default"] == "BOT_DRAFT_ONLY"
    assert submitted["secrets"]["access_token"] == "PAGE-TOKEN-9"
    assert submitted["secrets"]["app_secret"] == "meta-app-secret"
    assert submitted["secrets"]["verify_token"] == "vt-1"
    # 交换链路:短 token → 长 token → me/accounts
    assert sum("access_token" in c for c in meta_env["calls"]) == 2
    assert any(c.endswith("/me/accounts") for c in meta_env["calls"])


async def test_facebook_flow_defaults_to_active_comments_when_enabled(
    session, meta_env, monkeypatch
):
    await _seed_meta_app(session)
    settings = meta.get_settings().model_copy(
        update={"meta_comment_reply_enabled": True, "meta_auto_reply_enabled": True}
    )
    monkeypatch.setattr(meta, "get_settings", lambda: settings)
    meta_env["pages"]["pages"] = [
        {"id": "page-9", "name": "Acme", "access_token": "PAGE-TOKEN-9"}
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="https://test",
        follow_redirects=False,
    ) as client:
        csrf = await _login(client)
        start = await _run_start(client, csrf, "facebook")
        location = start.headers["location"]
        assert "pages_read_engagement" in location
        assert "pages_read_user_content" in location
        assert "pages_manage_engagement" in location
        assert "auth_type=rerequest" in location
        state_token = location.split("state=")[1].split("&")[0]
        callback = await client.get(
            f"/admin/oauth/meta/callback?code=auth-code-9&state={state_token}"
        )

    assert callback.status_code == 303
    assert meta_env["submitted"]["request"]["enable_comments"] is True
    assert meta_env["submitted"]["request"]["automation_default"] == "BOT_ACTIVE"


async def test_instagram_filters_pages_without_ig_and_shows_picker(session, meta_env):
    await _seed_meta_app(session)
    meta_env["pages"]["pages"] = [
        {"id": "page-a", "name": "No IG Page", "access_token": "T-A"},
        {
            "id": "page-b",
            "name": "Shop B",
            "access_token": "T-B",
            "instagram_business_account": {"id": "ig-b", "username": "shopb"},
        },
        {
            "id": "page-c",
            "name": "Shop C",
            "access_token": "T-C",
            "instagram_business_account": {"id": "ig-c", "username": "shopc"},
        },
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="https://test",
        follow_redirects=False,
    ) as client:
        csrf = await _login(client)
        start = await _run_start(client, csrf, "instagram")
        assert "instagram_manage_messages" in start.headers["location"]
        assert "instagram_manage_comments" not in start.headers["location"]
        state_token = start.headers["location"].split("state=")[1].split("&")[0]
        picker = await client.get(f"/admin/oauth/meta/callback?code=code-ig&state={state_token}")
    # 无 IG 的 Page 被过滤,只剩两个 IG 候选,渲染选择页
    assert picker.status_code == 200
    assert "shopb" in picker.text and "shopc" in picker.text
    assert "No IG Page" not in picker.text
    assert meta._PICK_COOKIE in picker.cookies
    assert not meta_env["submitted"]  # 选择前不提交


async def test_facebook_login_instagram_defaults_to_active_comments_when_enabled(
    session, meta_env, monkeypatch
):
    await _seed_meta_app(session)
    settings = meta.get_settings().model_copy(
        update={"meta_comment_reply_enabled": True, "meta_auto_reply_enabled": True}
    )
    monkeypatch.setattr(meta, "get_settings", lambda: settings)
    meta_env["pages"]["pages"] = [
        {
            "id": "page-b",
            "name": "Shop B",
            "access_token": "T-B",
            "instagram_business_account": {"id": "ig-b", "username": "shopb"},
        }
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="https://test",
        follow_redirects=False,
    ) as client:
        csrf = await _login(client)
        start = await _run_start(client, csrf, "instagram")
        location = start.headers["location"]
        assert "pages_read_engagement" in location
        assert "instagram_manage_comments" in location
        assert "auth_type=rerequest" in location
        state_token = location.split("state=")[1].split("&")[0]
        callback = await client.get(
            f"/admin/oauth/meta/callback?code=code-ig&state={state_token}"
        )

    assert callback.status_code == 303
    assert meta_env["submitted"]["request"]["enable_comments"] is True
    assert meta_env["submitted"]["request"]["automation_default"] == "BOT_ACTIVE"


async def test_instagram_picker_does_not_consume_state_when_platform_is_disabled(
    session, meta_env, monkeypatch
):
    await _seed_meta_app(session)
    meta_env["pages"]["pages"] = [
        {
            "id": "page-b",
            "name": "Shop B",
            "access_token": "T-B",
            "instagram_business_account": {"id": "ig-b", "username": "shopb"},
        },
        {
            "id": "page-c",
            "name": "Shop C",
            "access_token": "T-C",
            "instagram_business_account": {"id": "ig-c", "username": "shopc"},
        },
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="https://test",
        follow_redirects=False,
    ) as client:
        csrf = await _login(client)
        start = await _run_start(client, csrf, "instagram")
        state_token = start.headers["location"].split("state=")[1].split("&")[0]
        picker = await client.get(f"/admin/oauth/meta/callback?code=code-ig&state={state_token}")
        assert picker.status_code == 200
        disabled = meta.get_settings().model_copy(update={"instagram_messaging_enabled": False})
        monkeypatch.setattr(meta, "get_settings", lambda: disabled)

        async def unexpected_take(*_args, **_kwargs):
            raise AssertionError("disabled picker must preserve OAuth state")

        monkeypatch.setattr(meta, "take_oauth_state", unexpected_take)
        select_response = await client.post(
            "/admin/oauth/meta/select",
            data={"csrf_token": client.cookies["reply_admin_csrf"], "choice": "1"},
        )
    assert select_response.status_code == 503
    assert "平台集成已关闭" in select_response.text
    assert not meta_env["submitted"]


async def test_instagram_select_finalizes_with_ig_id(session, meta_env):
    await _seed_meta_app(session)
    meta_env["pages"]["pages"] = [
        {
            "id": "page-b",
            "name": "Shop B",
            "access_token": "T-B",
            "instagram_business_account": {"id": "ig-b", "username": "shopb"},
        },
        {
            "id": "page-c",
            "name": "Shop C",
            "access_token": "T-C",
            "instagram_business_account": {"id": "ig-c", "username": "shopc"},
        },
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="https://test",
        follow_redirects=False,
    ) as client:
        csrf = await _login(client)
        start = await _run_start(client, csrf, "instagram")
        state_token = start.headers["location"].split("state=")[1].split("&")[0]
        await client.get(f"/admin/oauth/meta/callback?code=code-ig&state={state_token}")
        select = await client.post(
            "/admin/oauth/meta/select",
            data={"csrf_token": client.cookies["reply_admin_csrf"], "choice": "1"},
        )
    assert select.status_code == 303
    submitted = meta_env["submitted"]
    assert submitted["platform"] == "instagram"
    # 选了第二个(index 1)= Shop C,落库用 IG 账号 id,不是 Page id
    assert submitted["request"]["external_account_id"] == "ig-c"
    assert submitted["request"]["page_id"] == "page-c"
    assert submitted["request"]["name"] == "@shopc"
    assert submitted["request"]["enable_dm"] is True
    assert submitted["request"]["enable_comments"] is False
    assert submitted["secrets"]["access_token"] == "T-C"


async def test_start_requires_login_csrf_and_valid_platform(session, meta_env):
    await _seed_meta_app(session)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="https://test",
        follow_redirects=False,
    ) as client:
        anon = await client.post(
            "/admin/oauth/meta/start", data={"platform": "facebook", "tenant_id": "default"}
        )
        assert anon.status_code == 303 and anon.headers["location"] == "/admin/login"
        csrf = await _login(client)
        no_csrf = await client.post(
            "/admin/oauth/meta/start", data={"platform": "facebook", "tenant_id": "default"}
        )
        assert no_csrf.status_code == 403
        bad_platform = await client.post(
            "/admin/oauth/meta/start",
            data={"csrf_token": csrf, "platform": "twitter", "tenant_id": "default"},
        )
        assert bad_platform.status_code == 422


async def test_start_without_meta_app_shows_guidance(session, meta_env, monkeypatch):
    async def no_app(_tenant_id: str):
        return None

    monkeypatch.setattr(meta, "facebook_app_credentials", no_app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="https://test",
        follow_redirects=False,
    ) as client:
        csrf = await _login(client)
        response = await _run_start(client, csrf, "facebook")
    assert response.status_code == 422
    assert "Meta App" in response.text
