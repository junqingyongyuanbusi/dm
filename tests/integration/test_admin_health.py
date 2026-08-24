import re
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import insert

from apps.api.main import create_app
from social_reply.application.account_management import admin_console
from social_reply.infrastructure.database import models

pytestmark = pytest.mark.integration


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://test",
        follow_redirects=False,
    )


async def _login(client: httpx.AsyncClient) -> None:
    await client.get("/admin/login")
    csrf = client.cookies["reply_admin_csrf"]
    response = await client.post(
        "/admin/login",
        data={
            "csrf_token": csrf,
            "username": "admin",
            "password": "test-admin-password",
        },
    )
    assert response.status_code == 303


def _health_row(page: str, key: str) -> str:
    match = re.search(rf'<tr data-health="{key}">(.*?)</tr>', page, re.DOTALL)
    assert match is not None, key
    return match.group(1)


async def test_health_center_renders_healthy_empty_state(migrated_db):
    async with _client() as client:
        await _login(client)
        response = await client.get("/admin")

    assert response.status_code == 200
    assert "运行健康" in response.text
    for key in (
        "ingestion",
        "decisions",
        "delivery",
        "provisioning",
        "sync",
        "accounts",
    ):
        row = _health_row(response.text, key)
        assert "HEALTHY" in row
        assert "0 需处理 · 0 恢复中" in row
        assert "—" in row


async def test_health_aggregates_exclude_other_tenants_and_mismatched_checkpoints(
    session,
):
    account_id, disabled_account_id = uuid.uuid4(), uuid.uuid4()
    contact_id, conversation_id, message_id, outbox_id = (
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
    )
    checkpoint_b, checkpoint_mismatch = uuid.uuid4(), uuid.uuid4()
    run_b, run_mismatch = uuid.uuid4(), uuid.uuid4()
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            tenant_id="tenant-b",
            brand_id="default",
            platform="x",
            name="tenant-b-x",
            status="active",
            capability={"dm": True, "x_chat": True, "mentions": True},
        )
    )
    await session.execute(
        insert(models.PlatformAccount).values(
            id=disabled_account_id,
            tenant_id="tenant-b",
            brand_id="default",
            platform="telegram",
            name="tenant-b-disabled",
            status="DISABLED",
            capability={"dm": True, "max_text_length": 4096},
        )
    )
    await session.execute(
        insert(models.Contact).values(
            id=contact_id,
            tenant_id="tenant-b",
            platform="x",
            platform_account_id=account_id,
            external_user_id="user-b",
        )
    )
    await session.execute(
        insert(models.Conversation).values(
            id=conversation_id,
            tenant_id="tenant-b",
            brand_id="default",
            platform="x",
            platform_account_id=account_id,
            contact_id=contact_id,
            conversation_key="x:tenant-b:user-b",
        )
    )
    await session.execute(
        insert(models.Message).values(
            id=message_id,
            conversation_id=conversation_id,
            direction="inbound",
            sender_type="contact",
            text="hello",
        )
    )
    await session.execute(
        insert(models.RawEvent).values(
            tenant_id="tenant-b",
            source="x",
            payload={},
            processing_status="INITIAL_DISPATCH_DEAD",
        )
    )
    await session.execute(
        insert(models.DecisionJob).values(
            conversation_id=conversation_id,
            message_id=message_id,
            account_id=account_id,
            snapshot={},
            status="NEEDS_REVIEW",
        )
    )
    await session.execute(
        insert(models.OutboxMessage).values(
            id=outbox_id,
            tenant_id="tenant-b",
            conversation_id=conversation_id,
            platform_account_id=account_id,
            destination_type="x_dm",
            destination_id="x:tenant-b:user-b",
            message_type="text",
            payload={"text": "reply"},
            idempotency_key=str(outbox_id),
            status="NEEDS_REVIEW",
        )
    )
    await session.execute(
        insert(models.ProvisioningJob).values(
            tenant_id="tenant-b",
            brand_id="default",
            platform="x",
            actor="user:admin",
            idempotency_key="tenant-b-health",
            request={},
            status="NEEDS_ACTION",
        )
    )
    for checkpoint_id, tenant_id, stream, run_id in (
        (checkpoint_b, "tenant-b", "X_LEGACY_DM", run_b),
        (checkpoint_mismatch, "tenant-a", "XCHAT_DISCOVERY", run_mismatch),
    ):
        await session.execute(
            insert(models.PlatformCheckpoint).values(
                id=checkpoint_id,
                tenant_id=tenant_id,
                platform_account_id=account_id,
                stream=stream,
                scope_key="",
            )
        )
        await session.execute(
            insert(models.SyncRun).values(
                id=run_id,
                checkpoint_id=checkpoint_id,
                claim_token=uuid.uuid4(),
                mode="BACKFILL",
                status="GAPPED",
            )
        )
        await session.execute(
            insert(models.SyncGap).values(
                checkpoint_id=checkpoint_id,
                sync_run_id=run_id,
                gap_type="DECRYPT_ERROR",
                status="OPEN",
            )
        )
    await session.commit()

    metrics = await admin_console._load_health_metrics(
        session,
        frozenset({"tenant-a"}),
        datetime.now(UTC),
    )
    assert {metric.key: (metric.action_count, metric.warning_count) for metric in metrics} == {
        "ingestion": (0, 0),
        "decisions": (0, 0),
        "delivery": (0, 0),
        "provisioning": (0, 0),
        "sync": (0, 0),
        "accounts": (0, 0),
    }


async def test_health_center_surfaces_actionable_postgres_state(session):
    account_id, disabled_account_id = uuid.uuid4(), uuid.uuid4()
    contact_id, conversation_id, message_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    outbox_id, checkpoint_id, run_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    old = datetime.now(UTC) - timedelta(hours=3)
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            tenant_id="default",
            brand_id="default",
            platform="x",
            name="active-x",
            status="active",
            capability={"dm": True, "x_chat": True, "mentions": True},
        )
    )
    await session.execute(
        insert(models.PlatformAccount).values(
            id=disabled_account_id,
            tenant_id="default",
            brand_id="default",
            platform="telegram",
            name="disabled-account",
            status="DISABLED",
            capability={"dm": True, "max_text_length": 4096},
            created_at=old,
        )
    )
    await session.execute(
        insert(models.Contact).values(
            id=contact_id,
            tenant_id="default",
            platform="x",
            platform_account_id=account_id,
            external_user_id="user-1",
        )
    )
    await session.execute(
        insert(models.Conversation).values(
            id=conversation_id,
            tenant_id="default",
            brand_id="default",
            platform="x",
            platform_account_id=account_id,
            contact_id=contact_id,
            conversation_key="x:account:user-1",
        )
    )
    await session.execute(
        insert(models.Message).values(
            id=message_id,
            conversation_id=conversation_id,
            direction="inbound",
            sender_type="contact",
            text="hello",
        )
    )
    await session.execute(
        insert(models.RawEvent).values(
            tenant_id="default",
            source="telegram",
            payload={},
            processing_status="INITIAL_DISPATCH_DEAD",
            received_at=old,
        )
    )
    await session.execute(
        insert(models.DecisionJob).values(
            conversation_id=conversation_id,
            message_id=message_id,
            account_id=account_id,
            snapshot={},
            status="NEEDS_REVIEW",
            created_at=old,
        )
    )
    await session.execute(
        insert(models.OutboxMessage).values(
            id=outbox_id,
            tenant_id="default",
            conversation_id=conversation_id,
            platform_account_id=account_id,
            destination_type="x_dm",
            destination_id="x:account:user-1",
            message_type="text",
            payload={"text": "reply", "target": {"kind": "dm", "participant_id": "user-1"}},
            idempotency_key=str(outbox_id),
            status="NEEDS_REVIEW",
            created_at=old,
        )
    )
    await session.execute(
        insert(models.ProvisioningJob).values(
            tenant_id="default",
            brand_id="default",
            platform="x",
            actor="user:admin",
            idempotency_key="health-provisioning",
            request={},
            status="NEEDS_ACTION",
            created_at=old,
        )
    )
    await session.execute(
        insert(models.PlatformCheckpoint).values(
            id=checkpoint_id,
            tenant_id="default",
            platform_account_id=account_id,
            stream="X_LEGACY_DM",
            scope_key="",
        )
    )
    await session.execute(
        insert(models.SyncRun).values(
            id=run_id,
            checkpoint_id=checkpoint_id,
            claim_token=uuid.uuid4(),
            mode="BACKFILL",
            status="GAPPED",
        )
    )
    await session.execute(
        insert(models.SyncGap).values(
            checkpoint_id=checkpoint_id,
            sync_run_id=run_id,
            gap_type="DECRYPT_ERROR",
            status="OPEN",
            created_at=old,
        )
    )
    await session.commit()

    async with _client() as client:
        await _login(client)
        response = await client.get("/admin")
        health = await client.get("/admin/health")

    for key in (
        "ingestion",
        "decisions",
        "delivery",
        "provisioning",
        "sync",
        "accounts",
    ):
        row = _health_row(response.text, key)
        assert "ACTION" in row
        assert "1 需处理" in row
        assert "3 小时" in row
    assert "/admin/health#ingress" in _health_row(response.text, "ingestion")
    assert "/admin/health#decisions" in _health_row(response.text, "decisions")
    assert "/admin/inbox?queue=delivery" in _health_row(response.text, "delivery")
    assert "/admin/accounts" in _health_row(response.text, "sync")
    assert "INITIAL_DISPATCH_DEAD" in health.text


async def test_health_center_flags_permanent_xchat_public_key_failure(session):
    await session.execute(
        insert(models.RawEvent).values(
            tenant_id="default",
            source="x",
            payload={},
            processing_status="XCHAT_PUBLIC_KEY_HTTP_400",
        )
    )
    await session.commit()

    async with _client() as client:
        await _login(client)
        response = await client.get("/admin")

    row = _health_row(response.text, "ingestion")
    assert "ACTION" in row
    assert "1 需处理 · 0 恢复中" in row
