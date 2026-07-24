import uuid

import pytest
from sqlalchemy import insert, select, update

from social_reply.application.event_ingestion import direct as direct_module
from social_reply.application.event_ingestion.direct import ingest_canonical_event
from social_reply.domain.messages.canonical import CanonicalEvent
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory

pytestmark = pytest.mark.integration


async def test_disabled_account_is_rechecked_before_direct_ingestion(session):
    account_id = uuid.uuid4()
    raw_event_id = uuid.uuid4()
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            tenant_id="default",
            brand_id="b1",
            platform="telegram",
            name="disabled",
            status="DISABLED",
            capability={"dm": True, "max_text_length": 4096},
        )
    )
    await session.execute(
        insert(models.RawEvent).values(
            id=raw_event_id,
            source="telegram",
            payload={},
            processing_status="PENDING",
        )
    )
    await session.commit()

    result = await ingest_canonical_event(
        CanonicalEvent(
            platform="telegram",
            platform_account_key=str(account_id),
            external_event_id="event-1",
            external_user_id="user-1",
            conversation_key="telegram:account:user-1",
            text="hello",
        ),
        raw_event_id=raw_event_id,
    )

    assert result is None
    session.expire_all()
    raw_status = await session.scalar(
        select(models.RawEvent.processing_status).where(models.RawEvent.id == raw_event_id)
    )
    normalized_count = await session.scalar(select(models.NormalizedEvent.id).limit(1))
    assert raw_status == "IGNORED_ACCOUNT_INACTIVE"
    assert normalized_count is None


async def test_account_disable_is_rechecked_at_ingestion_commit(session, monkeypatch):
    account_id = uuid.uuid4()
    raw_event_id = uuid.uuid4()
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            tenant_id="default",
            brand_id="b1",
            platform="telegram",
            name="active",
            status="active",
            capability={"dm": True, "max_text_length": 4096},
        )
    )
    await session.execute(
        insert(models.RawEvent).values(
            id=raw_event_id,
            source="telegram",
            payload={},
            processing_status="PENDING",
        )
    )
    await session.commit()

    original_ensure_state = direct_module.ensure_state

    async def disable_after_state(current_session, conversation_id, default_state):
        result = await original_ensure_state(current_session, conversation_id, default_state)
        async with get_session_factory()() as other_session:
            await other_session.execute(
                update(models.PlatformAccount)
                .where(models.PlatformAccount.id == account_id)
                .values(status="DISABLED")
            )
            await other_session.commit()
        return result

    monkeypatch.setattr(direct_module, "ensure_state", disable_after_state)
    result = await ingest_canonical_event(
        CanonicalEvent(
            platform="telegram",
            platform_account_key=str(account_id),
            external_event_id="event-race",
            external_user_id="user-1",
            conversation_key="telegram:account:user-1",
            text="hello",
        ),
        raw_event_id=raw_event_id,
    )

    assert result is None
    session.expire_all()
    raw_status = await session.scalar(
        select(models.RawEvent.processing_status).where(models.RawEvent.id == raw_event_id)
    )
    normalized_count = await session.scalar(select(models.NormalizedEvent.id).limit(1))
    assert raw_status == "IGNORED_ACCOUNT_INACTIVE"
    assert normalized_count is None
