from types import SimpleNamespace

from social_reply.infrastructure.database import engine as database_engine


def test_async_engine_hides_statement_parameters(monkeypatch):
    database_url = "postgresql+asyncpg://unit-user:unit-password@localhost/unit_database_test"
    monkeypatch.setattr(database_engine, "_engine", None)
    monkeypatch.setattr(database_engine, "_session_factory", None)
    monkeypatch.setattr(
        database_engine,
        "get_settings",
        lambda: SimpleNamespace(database_url=database_url),
    )

    engine = database_engine.get_engine()

    assert engine.sync_engine.hide_parameters is True
