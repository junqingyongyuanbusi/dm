import uuid

import httpx

from apps.api.main import create_app
from social_reply.application.account_management import router as account_router


async def _client():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://test",
    )


async def test_account_api_requires_control_api_key():
    async with await _client() as client:
        response = await client.post(
            "/api/v1/platform-accounts/telegram",
            json={"token": "123:token"},
        )
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid_control_api_key"


async def test_telegram_account_api_submits_durable_job(monkeypatch):
    captured = {}

    async def fake_submit(**kwargs):
        captured.update(kwargs)
        return uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

    async def fake_enqueue(_job_id):
        return None

    monkeypatch.setattr(account_router, "submit_provisioning_job", fake_submit)
    monkeypatch.setattr(account_router, "_enqueue", fake_enqueue)
    async with await _client() as client:
        response = await client.post(
            "/api/v1/platform-accounts/telegram",
            headers={"Authorization": "Bearer test-control-key"},
            json={"token": "123:token", "brand_id": "brand-a"},
        )
    assert response.status_code == 202
    assert response.json() == {
        "job_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "status": "PENDING",
        "status_url": "/api/v1/platform-accounts/jobs/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    }
    assert captured["platform"] == "telegram"
    assert captured["brand_id"] == "brand-a"
    assert captured["secrets"] == {"token": "123:token"}
    assert "token" not in captured["request"]


async def test_meta_account_api_rejects_missing_app_identity():
    async with await _client() as client:
        response = await client.post(
            "/api/v1/platform-accounts/meta",
            headers={"X-Control-Api-Key": "test-control-key"},
            json={
                "platform": "instagram",
                "external_account_id": "ig-1",
                "access_token": "token",
                "app_secret": "secret",
            },
        )
    assert response.status_code == 422


async def test_whatsapp_account_api_submits_phone_number_job(monkeypatch):
    captured = {}

    async def fake_submit(**kwargs):
        captured.update(kwargs)
        return uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

    async def fake_enqueue(_job_id):
        return None

    monkeypatch.setattr(account_router, "submit_provisioning_job", fake_submit)
    monkeypatch.setattr(account_router, "_enqueue", fake_enqueue)
    async with await _client() as client:
        response = await client.post(
            "/api/v1/platform-accounts/whatsapp",
            headers={"Authorization": "Bearer test-control-key"},
            json={
                "external_account_id": "phone-1",
                "access_token": "token",
                "app_secret": "secret",
                "app_id": "app-1",
                "verify_token": "verify",
            },
        )
    assert response.status_code == 202
    assert captured["platform"] == "whatsapp"
    assert captured["request"]["external_account_id"] == "phone-1"
    assert captured["secrets"] == {
        "access_token": "token",
        "app_secret": "secret",
        "verify_token": "verify",
    }
