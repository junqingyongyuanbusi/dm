import pytest
from sqlalchemy import select
from tests.integration.conftest import (
    chatwoot_payload,
    seed_chatwoot_account,
    seed_raw_event,
)

import social_reply.infrastructure.queue.broker  # noqa: F401  确保测试用 StubBroker
from social_reply.application.event_ingestion.processor import process_raw_event
from social_reply.infrastructure.database import models

pytestmark = pytest.mark.integration


async def test_bot_active_inbound_fast_path_delivers_immediately(session):
    await seed_chatwoot_account(session, "BOT_ACTIVE")
    await process_raw_event(await seed_raw_event(session, chatwoot_payload()))
    ob = (await session.execute(select(models.OutboxMessage))).scalar_one()
    assert ob.status == "SENT"
    assert ob.chatwoot_message_id is not None


async def test_handoff_no_outbox(session):
    await seed_chatwoot_account(session, "BOT_ACTIVE")
    await process_raw_event(
        await seed_raw_event(session, chatwoot_payload(content="我要起诉，无法出金"))
    )
    ob = (await session.execute(select(models.OutboxMessage))).first()
    assert ob is None
