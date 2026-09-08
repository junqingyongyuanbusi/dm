import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from social_reply.infrastructure.database.engine import get_engine


def _conversation_delivery_key(conversation_id: uuid.UUID) -> str:
    return f"social-reply:conversation-delivery:{conversation_id}"


async def _invalidate_connection_best_effort(connection: AsyncConnection) -> None:
    invalidate_task = asyncio.create_task(connection.invalidate())
    try:
        await asyncio.shield(invalidate_task)
    except asyncio.CancelledError:
        try:
            await invalidate_task
        except BaseException:
            pass
    except BaseException:
        pass


async def acquire_xact_lock(session: AsyncSession, key: str) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": key},
    )


async def acquire_shared_xact_lock(session: AsyncSession, key: str) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock_shared(hashtextextended(:key, 0))"),
        {"key": key},
    )


async def acquire_conversation_delivery_xact_lock(
    session: AsyncSession,
    conversation_id: uuid.UUID,
) -> None:
    await acquire_xact_lock(session, _conversation_delivery_key(conversation_id))


@asynccontextmanager
async def _hold_connection_advisory_lock(
    connection: AsyncConnection,
    key: str,
    *,
    shared: bool,
    allow_transaction: bool = False,
) -> AsyncIterator[None]:
    """Hold a session advisory lock without owning caller business transactions.

    By default, the connection must be idle on entry. ``allow_transaction`` is reserved for
    callers that already own the transaction and need the session lock to outlive its commit.
    Lock acquisition itself never commits a caller-owned transaction. If work leaves a
    transaction open, invalidating the connection releases the session lock without committing
    or rolling back caller-owned business state.
    """
    transaction_on_entry = connection.in_transaction()
    if transaction_on_entry and not allow_transaction:
        raise RuntimeError("advisory_lock_requires_idle_connection")
    lock_function = "pg_advisory_lock_shared" if shared else "pg_advisory_lock"
    unlock_function = "pg_advisory_unlock_shared" if shared else "pg_advisory_unlock"
    try:
        await connection.execute(
            text(f"SELECT {lock_function}(hashtextextended(:key, 0))"),
            {"key": key},
        )
        if not transaction_on_entry:
            await connection.commit()
    except BaseException:
        try:
            if connection.in_transaction():
                await connection.rollback()
        except BaseException:
            pass
        # The server may have acquired a session lock before the await failed. Do not return
        # this connection to the pool, even when the acquisition outcome is uncertain.
        await _invalidate_connection_best_effort(connection)
        raise

    try:
        yield
    finally:
        if connection.in_transaction():
            await _invalidate_connection_best_effort(connection)
        else:
            try:
                unlocked = await connection.scalar(
                    text(f"SELECT {unlock_function}(hashtextextended(:key, 0))"),
                    {"key": key},
                )
                await connection.commit()
                if unlocked is not True:
                    await _invalidate_connection_best_effort(connection)
            except BaseException:
                try:
                    if connection.in_transaction():
                        await connection.rollback()
                except BaseException:
                    pass
                await _invalidate_connection_best_effort(connection)
                raise


@asynccontextmanager
async def hold_connection_advisory_lock(
    connection: AsyncConnection,
    key: str,
) -> AsyncIterator[None]:
    async with _hold_connection_advisory_lock(connection, key, shared=False):
        yield


@asynccontextmanager
async def hold_connection_advisory_shared_lock(
    connection: AsyncConnection,
    key: str,
) -> AsyncIterator[None]:
    """Allow concurrent readers while serializing against an exclusive mutation."""
    async with _hold_connection_advisory_lock(connection, key, shared=True):
        yield


@asynccontextmanager
async def hold_connection_advisory_lock_in_transaction(
    connection: AsyncConnection,
    key: str,
) -> AsyncIterator[None]:
    """Hold an exclusive session lock while a caller-owned transaction may commit."""
    async with _hold_connection_advisory_lock(
        connection,
        key,
        shared=False,
        allow_transaction=True,
    ):
        yield


@asynccontextmanager
async def hold_connection_advisory_shared_lock_in_transaction(
    connection: AsyncConnection,
    key: str,
) -> AsyncIterator[None]:
    """Hold a shared session lock while a caller-owned transaction may commit."""
    async with _hold_connection_advisory_lock(
        connection,
        key,
        shared=True,
        allow_transaction=True,
    ):
        yield


@asynccontextmanager
async def hold_conversation_delivery_lock_on_connection(
    connection: AsyncConnection,
    conversation_id: uuid.UUID,
) -> AsyncIterator[None]:
    """Hold conversation serialization on a caller-owned idle connection."""
    async with hold_connection_advisory_lock(
        connection,
        _conversation_delivery_key(conversation_id),
    ):
        yield


@asynccontextmanager
async def hold_conversation_delivery_lock_on_connection_in_transaction(
    connection: AsyncConnection,
    conversation_id: uuid.UUID,
) -> AsyncIterator[None]:
    """Hold conversation serialization across commits on a caller-owned connection."""
    async with hold_connection_advisory_lock_in_transaction(
        connection,
        _conversation_delivery_key(conversation_id),
    ):
        yield


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
