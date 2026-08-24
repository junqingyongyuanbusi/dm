import asyncio

import pytest
from scripts import prepare_database

pytestmark = pytest.mark.integration


async def test_database_preparation_is_serialized_across_concurrent_api_starts(monkeypatch):
    active = 0
    max_active = 0

    async def current_revision():
        return None

    async def migrate():
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.05)
        active -= 1
        return (0, 0, 0)

    monkeypatch.setattr(prepare_database, "_current_revision", current_revision)
    monkeypatch.setattr(prepare_database, "migrate", migrate)
    monkeypatch.setattr(prepare_database.command, "upgrade", lambda *_args: None)

    script = type("Script", (), {"get_revision": lambda *_args: None})()
    await asyncio.gather(
        prepare_database._prepare_locked(object(), script),
        prepare_database._prepare_locked(object(), script),
    )

    assert max_active == 1
