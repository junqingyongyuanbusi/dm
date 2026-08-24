import asyncio
from contextlib import asynccontextmanager

from scripts import prepare_database as _module


def test_prepare_serializes_schema_and_secret_work_under_advisory_lock(monkeypatch):
    events: list[str] = []

    @asynccontextmanager
    async def hold_lock():
        events.append("lock")
        try:
            yield
        finally:
            events.append("unlock")

    async def current_revision():
        events.append("current")
        return None

    async def migrate():
        events.append("secrets")
        return (0, 0, 0)

    def upgrade(_config, target):
        events.append(f"upgrade:{target}")

    monkeypatch.setattr(_module, "_hold_database_preparation_lock", hold_lock)
    monkeypatch.setattr(_module, "_current_revision", current_revision)
    monkeypatch.setattr(_module, "migrate", migrate)
    monkeypatch.setattr(_module.command, "upgrade", upgrade)

    script = type("Script", (), {"get_revision": lambda *_args: None})()
    asyncio.run(_module._prepare_locked(object(), script))

    assert events == [
        "lock",
        "current",
        f"upgrade:{_module._SECRET_EXPANSION_REVISION}",
        "secrets",
        "upgrade:head",
        "unlock",
    ]
