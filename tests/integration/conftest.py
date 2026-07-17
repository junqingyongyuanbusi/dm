import os

os.environ.setdefault("TESTING", "true")

import uuid

import pytest
from sqlalchemy import func, insert, select, text

from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_engine, get_session_factory


@pytest.fixture
async def migrated_db():
    """每个测试用干净 schema：直接用 metadata 建表（迁移文件另行人工验证）"""
    engine = get_engine()
    async with engine.begin() as conn:
        # pgvector 扩展（迁移里也有，这里覆盖 metadata 直建路径）
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(models.Base.metadata.drop_all)
        await conn.run_sync(models.Base.metadata.create_all)
    yield


def chatwoot_payload(**overrides) -> dict:
    payload = {
        "event": "message_created",
        "id": 55,
        "content": "请问怎么改邮箱",
        "message_type": "incoming",
        "private": False,
        "created_at": "2026-07-14T10:00:00Z",
        "sender": {"id": 9, "type": "contact"},
        "conversation": {"id": 77, "inbox_id": 101, "status": "pending"},
        "account": {"id": 1},
    }
    payload.update(overrides)
    return payload


async def seed_chatwoot_account(session, automation_default="BOT_ACTIVE") -> uuid.UUID:
    account_id = uuid.uuid4()
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            brand_id="b1",
            platform="telegram",
            name="a",
            chatwoot_inbox_id=101,
            automation_default=automation_default,
        )
    )
    await session.commit()
    return account_id


async def seed_raw_event(session, payload: dict) -> str:
    raw_id = (
        await session.execute(
            insert(models.RawEvent)
            .values(source="chatwoot", payload=payload)
            .returning(models.RawEvent.id)
        )
    ).scalar_one()
    await session.commit()
    return str(raw_id)


async def count_rows(session, model) -> int:
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


@pytest.fixture
async def session(migrated_db):
    async with get_session_factory()() as s:
        yield s
        await s.rollback()


@pytest.fixture(autouse=True)
def _flush_stub_broker():
    import dramatiq
    from dramatiq.brokers.stub import StubBroker

    broker = dramatiq.get_broker()
    if isinstance(broker, StubBroker):
        broker.flush_all()
    yield
