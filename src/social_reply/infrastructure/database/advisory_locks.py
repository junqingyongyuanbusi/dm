import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from social_reply.infrastructure.database.engine import get_engine


def _conversation_delivery_key(conversation_id: uuid.UUID) -> str:
    return f"social-reply:conversation-delivery:{conversation_id}"


async def acquire_xact_lock(session: AsyncSession, key: str) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": key},
    )


async def acquire_conversation_delivery_xact_lock(
    session: AsyncSession,
    conversation_id: uuid.UUID,
) -> None:
    await acquire_xact_lock(session, _conversation_delivery_key(conversation_id))


@asynccontextmanager
async def hold_connection_advisory_lock(
    connection: AsyncConnection,
    key: str,
) -> AsyncIterator[None]:
    """Hold a session advisory lock without owning caller business transactions.

    The connection must be idle on entry. Lock/unlock statements use their own short
    transactions; work inside the context remains responsible for committing its own state.
    If that work leaves a transaction open, invalidating the connection releases the session
    lock without committing or rolling back caller-owned business state.
    """
    if connection.in_transaction():
        raise RuntimeError("advisory_lock_requires_idle_connection")
    try:
        await connection.execute(
            text("SELECT pg_advisory_lock(hashtextextended(:key, 0))"),
            {"key": key},
        )
        await connection.commit()
    except BaseException:
        if connection.in_transaction():
            await connection.rollback()
        raise

    try:
        yield
    finally:
        if connection.in_transaction():
            await connection.invalidate()
        else:
            try:
                unlocked = await connection.scalar(
                    text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))"),
                    {"key": key},
                )
                await connection.commit()
                if unlocked is not True:
                    await connection.invalidate()
            except BaseException:
                if connection.in_transaction():
                    await connection.rollback()
                await connection.invalidate()
                raise


@asynccontextmanager
async def hold_conversation_delivery_lock(
    conversation_id: uuid.UUID,
) -> AsyncIterator[AsyncConnection]:
    async with get_engine().connect() as connection:
        async with hold_connection_advisory_lock(
            connection,
            _conversation_delivery_key(conversation_id),
        ):
            yield connection
