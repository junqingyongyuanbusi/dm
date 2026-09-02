import uuid

import httpx
from sqlalchemy import func, insert, select

from apps.api.main import create_app
from social_reply.infrastructure.database import models


async def _login(client: httpx.AsyncClient) -> str:
    await client.get("/auth/login")
    csrf = client.cookies["reply_admin_csrf"]
    await client.post(
        "/auth/login",
        data={
            "csrf_token": csrf,
            "username": "admin",
            "password": "test-admin-password",
        },
    )
    return csrf


async def test_admin_cannot_retry_ambiguous_outbox(session):
    account_id, contact_id, conversation_id, outbox_id = (uuid.uuid4() for _ in range(4))
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id, brand_id="b1", platform="telegram", name="bot"
        )
    )
    await session.execute(
        insert(models.Contact).values(
            id=contact_id,
            platform="telegram",
            platform_account_id=account_id,
            external_user_id="user",
        )
    )
    await session.execute(
        insert(models.Conversation).values(
            id=conversation_id,
            brand_id="b1",
            platform="telegram",
            platform_account_id=account_id,
            contact_id=contact_id,
            conversation_key=f"telegram:{account_id}:user",
        )
    )
    await session.execute(
        insert(models.OutboxMessage).values(
            id=outbox_id,
            conversation_id=conversation_id,
            platform_account_id=account_id,
            destination_type="telegram_dm",
            destination_id="user",
            message_type="text",
            payload={"text": "hello"},
            idempotency_key=str(outbox_id),
            status="NEEDS_REVIEW",
            last_error_code="AMBIGUOUS_SEND",
        )
    )
    await session.commit()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        csrf = await _login(client)
        response = await client.post(
            f"/admin/delivery/{outbox_id}/retry", data={"csrf_token": csrf}
        )
    assert response.status_code == 409


async def test_legacy_admin_failed_retry_uses_recovery_service_with_idempotent_audit(
    session,
    monkeypatch,
):
    account_id, contact_id, conversation_id, outbox_id = (uuid.uuid4() for _ in range(4))
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            brand_id="b1",
            platform="telegram",
            name="bot",
        )
    )
    await session.execute(
        insert(models.Contact).values(
            id=contact_id,
            platform="telegram",
            platform_account_id=account_id,
            external_user_id="failed-user",
        )
    )
    await session.execute(
        insert(models.Conversation).values(
            id=conversation_id,
            brand_id="b1",
            platform="telegram",
            platform_account_id=account_id,
            contact_id=contact_id,
            conversation_key=f"telegram:{account_id}:failed-user",
        )
    )
    await session.execute(
        insert(models.OutboxMessage).values(
            id=outbox_id,
            conversation_id=conversation_id,
            platform_account_id=account_id,
            destination_type="telegram_dm",
            destination_id="failed-user",
            message_type="text",
            payload={"text": "hello"},
            idempotency_key=str(outbox_id),
            status="FAILED",
            attempt_count=2,
            last_error_code="PROVIDER_REJECTED",
        )
    )
    await session.commit()

    from social_reply.application.message_delivery import recovery

    dispatched: list[str] = []

    async def dispatch(_actor, outbox_id_value: str) -> None:
        dispatched.append(outbox_id_value)

    monkeypatch.setattr(recovery, "dispatch_actor", dispatch)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        csrf = await _login(client)
        response = await client.post(
            f"/admin/delivery/{outbox_id}/retry",
            data={"csrf_token": csrf},
        )
        replay = await client.post(
            f"/admin/delivery/{outbox_id}/retry",
            data={"csrf_token": csrf},
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/inbox?queue=delivery"
    assert replay.status_code == 303
    assert dispatched == [str(outbox_id)]
    session.expire_all()
    outbox = await session.get(models.OutboxMessage, outbox_id)
    assert outbox is not None
    assert outbox.status == "PENDING"
    audit_count = await session.scalar(
        select(func.count())
        .select_from(models.AuditLog)
        .where(
            models.AuditLog.action == "RETRY_CONFIRMED_FAILURE",
            models.AuditLog.subject_id == str(outbox_id),
        )
    )
    assert audit_count == 1
