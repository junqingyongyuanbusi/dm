import uuid

import httpx
from sqlalchemy import insert

from apps.api.main import create_app
from social_reply.infrastructure.database import models


async def _login(client: httpx.AsyncClient) -> str:
    await client.get("/admin/login")
    csrf = client.cookies["reply_admin_csrf"]
    await client.post(
        "/admin/login",
        data={"csrf_token": csrf, "username": "admin", "password": "test-admin-password"},
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
