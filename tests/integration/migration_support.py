import asyncio
import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


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
