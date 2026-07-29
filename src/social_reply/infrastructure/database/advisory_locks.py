import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from social_reply.infrastructure.database.engine import get_engine


def _conversation_delivery_key(conversation_id: uuid.UUID) -> str:
    return f"social-reply:conversation-delivery:{conversation_id}"


async def acquire_conversation_delivery_xact_lock(
    session: AsyncSession,
    conversation_id: uuid.UUID,
) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": _conversation_delivery_key(conversation_id)},
    )


@asynccontextmanager
async def hold_conversation_delivery_lock(
    conversation_id: uuid.UUID,
) -> AsyncIterator[AsyncConnection]:
    key = _conversation_delivery_key(conversation_id)
    async with get_engine().connect() as connection:
        await connection.execute(
            text("SELECT pg_advisory_lock(hashtextextended(:key, 0))"),
            {"key": key},
        )
        await connection.commit()
        try:
            yield connection
        finally:
            try:
                if connection.in_transaction():
                    await connection.rollback()
                unlocked = await connection.scalar(
                    text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))"),
                    {"key": key},
                )
                await connection.commit()
                if unlocked is not True:
                    await connection.invalidate()
            except BaseException:
                await connection.invalidate()
                raise
