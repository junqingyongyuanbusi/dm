"""Restart-safe database preparation for API startup."""

import asyncio

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from migrate_legacy_secrets import migrate
from sqlalchemy.ext.asyncio import create_async_engine

from social_reply.shared.config import get_settings

_SECRET_EXPANSION_REVISION = "b7d1e4a9c2f3"


async def _current_revision() -> str | None:
    engine = create_async_engine(get_settings().database_url)
    async with engine.connect() as connection:
        revision = await connection.run_sync(
            lambda sync: MigrationContext.configure(sync).get_current_revision()
        )
    await engine.dispose()
    return revision


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


def prepare() -> None:
    config = Config("alembic.ini")
    script = ScriptDirectory.from_config(config)
    current = asyncio.run(_current_revision())
    if current is None or not _is_at_or_after(script, current, _SECRET_EXPANSION_REVISION):
        command.upgrade(config, _SECRET_EXPANSION_REVISION)
    asyncio.run(migrate())
    command.upgrade(config, "head")


def assert_ready() -> None:
    config = Config("alembic.ini")
    script = ScriptDirectory.from_config(config)
    current = asyncio.run(_current_revision())
    if current not in script.get_heads():
        raise RuntimeError(f"database_not_at_head:{current}")
    # Idempotent validation: decrypts every envelope and fails on plaintext/corrupt data.
    asyncio.run(migrate())


if __name__ == "__main__":
    prepare()
