"""X OAuth 1.0a account connection tests using deployment-level App keys."""

import hashlib
import logging
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from apps.api.main import create_app
from social_reply.application.account_management.oauth import common as oauth_common
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
        self.commands: list[tuple[str, str]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def get(self, key):
        self.commands.append(("get", str(key)))

    def delete(self, key):
        self.commands.append(("delete", str(key)))

    async def execute(self):
        results = []
        for command, key in self.commands:
            if command == "get":
                results.append(self.redis.values.get(key))
            else:
                results.append(int(self.redis.values.pop(key, None) is not None))
        return results


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
    monkeypatch.setattr(oauth_common, "oauth_redis", lambda: redis)

    submitted: dict = {}
    job_id = uuid.uuid4()

    async def fake_submit(**kwargs):
        submitted.update(kwargs)
        return job_id

    async def fake_process(processed_job_id):
        submitted["processed_job_id"] = processed_job_id
        submitted.setdefault("process_calls", []).append(processed_job_id)
        return "COMPLETED"

    monkeypatch.setattr(oauth_connect, "submit_provisioning_job", fake_submit)
    monkeypatch.setattr(oauth_connect, "process_provisioning_job", fake_process)
    return {"calls": calls, "submitted": submitted, "job_id": job_id, "redis": redis}


async def test_full_oauth_flow_uses_env_app_credentials(oauth_env, migrated_db):
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
    assert callback.headers["location"] == "/admin/accounts?provider=x&status=connected"
    assert callback.headers["cache-control"] == "no-store"
    assert callback.headers["pragma"] == "no-cache"
    assert callback.headers["referrer-policy"] == "no-referrer"

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
    assert submitted["processed_job_id"] == str(oauth_env["job_id"])
    assert oauth_env["redis"].values == {}

    assert [request.url.path for request in oauth_env["calls"]] == [
        "/oauth/request_token",
        "/oauth/access_token",
    ]
    request_token_call = oauth_env["calls"][0]
    assert request_token_call.content == b""
    assert "x_auth_access_type" not in request_token_call.url.params
    assert "x_auth_access_type" not in request_token_call.headers["authorization"]
    assert "oauth_callback=" in request_token_call.headers["authorization"]
    assert request_token_call.headers["authorization"].startswith("OAuth ")


@pytest.mark.parametrize(
    "user_agent",
    [
        "Mozilla/5.0 Chrome/126.0.0.0 Safari/537.36",
        "Mozilla/5.0 Version/17.5 Safari/605.1.15",
    ],
)
async def test_callback_without_cookie_completes_then_login_returns_to_accounts(
    oauth_env,
    migrated_db,
    user_agent,
):
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://test",
        follow_redirects=False,
    ) as initiating_client:
        csrf = await _login(initiating_client)
        start = await initiating_client.post(
            "/admin/oauth/x/start",
            data={"csrf_token": csrf, "tenant_id": "default", "brand_id": "brand-x"},
        )
        assert start.status_code == 303

    result_path = "/admin/accounts?provider=x&status=connected"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="https://test",
        follow_redirects=False,
        headers={"User-Agent": user_agent, "X-Forwarded-Proto": "https"},
    ) as callback_client:
        callback = await callback_client.get(
            f"/admin/oauth/x/callback?oauth_token={_REQ_TOKEN}&oauth_verifier=verifier-7"
        )
        assert callback.status_code == 303
        assert callback.headers["location"] == (
            "/admin/login?next=%2Fadmin%2Faccounts%3Fprovider%3Dx%26status%3Dconnected"
        )
        assert oauth_env["submitted"]["process_calls"] == [str(oauth_env["job_id"])]

        replay = await callback_client.get(
            f"/admin/oauth/x/callback?oauth_token={_REQ_TOKEN}&oauth_verifier=verifier-7"
        )
        assert replay.status_code == 303
        assert "oauth_state_missing" in replay.headers["location"]
        assert oauth_env["submitted"]["process_calls"] == [str(oauth_env["job_id"])]

        login_page = await callback_client.get(callback.headers["location"])
        assert login_page.status_code == 200
        assert (
            'name="next" value="/admin/accounts?provider=x&amp;status=connected"' in login_page.text
        )
        csrf = callback_client.cookies["reply_admin_csrf"]
        login = await callback_client.post(
            "/admin/login",
            data={
                "csrf_token": csrf,
                "next": result_path,
                "username": "admin",
                "password": "test-admin-password",
            },
        )
        assert login.status_code == 303
        assert login.headers["location"] == result_path


async def test_callback_waits_when_worker_claims_job_first(
    oauth_env,
    migrated_db,
    monkeypatch,
):
    async def skipped_process(_job_id):
        return "SKIPPED_NOT_CLAIMABLE"

    async def completed_job(_job_id):
        return type("Job", (), {"status": "COMPLETED"})()

    monkeypatch.setattr(oauth_connect, "process_provisioning_job", skipped_process)
    monkeypatch.setattr(oauth_connect, "_load_provisioning_job", completed_job)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="https://test",
        follow_redirects=False,
    ) as client:
        csrf = await _login(client)
        await client.post(
            "/admin/oauth/x/start",
            data={"csrf_token": csrf, "tenant_id": "default", "brand_id": "brand-x"},
        )
        callback = await client.get(
            f"/admin/oauth/x/callback?oauth_token={_REQ_TOKEN}&oauth_verifier=verifier-7"
        )
    assert callback.status_code == 303
    assert callback.headers["location"] == "/admin/accounts?provider=x&status=connected"


async def test_claim_race_timeout_is_processing_not_failure(monkeypatch):
    async def processing_job(_job_id):
        return type("Job", (), {"status": "PROCESSING"})()

    monkeypatch.setattr(oauth_connect, "_load_provisioning_job", processing_job)
    result = await oauth_connect._resolve_provisioning_result(
        uuid.uuid4(),
        "SKIPPED_NOT_CLAIMABLE",
        timeout_seconds=0.01,
    )
    assert result == "PROCESSING"


async def test_retryable_provisioning_failure_is_reported_as_processing(
    oauth_env,
    migrated_db,
    monkeypatch,
):
    async def retryable_failure(_job_id):
        return "FAILED"

    monkeypatch.setattr(oauth_connect, "process_provisioning_job", retryable_failure)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="https://test",
        follow_redirects=False,
    ) as client:
        csrf = await _login(client)
        await client.post(
            "/admin/oauth/x/start",
            data={"csrf_token": csrf, "tenant_id": "default", "brand_id": "brand-x"},
        )
        callback = await client.get(
            f"/admin/oauth/x/callback?oauth_token={_REQ_TOKEN}&oauth_verifier=verifier-7"
        )
    assert callback.status_code == 303
    assert callback.headers["location"] == (
        "/admin/accounts?provider=x&status=processing&code=provisioning_in_progress"
    )


async def test_post_submit_dispatch_failure_is_reported_as_processing(
    oauth_env,
    migrated_db,
    monkeypatch,
):
    async def fail_dispatch(*_args, **_kwargs):
        raise TimeoutError("broker timeout")

    monkeypatch.setattr(oauth_connect, "dispatch_actor", fail_dispatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="https://test",
        follow_redirects=False,
    ) as client:
        csrf = await _login(client)
        await client.post(
            "/admin/oauth/x/start",
            data={"csrf_token": csrf, "tenant_id": "default", "brand_id": "brand-x"},
        )
        callback = await client.get(
            f"/admin/oauth/x/callback?oauth_token={_REQ_TOKEN}&oauth_verifier=verifier-7"
        )
    assert callback.status_code == 303
    assert callback.headers["location"] == (
        "/admin/accounts?provider=x&status=processing&code=provisioning_in_progress"
    )


async def test_submit_failure_is_reported_as_terminal_error(
    oauth_env,
    migrated_db,
    monkeypatch,
):
    async def fail_submit(**_kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(oauth_connect, "submit_provisioning_job", fail_submit)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="https://test",
        follow_redirects=False,
    ) as client:
        csrf = await _login(client)
        await client.post(
            "/admin/oauth/x/start",
            data={"csrf_token": csrf, "tenant_id": "default", "brand_id": "brand-x"},
        )
        callback = await client.get(
            f"/admin/oauth/x/callback?oauth_token={_REQ_TOKEN}&oauth_verifier=verifier-7"
        )
    assert callback.status_code == 303
    assert callback.headers["location"] == (
        "/admin/accounts?provider=x&status=error&code=provisioning_submit_failed"
    )


async def test_callback_unhandled_error_is_no_store(
    oauth_env,
    migrated_db,
    monkeypatch,
):
    async def fail_state(_namespace, _key):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(oauth_connect, "take_oauth_state", fail_state)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="https://test",
        follow_redirects=False,
    ) as client:
        response = await client.get(
            "/admin/oauth/x/callback?oauth_token=opaque&oauth_verifier=opaque"
        )
    assert response.status_code == 500
    assert response.text == "OAuth callback failed"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["referrer-policy"] == "no-referrer"


async def test_expired_transaction_does_not_restart_oauth(oauth_env, migrated_db):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="https://test",
        follow_redirects=False,
    ) as client:
        csrf = await _login(client)
        await client.post(
            "/admin/oauth/x/start",
            data={"csrf_token": csrf, "tenant_id": "default", "brand_id": "brand-x"},
        )
        state = await oauth_common.take_oauth_state("x", _REQ_TOKEN)
        assert state is not None
        state["created_at"] = (datetime.now(UTC) - timedelta(minutes=11)).isoformat()
        await oauth_common.store_oauth_state("x", _REQ_TOKEN, state)

        callback = await client.get(
            f"/admin/oauth/x/callback?oauth_token={_REQ_TOKEN}&oauth_verifier=verifier-7"
        )
    assert callback.status_code == 303
    assert "oauth_transaction_invalid" in callback.headers["location"]
    assert [request.url.path for request in oauth_env["calls"]] == ["/oauth/request_token"]
    assert "process_calls" not in oauth_env["submitted"]


@pytest.mark.parametrize("exchange_status", [401, 403])
async def test_token_exchange_rejection_does_not_restart_oauth(
    oauth_env,
    migrated_db,
    monkeypatch,
    caplog,
    exchange_status,
):
    caplog.set_level(logging.INFO, logger=oauth_connect.__name__)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="https://test",
        follow_redirects=False,
    ) as client:
        csrf = await _login(client)
        await client.post(
            "/admin/oauth/x/start",
            data={"csrf_token": csrf, "tenant_id": "default", "brand_id": "brand-x"},
        )

        async def reject_exchange(**kwargs):
            request = httpx.Request("POST", "https://api.x.com/oauth/access_token")
            response = httpx.Response(exchange_status, request=request, text="rejected")
            raise httpx.HTTPStatusError("rejected", request=request, response=response)

        monkeypatch.setattr(oauth_connect, "_access_token", reject_exchange)
        callback = await client.get(
            f"/admin/oauth/x/callback?oauth_token={_REQ_TOKEN}&oauth_verifier=verifier-7"
        )
    assert callback.status_code == 303
    assert callback.headers["location"] == (
        "/admin/accounts?provider=x&status=error&code=x_token_exchange_rejected"
    )
    assert [request.url.path for request in oauth_env["calls"]] == ["/oauth/request_token"]
    assert "process_calls" not in oauth_env["submitted"]
    assert _REQ_TOKEN not in caplog.text
    assert _REQ_SECRET not in caplog.text
    assert "verifier-7" not in caplog.text
    assert hashlib.sha256(_REQ_TOKEN.encode()).hexdigest()[:12] in caplog.text


async def test_state_is_one_time_and_supports_parallel_account_flows(oauth_env):
    await oauth_common.store_oauth_state("x", "token-a", {"request_token_secret": "secret-a"})
    await oauth_common.store_oauth_state("x", "token-b", {"request_token_secret": "secret-b"})

    assert await oauth_common.take_oauth_state("x", "token-a") == {
        "request_token_secret": "secret-a"
    }
    assert await oauth_common.take_oauth_state("x", "token-a") is None
    assert await oauth_common.take_oauth_state("x", "token-b") == {
        "request_token_secret": "secret-b"
    }


async def test_start_requires_login_csrf_and_config(oauth_env, monkeypatch, migrated_db):
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


async def test_oauth_callback_rechecks_feature_flags(oauth_env, migrated_db, monkeypatch):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="https://test",
        follow_redirects=False,
    ) as client:
        csrf = await _login(client)
        await client.post(
            "/admin/oauth/x/start",
            data={"csrf_token": csrf, "tenant_id": "default", "brand_id": "b"},
        )
        settings = get_settings().model_copy(
            update={
                "x_legacy_dm_enabled": False,
                "x_activity_enabled": False,
                "xchat_enabled": False,
            }
        )
        monkeypatch.setattr(oauth_connect, "get_settings", lambda: settings)
        callback = await client.get(
            f"/admin/oauth/x/callback?oauth_token={_REQ_TOKEN}&oauth_verifier=v"
        )

    assert callback.status_code == 303
    assert "x_integration_disabled" in callback.headers["location"]
    assert [request.url.path for request in oauth_env["calls"]] == ["/oauth/request_token"]
    assert "processed_job_id" not in oauth_env["submitted"]


async def test_oauth_callback_rejects_pin_when_xchat_was_disabled_mid_flow(
    oauth_env, migrated_db, monkeypatch
):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="https://test",
        follow_redirects=False,
    ) as client:
        csrf = await _login(client)
        await client.post(
            "/admin/oauth/x/start",
            data={
                "csrf_token": csrf,
                "tenant_id": "default",
                "brand_id": "b",
                "xchat_pin": "1234",
            },
        )
        settings = get_settings().model_copy(
            update={"x_legacy_dm_enabled": True, "xchat_enabled": False}
        )
        monkeypatch.setattr(oauth_connect, "get_settings", lambda: settings)
        callback = await client.get(
            f"/admin/oauth/x/callback?oauth_token={_REQ_TOKEN}&oauth_verifier=v"
        )

    assert callback.status_code == 303
    assert "xchat_disabled" in callback.headers["location"]
    assert [request.url.path for request in oauth_env["calls"]] == ["/oauth/request_token"]
    assert "processed_job_id" not in oauth_env["submitted"]


async def test_oauth_start_rejects_when_all_x_stacks_are_disabled(
    oauth_env, migrated_db, monkeypatch
):
    settings = get_settings().model_copy(
        update={
            "x_legacy_dm_enabled": False,
            "x_activity_enabled": False,
            "xchat_enabled": False,
        }
    )
    monkeypatch.setattr(oauth_connect, "get_settings", lambda: settings)
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
    assert response.status_code == 503
    assert "X 集成已关闭" in response.text
    assert oauth_env["calls"] == []


async def test_callback_rejects_replay_and_handles_denial(oauth_env, migrated_db):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="https://test",
        follow_redirects=False,
    ) as client:
        denied = await client.get("/admin/oauth/x/callback?denied=unknown")
        assert denied.status_code == 303
        assert "status%3Derror" in denied.headers["location"]
        assert "oauth_state_missing" in denied.headers["location"]

        no_state = await client.get("/admin/oauth/x/callback?oauth_token=missing&oauth_verifier=v")
        assert no_state.status_code == 303
        assert "oauth_state_missing" in no_state.headers["location"]
        assert no_state.headers["cache-control"] == "no-store"

        missing_parameters = await client.get("/admin/oauth/x/callback")
        assert missing_parameters.status_code == 400
        assert missing_parameters.headers["cache-control"] == "no-store"

        slash_variant = await client.get(
            "/admin/oauth/x/callback/?oauth_token=opaque&oauth_verifier=opaque"
        )
        assert slash_variant.status_code == 400
        assert "location" not in slash_variant.headers
        assert slash_variant.headers["cache-control"] == "no-store"


async def test_callback_rejects_revoked_admin_session(oauth_env, migrated_db):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="https://test",
        follow_redirects=False,
    ) as client:
        csrf = await _login(client)
        start = await client.post(
            "/admin/oauth/x/start",
            data={"csrf_token": csrf, "tenant_id": "default", "brand_id": "b"},
        )
        assert start.status_code == 303
        logout_page = await client.get("/admin/logout")
        assert logout_page.status_code == 200
        assert (await client.get("/admin/accounts")).status_code == 200
        missing_csrf = await client.post("/admin/logout")
        assert missing_csrf.status_code == 403
        assert (await client.get("/admin/accounts")).status_code == 200
        logout = await client.post("/admin/logout", data={"csrf_token": csrf})
        assert logout.status_code == 303
        callback = await client.get(
            f"/admin/oauth/x/callback?oauth_token={_REQ_TOKEN}&oauth_verifier=v"
        )
    assert callback.status_code == 303
    assert "admin_session_invalid" in callback.headers["location"]
    assert "processed_job_id" not in oauth_env["submitted"]
    assert [request.url.path for request in oauth_env["calls"]] == ["/oauth/request_token"]


def test_x_error_detail_extracts_xml_message():
    request = httpx.Request("POST", "https://api.x.com/oauth/request_token")
    response = httpx.Response(
        403,
        request=request,
        text=(
            "<?xml version='1.0'?><errors><error code=\"415\">"
            "Callback URL not approved for this client application"
            "</error></errors>"
        ),
    )
    error = httpx.HTTPStatusError("forbidden", request=request, response=response)
    assert oauth_connect._x_error_detail(error) == (
        "Callback URL not approved for this client application"
    )


def test_settings_expose_postiz_style_x_app_credentials():
    settings = get_settings()
    assert settings.x_app_credentials == ("ck-app", "cs-app")
