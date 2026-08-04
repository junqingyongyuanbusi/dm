import asyncio
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from social_reply.shared.config import get_settings

_REPO_ROOT = Path(__file__).resolve().parents[2]


@asynccontextmanager
async def temporary_database(prefix: str) -> AsyncIterator[str]:
    base_url = make_url(get_settings().database_url)
    database_name = f"{prefix}_{uuid.uuid4().hex[:12]}"
    database_url = base_url.set(database=database_name).render_as_string(hide_password=False)
    admin_engine = create_async_engine(
        base_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    try:
        async with admin_engine.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{database_name}"'))
        yield database_url
    finally:
        try:
            async with admin_engine.connect() as connection:
                await connection.execute(
                    text(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
                )
        finally:
            await admin_engine.dispose()


async def run_alembic(database_url: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "DATABASE_URL": database_url, "TESTING": "true"}
    return await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", *args],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


async def assert_alembic_succeeds(database_url: str, *args: str) -> None:
    result = await run_alembic(database_url, *args)
    assert result.returncode == 0, result.stdout + result.stderr
