import asyncio
import uuid
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import insert, select

from apps.api.main import create_app
from social_reply.application.account_management import feishu_handoff_admin as admin_module
from social_reply.connectors.feishu.client import FeishuClient
from social_reply.infrastructure.database import models

pytestmark = pytest.mark.integration


async def _login(client: httpx.AsyncClient) -> str:
    page = await client.get("/admin/login")
    csrf = client.cookies["reply_admin_csrf"]
    response = await client.post(
        "/admin/login",
        data={
            "csrf_token": csrf,
            "username": "admin",
            "password": "test-admin-password",
        },
    )
    assert page.status_code == 200
    assert response.status_code == 303
    return csrf


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://test",
        follow_redirects=False,
    )


async def _seed_feishu_account(session, *, tenant_id="default") -> uuid.UUID:
    account_id = uuid.uuid4()
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            tenant_id=tenant_id,
            brand_id="b1",
            platform="feishu",
            name=f"Support {tenant_id}",
            public_id=f"fs-{account_id}",
            external_account_id="cli_12345678",
            config={"feishu_health_status": "READY"},
            capability={"dm": True, "mentions": True, "max_text_length": 4000},
            status="active",
        )
    )
    await session.commit()
    return account_id


async def test_admin_configures_handoff_route_and_operator(session):
    account_id = await _seed_feishu_account(session)
    async with _client() as client:
        csrf = await _login(client)
        page = await client.get("/admin/feishu-handoff")
        configured = await client.post(
            "/admin/feishu-handoff/config",
            data={
                "csrf_token": csrf,
                "tenant_id": "default",
                "feishu_platform_account_id": str(account_id),
                "destination_chat_id": "oc_support",
                "enabled": "true",
            },
        )
        operator = await client.post(
            "/admin/feishu-handoff/operators",
            data={
                "csrf_token": csrf,
                "tenant_id": "default",
                "operator_open_id": "ou_agent",
                "display_name": "Agent One",
                "can_claim": "true",
                "can_resolve": "true",
            },
        )

    assert page.status_code == 200
    assert "飞书人工通知" in page.text
    assert configured.status_code == operator.status_code == 303
    config = (await session.execute(select(models.TenantFeishuHandoffConfig))).scalar_one()
    saved_operator = (await session.execute(select(models.FeishuHandoffOperator))).scalar_one()
    assert config.feishu_platform_account_id == account_id
    assert config.destination_chat_id == "oc_support"
    assert config.enabled is True
    assert saved_operator.operator_open_id == "ou_agent"
    assert saved_operator.status == "ACTIVE"
    actions = set((await session.execute(select(models.AuditLog.action))).scalars())
    assert {"SET_FEISHU_HANDOFF_CONFIG", "SET_FEISHU_HANDOFF_OPERATOR"} <= actions


async def test_concurrent_first_config_writes_are_serialized(session):
    account_id = await _seed_feishu_account(session)
    first = _client()
    second = _client()
    try:
        first_csrf, second_csrf = await asyncio.gather(_login(first), _login(second))

        async def save(client, csrf, chat_id):
            return await client.post(
                "/admin/feishu-handoff/config",
                data={
                    "csrf_token": csrf,
                    "tenant_id": "default",
                    "feishu_platform_account_id": str(account_id),
                    "destination_chat_id": chat_id,
                    "enabled": "true",
                },
            )

        first_response, second_response = await asyncio.gather(
            save(first, first_csrf, "oc_first"),
            save(second, second_csrf, "oc_second"),
        )
    finally:
        await first.aclose()
        await second.aclose()

    assert first_response.status_code == second_response.status_code == 303
    config = (await session.execute(select(models.TenantFeishuHandoffConfig))).scalar_one()
    assert config.destination_chat_id in {"oc_first", "oc_second"}
    assert config.config_version == 2


async def test_admin_config_rejects_account_from_another_tenant(session):
    foreign_account_id = await _seed_feishu_account(session, tenant_id="tenant-a")
    async with _client() as client:
        csrf = await _login(client)
        response = await client.post(
            "/admin/feishu-handoff/config",
            data={
                "csrf_token": csrf,
                "tenant_id": "default",
                "feishu_platform_account_id": str(foreign_account_id),
                "destination_chat_id": "oc_support",
                "enabled": "true",
            },
        )

    assert response.status_code == 404
    assert (await session.execute(select(models.TenantFeishuHandoffConfig))).first() is None


async def test_admin_sends_explicit_configuration_test_card(session, monkeypatch):
    account_id = await _seed_feishu_account(session)
    await session.execute(
        insert(models.TenantFeishuHandoffConfig).values(
            tenant_id="default",
            feishu_platform_account_id=account_id,
            destination_chat_id="oc_support",
            enabled=True,
            config_version=1,
        )
    )
    await session.commit()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "tenant", "expire": 7200},
            )
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_test"}})

    sender = FeishuClient(
        app_id="cli_12345678",
        app_secret="app-secret",
        transport=httpx.MockTransport(handler),
    )

    async def get_sender(_account_id):
        return sender

    monkeypatch.setattr(admin_module, "get_platform_sender", get_sender)
    monkeypatch.setattr(
        admin_module,
        "get_settings",
        lambda: SimpleNamespace(feishu_enabled=True),
    )
    async with _client() as client:
        csrf = await _login(client)
        response = await client.post(
            "/admin/feishu-handoff/test",
            data={"csrf_token": csrf, "tenant_id": "default"},
        )
    await sender.aclose()

    assert response.status_code == 303
    assert response.headers["location"].endswith("notice=test_sent")
    audit = (
        await session.execute(
            select(models.AuditLog).where(models.AuditLog.action == "SEND_FEISHU_HANDOFF_TEST_CARD")
        )
    ).scalar_one()
    assert audit.detail["outcome"] == "test_sent"
    assert audit.detail["provider_message_id"] == "om_test"
    assert any(request.url.params.get("receive_id_type") == "chat_id" for request in requests)
