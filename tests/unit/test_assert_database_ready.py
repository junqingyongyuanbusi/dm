from scripts import assert_database_ready


def test_readiness_entrypoint_only_checks_database_readiness(monkeypatch) -> None:
    events: list[str] = []

    def assert_ready() -> None:
        events.append("ready")

    monkeypatch.setattr(assert_database_ready, "assert_ready", assert_ready)

    assert_database_ready.main()

    assert events == ["ready"]
