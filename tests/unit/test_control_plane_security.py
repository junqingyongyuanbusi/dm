import httpx
import pytest

from apps.api.main import create_app
from social_reply.application.account_management import jobs
from social_reply.shared.config import Settings


async def test_provisioning_rejects_tenant_path_traversal(tmp_path, monkeypatch):
    monkeypatch.setenv("ACCOUNT_SECRETS_ROOT", str(tmp_path))
    with pytest.raises(ValueError, match="invalid_tenant_id"):
        await jobs.submit_provisioning_job(
            tenant_id="../../escape",
            brand_id="brand",
            platform="telegram",
            actor="user:admin",
            request={"idempotency_key": "traversal-test"},
            secrets={"token": "secret"},
        )


def test_production_settings_require_https_public_base_url():
    with pytest.raises(ValueError, match="PUBLIC_BASE_URL"):
        Settings(
            testing=False,
            chatwoot_webhook_secret="real",
            chatwoot_api_token="real",
            control_api_key="control",
            admin_session_secret="x" * 32,
            admin_username="admin",
            admin_password="password",
            public_base_url="http://reply.example.com",
        )


async def test_control_api_denies_unallowed_tenant():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/platform-accounts/telegram",
            headers={"Authorization": "Bearer test-control-key"},
            json={"tenant_id": "forbidden", "token": "123:token"},
        )
    assert response.status_code == 403
    assert response.json()["detail"] == "tenant_access_denied"
