import os

os.environ.setdefault("TESTING", "true")

import pytest

from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_engine, get_session_factory


@pytest.fixture
async def migrated_db():
    """每个测试用干净 schema：直接用 metadata 建表（迁移文件另行人工验证）"""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.drop_all)
        await conn.run_sync(models.Base.metadata.create_all)
    yield


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
