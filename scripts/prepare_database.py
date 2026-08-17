"""Restart-safe database preparation for API startup."""

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from scripts.migrate_legacy_secrets import migrate
from social_reply.application.knowledge.readiness import (
    assert_knowledge_readiness,
    knowledge_readiness_report,
)
from social_reply.infrastructure.database.engine import get_engine, get_session_factory
from social_reply.shared.config import get_settings

_SECRET_EXPANSION_REVISION = "b7d1e4a9c2f3"
_DATABASE_PREPARATION_LOCK = "social-reply:database-preparation"
_DATABASE_PREPARATION_LOCK_TIMEOUT = timedelta(minutes=5)
_DATABASE_PREPARATION_LOCK_RETRY_SECONDS = 1.0


async def _current_revision() -> str | None:
    engine = create_async_engine(get_settings().database_url)
    async with engine.connect() as connection:
        revision = await connection.run_sync(
            lambda sync: MigrationContext.configure(sync).get_current_revision()
        )
    await engine.dispose()
    return revision


async def _assert_multilingual_knowledge_ready() -> None:
    settings = get_settings()
    if not settings.multilingual_knowledge_reply_enabled:
        return
    async with get_session_factory()() as session:
        report = await knowledge_readiness_report(
            session,
            expected_embedding_version=settings.openai_embedding_model,
        )
    assert_knowledge_readiness(report)
    if report["corpus_fingerprint"] != settings.knowledge_corpus_version:
        raise RuntimeError("multilingual_knowledge_not_ready:corpus_fingerprint_mismatch")


@asynccontextmanager
async def _hold_database_preparation_lock() -> AsyncIterator[None]:
    deadline = time.monotonic() + _DATABASE_PREPARATION_LOCK_TIMEOUT.total_seconds()
    while True:
        async with get_engine().connect() as connection:
            acquired = await connection.scalar(
                text("SELECT pg_try_advisory_lock(hashtextextended(:key, 0))"),
                {"key": _DATABASE_PREPARATION_LOCK},
            )
            await connection.commit()
            if acquired is True:
                try:
                    yield
                finally:
                    unlocked = await connection.scalar(
                        text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))"),
                        {"key": _DATABASE_PREPARATION_LOCK},
                    )
                    await connection.commit()
                    if unlocked is not True:
                        await connection.invalidate()
                return
        if time.monotonic() >= deadline:
            raise TimeoutError("database_preparation_lock_timeout")
        await asyncio.sleep(_DATABASE_PREPARATION_LOCK_RETRY_SECONDS)


def _is_at_or_after(script: ScriptDirectory, current: str, target: str) -> bool:
    revision = script.get_revision(current)
    while revision is not None:
        if revision.revision == target:
            return True
        down_revision = revision.down_revision
        if not isinstance(down_revision, str):
            return False
        revision = script.get_revision(down_revision)
    return False


async def _prepare_locked(config: Config, script: ScriptDirectory) -> None:
    async with _hold_database_preparation_lock():
        current = await _current_revision()
        if current is None or not _is_at_or_after(script, current, _SECRET_EXPANSION_REVISION):
            await asyncio.to_thread(command.upgrade, config, _SECRET_EXPANSION_REVISION)
        await migrate()
        await asyncio.to_thread(command.upgrade, config, "head")
        await _assert_multilingual_knowledge_ready()


def prepare() -> None:
    config = Config("alembic.ini")
    script = ScriptDirectory.from_config(config)
    asyncio.run(_prepare_locked(config, script))


async def _assert_ready_async(script: ScriptDirectory) -> None:
    current = await _current_revision()
    if current not in script.get_heads():
        raise RuntimeError(f"database_not_at_head:{current}")
    await _assert_multilingual_knowledge_ready()
    # Idempotent validation: decrypts every envelope and fails on plaintext/corrupt data.
    await migrate()


def assert_ready() -> None:
    config = Config("alembic.ini")
    script = ScriptDirectory.from_config(config)
    asyncio.run(_assert_ready_async(script))


if __name__ == "__main__":
    prepare()
