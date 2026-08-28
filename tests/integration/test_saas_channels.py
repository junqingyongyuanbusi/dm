import uuid

import httpx
import pytest

from apps.api.main import create_app
from social_reply.application.account_management.auth import hash_password
from social_reply.application.account_management.meta_credentials import (
    MetaAppCredentials,
)
from social_reply.infrastructure.database import models
from social_reply.infrastructure.secret_crypto import encrypt_secret_bundle

pytestmark = pytest.mark.integration

_USER_PASSWORD = "channel-user-password-123"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="https://test",
        follow_redirects=False,
    )


async def _seed_users(session) -> tuple[models.AdminUser, models.AdminUser]:
    first_user = models.AdminUser(
        username="channel-user-a",
        password_hash=await hash_password(_USER_PASSWORD),
        tenant_id="tenant-a",
        role="USER",
        must_change_password=False,
        status="active",
    )
    second_user = models.AdminUser(
        username="channel-user-b",
        password_hash=await hash_password(_USER_PASSWORD),
        tenant_id="tenant-a",
        role="USER",
        must_change_password=False,
        status="active",
    )
    session.add_all([first_user, second_user])
    await session.commit()
    return first_user, second_user


async def _login(client: httpx.AsyncClient, username: str) -> str:
    await client.get("/auth/login")
    csrf = client.cookies["reply_admin_csrf"]
    response = await client.post(
        "/auth/login",
        data={
            "csrf_token": csrf,
            "username": username,
            "password": _USER_PASSWORD,
        },
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/app"
    return csrf


async def test_channels_page_profile_and_job_endpoint_are_owner_scoped(
    session,
    migrated_db,
) -> None:
    first_user, second_user = await _seed_users(session)
    own_account = models.PlatformAccount(
        tenant_id="tenant-a",
        brand_id="default",
        platform="x",
        owner_user_id=first_user.id,
        name="Owned X Account",
        provider_username="owned_handle",
        avatar_url="https://pbs.twimg.com/profile_images/owned.jpg",
        external_account_id="x-owned",
        public_id="x_owned",
        config={},
        capability={},
        automation_default="BOT_DRAFT_ONLY",
        status="active",
    )
    sibling_account = models.PlatformAccount(
        tenant_id="tenant-a",
        brand_id="default",
        platform="telegram",
        owner_user_id=second_user.id,
        name="Sibling Secret Account",
        provider_username="sibling_bot",
        external_account_id="telegram-sibling",
        public_id="tg_sibling",
        config={},
        capability={},
        automation_default="BOT_DRAFT_ONLY",
        status="active",
    )
    session.add_all([own_account, sibling_account])
    await session.flush()
    own_job = models.ProvisioningJob(
        tenant_id="tenant-a",
        brand_id="default",
        platform="x",
        actor="user:channel-user-a",
        owner_user_id=first_user.id,
        idempotency_key="owned-channel-job",
        request={"name": "Owned X Account"},
        staging_secret=encrypt_secret_bundle({"access_token": "owned-secret-token"}),
        status="NEEDS_ACTION",
        current_step="NEEDS_ACTION",
        result={"nested": {"access_token": "result-secret-token"}},
        last_error_code="ACCOUNT_OWNER_CONFLICT",
        last_error_message="raw provider details should never render",
    )
    sibling_job = models.ProvisioningJob(
        tenant_id="tenant-a",
        brand_id="default",
        platform="telegram",
        actor="user:channel-user-b",
        owner_user_id=second_user.id,
        idempotency_key="sibling-channel-job",
        request={"name": "Sibling Secret Account"},
        status="PENDING",
        current_step="QUEUED",
        result={},
    )
    session.add_all([own_job, sibling_job])
    await session.commit()

    async with _client() as client:
        await _login(client, first_user.username)
        channels_page = await client.get("/app/t/tenant-a/channels")
        profile_page = await client.get("/app/t/tenant-a/profile")
        own_job_response = await client.get(
            f"/app/t/tenant-a/channels/jobs/{own_job.id}"
        )
        sibling_job_response = await client.get(
            f"/app/t/tenant-a/channels/jobs/{sibling_job.id}"
        )

    assert channels_page.status_code == 200
    assert channels_page.headers["cache-control"] == "no-store"
    assert "Owned X Account" in channels_page.text
    assert "@owned_handle" in channels_page.text
    assert "https://pbs.twimg.com/profile_images/owned.jpg" in channels_page.text
    assert 'referrerpolicy="no-referrer"' in channels_page.text
    assert "Sibling Secret Account" not in channels_page.text
    assert "sibling_bot" not in channels_page.text
    assert "owned-secret-token" not in channels_page.text
    assert "raw provider details" not in channels_page.text
    assert 'href="/admin' not in channels_page.text
    assert 'action="/admin' not in channels_page.text

    assert profile_page.status_code == 200
    assert "/app/t/tenant-a/channels" in profile_page.text
    assert "/auth/change-password" in profile_page.text
    assert "Bot Token" not in profile_page.text
    assert "App Password" not in profile_page.text
    assert "/channels/accounts/" not in profile_page.text

    assert own_job_response.status_code == 200
    assert own_job_response.headers["cache-control"] == "no-store"
    assert own_job_response.json()["last_error_code"] == "ACCOUNT_OWNER_CONFLICT"
    assert "其他归属范围" in own_job_response.json()["last_error_message"]
    assert "secret-token" not in own_job_response.text
    assert sibling_job_response.status_code == 404


async def test_user_can_start_all_channels_oauth_flows_with_bound_context(
    session,
    migrated_db,
    monkeypatch,
) -> None:
    from social_reply.application.account_management.oauth import instagram, meta, x

    first_user, _second_user = await _seed_users(session)
    stored_contexts: dict[str, dict] = {}

    async def fake_x_request_token(**_kwargs):
        return {
            "oauth_token": "request-token",
            "oauth_token_secret": "request-secret",
        }

    async def store_x_state(_namespace, _key, payload):
        stored_contexts["x"] = dict(payload)

    async def store_meta_state(_namespace, _key, payload):
        stored_contexts["facebook"] = dict(payload)

    async def store_instagram_state(_namespace, _key, payload):
        stored_contexts["instagram"] = dict(payload)

    async def facebook_credentials(_tenant_id):
        return MetaAppCredentials(
            app_id="facebook-app",
            app_secret="facebook-secret",
            verify_token="facebook-verify",
            public_id="meta_public",
            platform_family="meta",
        )

    async def instagram_credentials(_tenant_id):
        return MetaAppCredentials(
            app_id="instagram-app",
            app_secret="instagram-secret",
            verify_token="instagram-verify",
            public_id="instagram_public",
            platform_family="instagram",
        )

    monkeypatch.setattr(x, "x_app_credentials", lambda: ("x-key", "x-secret"))
    monkeypatch.setattr(x, "_request_token", fake_x_request_token)
    monkeypatch.setattr(x, "store_oauth_state", store_x_state)
    monkeypatch.setattr(meta, "facebook_app_credentials", facebook_credentials)
    monkeypatch.setattr(meta, "store_oauth_state", store_meta_state)
    monkeypatch.setattr(instagram, "instagram_app_credentials", instagram_credentials)
    monkeypatch.setattr(instagram, "store_oauth_state", store_instagram_state)

    async with _client() as client:
        csrf = await _login(client, first_user.username)
        x_response = await client.post(
            "/app/t/tenant-a/channels/oauth/x/start",
            data={
                "csrf_token": csrf,
                "tenant_id": "tenant-b",
                "brand_id": "default",
            },
        )
        facebook_response = await client.post(
            "/app/t/tenant-a/channels/oauth/meta/start",
            data={
                "csrf_token": csrf,
                "tenant_id": "tenant-b",
                "brand_id": "default",
                "platform": "facebook",
            },
        )
        instagram_response = await client.post(
            "/app/t/tenant-a/channels/oauth/instagram/start",
            data={
                "csrf_token": csrf,
                "tenant_id": "tenant-b",
                "brand_id": "default",
            },
        )

    assert x_response.status_code == 303
    assert x_response.headers["location"].startswith(
        "https://api.x.com/oauth/authorize?"
    )
    assert facebook_response.status_code == 303
    assert facebook_response.headers["location"].startswith(
        "https://www.facebook.com/"
    )
    assert instagram_response.status_code == 303
    assert instagram_response.headers["location"].startswith(
        "https://www.instagram.com/oauth/authorize?"
    )
    for provider, context in stored_contexts.items():
        assert context["provider"] == provider
        assert context["tenant_id"] == "tenant-a"
        assert context["initiator_user_id"] == str(first_user.id)
        assert context["initiator_session_id"]
        assert context["surface"] == "channels"
        assert context["return_to"] == "/app/t/tenant-a/channels"


async def test_channels_oauth_cancellation_returns_without_admin_links(
    session,
    migrated_db,
    monkeypatch,
) -> None:
    from social_reply.application.account_management.oauth import instagram, meta, x

    first_user, _second_user = await _seed_users(session)

    async def x_context(_namespace, _key):
        return {
            "surface": "channels",
            "tenant_id": "tenant-a",
            "return_to": "/app/t/tenant-a/channels",
        }

    async def meta_context(_namespace, _key):
        return {
            "surface": "channels",
            "tenant_id": "tenant-a",
            "platform": "facebook",
            "return_to": "/app/t/tenant-a/channels",
        }

    async def instagram_context(_namespace, _key):
        return {
            "surface": "channels",
            "tenant_id": "tenant-a",
            "return_to": "/app/t/tenant-a/channels",
        }

    monkeypatch.setattr(x, "take_oauth_state", x_context)
    monkeypatch.setattr(meta, "take_oauth_state", meta_context)
    monkeypatch.setattr(instagram, "take_oauth_state", instagram_context)

    async with _client() as client:
        await _login(client, first_user.username)
        x_response = await client.get(
            "/admin/oauth/x/callback?denied=request-token"
        )
        facebook_response = await client.get(
            "/admin/oauth/meta/callback?error=access_denied&state=meta-state"
        )
        instagram_response = await client.get(
            "/admin/oauth/instagram/callback?error=access_denied&state=instagram-state"
        )

    for response in (x_response, facebook_response, instagram_response):
        assert response.status_code == 303
        assert response.headers["location"].startswith(
            "/app/t/tenant-a/channels?"
        )
        assert "/admin" not in response.headers["location"]
        assert response.headers["cache-control"] == "no-store"
