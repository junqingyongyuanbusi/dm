import uuid

import httpx
import pytest
from sqlalchemy import insert, select

from social_reply.application.event_ingestion import reconcile
from social_reply.infrastructure.database import models

pytestmark = pytest.mark.integration


async def _seed_mapping(session):
    account_id, contact_id, conversation_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            brand_id="b1",
            platform="telegram",
            name="a",
            chatwoot_inbox_id=101,
        )
    )
    await session.execute(
        insert(models.Contact).values(
            id=contact_id,
            platform="telegram",
            platform_account_id=account_id,
            external_user_id="9",
        )
    )
    await session.execute(
        insert(models.Conversation).values(
            id=conversation_id,
            brand_id="b1",
            platform="telegram",
            platform_account_id=account_id,
            contact_id=contact_id,
            conversation_key="telegram:x:9",
        )
    )
    await session.execute(
        insert(models.ConversationMapping).values(
            chatwoot_account_id=1,
            chatwoot_conversation_id=77,
            conversation_id=conversation_id,
        )
    )
    await session.commit()


async def test_reconcile_creates_raw_for_missing_incoming(session, monkeypatch):
    await _seed_mapping(session)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "payload": [
                    {
                        "id": 55,
                        "content": "latest",
                        "message_type": 0,
                        "private": False,
                        "conversation_id": 77,
                        "inbox_id": 101,
                        "account_id": 1,
                        "sender": {"id": 9, "type": "contact"},
                    }
                ]
            },
        )

    real_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(reconcile.httpx, "AsyncClient", client_factory)
    created = await reconcile.reconcile_chatwoot_messages()

    assert len(created) == 1
    raw = (await session.execute(select(models.RawEvent))).scalar_one()
    assert raw.source == "chatwoot_reconcile"
    assert raw.payload["id"] == 55
    assert raw.payload["content"] == "latest"
    assert raw.processing_status == "PENDING"


async def test_reconcile_skips_already_normalized_message(session, monkeypatch):
    await _seed_mapping(session)
    account_id = (await session.execute(select(models.PlatformAccount.id))).scalar_one()
    await session.execute(
        insert(models.NormalizedEvent).values(
            tenant_id="default",
            platform="telegram",
            platform_account_id=account_id,
            external_event_id="55",
            event_type="dm.message.created",
        )
    )
    await session.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "payload": [
                    {
                        "id": 55,
                        "content": "latest",
                        "message_type": 0,
                        "private": False,
                        "conversation_id": 77,
                        "inbox_id": 101,
                        "account_id": 1,
                        "sender": {"id": 9, "type": "contact"},
                    }
                ]
            },
        )

    real_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(reconcile.httpx, "AsyncClient", client_factory)
    assert await reconcile.reconcile_chatwoot_messages() == []
    assert (await session.execute(select(models.RawEvent))).first() is None
