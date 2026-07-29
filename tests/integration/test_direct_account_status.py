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


@pytest.mark.parametrize(
    ("mismatch", "error"),
    [
        ("account", "raw_event_platform_account_mismatch"),
        ("tenant", "raw_event_tenant_mismatch"),
    ],
)
async def test_raw_event_ownership_is_validated_before_ingestion(session, mismatch, error):
    account_id = uuid.uuid4()
    other_account_id = uuid.uuid4()
    raw_event_id = uuid.uuid4()
    for current_id, tenant_id in (
        (account_id, "tenant-a"),
        (other_account_id, "tenant-b"),
    ):
        await session.execute(
            insert(models.PlatformAccount).values(
                id=current_id,
                tenant_id=tenant_id,
                brand_id="b1",
                platform="telegram",
                name=str(current_id),
                status="active",
                capability={"dm": True, "max_text_length": 4096},
            )
        )
    await session.execute(
        insert(models.RawEvent).values(
            id=raw_event_id,
            tenant_id="tenant-b" if mismatch == "tenant" else "tenant-a",
            platform_account_id=(other_account_id if mismatch == "account" else account_id),
            source="telegram_poll",
            ingress_kind="poll",
            event_namespace="telegram.dm",
            external_event_id=f"event-{mismatch}",
            payload={},
            context={},
            processing_status="PENDING",
        )
    )
    await session.commit()

    with pytest.raises(PermissionError, match=error):
        await ingest_canonical_event(
            CanonicalEvent(
                platform="telegram",
                platform_account_key=str(account_id),
                external_event_id=f"event-{mismatch}",
                external_user_id="user-1",
                conversation_key=f"telegram:account:user-{mismatch}",
                text="hello",
            ),
            raw_event_id=raw_event_id,
        )

    session.expire_all()
    raw_event = await session.get(models.RawEvent, raw_event_id)
    normalized = await session.scalar(select(models.NormalizedEvent.id).limit(1))
    assert raw_event.processing_status == "PENDING"
    assert normalized is None


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


async def test_stale_xchat_claim_cannot_mark_new_claim_inactive(session, monkeypatch):
    account_id = uuid.uuid4()
    raw_event_id = uuid.uuid4()
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            tenant_id="default",
            brand_id="b1",
            platform="x",
            name="active-x",
            external_account_id="bot-1",
            status="active",
            capability={"dm": True, "x_chat": True, "mentions": True},
        )
    )
    await session.execute(
        insert(models.RawEvent).values(
            id=raw_event_id,
            tenant_id="default",
            platform_account_id=account_id,
            source="x",
            payload={"data": {"event_type": "chat.received"}},
            context={},
            processing_status="XCHAT_PROCESSING",
            processing_claim_token=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        )
    )
    await session.commit()

    original_ensure_state = direct_module.ensure_state
    original_mark_inactive = direct_module._mark_raw_event_account_inactive

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

    async def establish_new_claim_before_inactive_mark(
        current_session,
        current_raw_event_id,
        *,
        claim_token,
    ):
        async with get_session_factory()() as other_session:
            await other_session.execute(
                update(models.RawEvent)
                .where(models.RawEvent.id == current_raw_event_id)
                .values(
                    processing_claim_token=uuid.UUID("22222222-2222-2222-2222-222222222222"),
                    processing_status="XCHAT_PROCESSING",
                )
            )
            await other_session.commit()
        await original_mark_inactive(
            current_session,
            current_raw_event_id,
            claim_token=claim_token,
        )

    monkeypatch.setattr(direct_module, "ensure_state", disable_after_state)
    monkeypatch.setattr(
        direct_module,
        "_mark_raw_event_account_inactive",
        establish_new_claim_before_inactive_mark,
    )
    result = await ingest_canonical_event(
        CanonicalEvent(
            platform="x",
            platform_account_key=str(account_id),
            external_event_id="event-x-race",
            external_user_id="user-1",
            conversation_key=f"x_chat:{account_id}:bot-1:user-1",
            text="hello",
            reply_target={"kind": "x_chat", "conversation_id": "bot-1:user-1"},
        ),
        raw_event_id=raw_event_id,
        raw_event_claim_token="11111111-1111-1111-1111-111111111111",
    )

    assert result is None
    session.expire_all()
    raw = await session.get(models.RawEvent, raw_event_id)
    normalized_count = await session.scalar(select(models.NormalizedEvent.id).limit(1))
    assert raw.processing_status == "XCHAT_PROCESSING"
    assert str(raw.processing_claim_token) == "22222222-2222-2222-2222-222222222222"
    assert normalized_count is None
