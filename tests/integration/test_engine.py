import pytest
from sqlalchemy import text

from social_reply.infrastructure.database.engine import get_engine

pytestmark = pytest.mark.integration


async def test_engine_connects():
    engine = get_engine()
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT 1"))
        assert result.scalar() == 1
    await engine.dispose()
