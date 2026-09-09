import json
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest
from scripts import apply_startup_cutover as startup


def make_payload(**changes):
    cutover_id = uuid.uuid4()
    return {
        "cutover_id": str(cutover_id),
        "before": "2020-01-01T00:00:00+00:00",
        "tenant": "default",
        "restore_admin_ids": [str(uuid.uuid4()), str(uuid.uuid4())],
        "namespace": f"dramatiq-cutover-{cutover_id.hex}",
        "processes_stopped": True,
        **changes,
    }


def parse_payload(payload, role="api", mode="apply"):
    return startup.parse_cutover(
        json.dumps(payload), role=role, mode=mode, namespace=payload["namespace"],
    )


@pytest.mark.parametrize("role,mode", [
    ("worker", "apply"), ("scheduler", "apply"), ("unknown", "check"), ("api", "check"),
])
def test_role_and_operation_must_match(role, mode):
    with pytest.raises(ValueError):
        parse_payload(make_payload(), role, mode)


@pytest.mark.parametrize("changes", [
    {"processes_stopped": False}, {"processes_stopped": "true"}, {"extra": "unknown"},
    {"tenant": "other"}, {"before": "2035-01-01"}, {"namespace": "dramatiq"},
    {"before": "9999-01-01T00:00:00+00:00"},
    {"restore_admin_ids": ["not-a-uuid"]}, {"cutover_id": "not-a-uuid"},
])
def test_invalid_envelope_fails_closed(changes):
    with pytest.raises(ValueError):
        parse_payload(make_payload(**changes))


def test_namespace_must_match_runtime_configuration():
    with pytest.raises(ValueError, match="namespace"):
        startup.parse_cutover(
            json.dumps(make_payload()), role="api", mode="apply", namespace="dramatiq",
        )


async def test_matching_audit_allows_same_id_restart_without_redis_or_writes(monkeypatch):
    request = parse_payload(make_payload())
    audit = SimpleNamespace(
        action=startup.AUDIT_ACTION, tenant_id="default",
        detail={"parameters": request.parameters()},
    )
    session = AsyncMock()
    session.get.return_value = audit
    check_namespace = AsyncMock()
    apply_cutover = AsyncMock()
    monkeypatch.setattr(startup, "assert_namespace_unused", check_namespace)
    monkeypatch.setattr(startup, "run_cutover", apply_cutover)
    for role in ("api", "worker", "scheduler", "api"):
        await startup.ensure_cutover(
            session, request, role=role,
            settings=SimpleNamespace(dramatiq_namespace=request.startup_namespace),
        )
    check_namespace.assert_not_called()
    apply_cutover.assert_not_called()


@pytest.mark.parametrize("role", ["worker", "scheduler"])
async def test_consumers_require_committed_audit_before_startup(role):
    session = AsyncMock()
    session.get.return_value = None
    request = parse_payload(make_payload())
    with pytest.raises(ValueError, match="audit_required"):
        await startup.ensure_cutover(
            session, request, role=role,
            settings=SimpleNamespace(dramatiq_namespace=request.startup_namespace),
        )


async def test_audit_conflict_never_applies_again(monkeypatch):
    session = AsyncMock()
    session.get.return_value = SimpleNamespace(
        action=startup.AUDIT_ACTION, tenant_id="default", detail={"parameters": {}},
    )
    apply_cutover = AsyncMock()
    monkeypatch.setattr(startup, "run_cutover", apply_cutover)
    request = parse_payload(make_payload())
    with pytest.raises(ValueError, match="parameter_conflict"):
        await startup.ensure_cutover(
            session, request, role="api",
            settings=SimpleNamespace(dramatiq_namespace=request.startup_namespace),
        )
    apply_cutover.assert_not_called()


async def test_plain_cli_audit_does_not_prove_namespace_was_checked():
    request = parse_payload(make_payload())
    plain_request = replace(request, startup_namespace=None)
    session = AsyncMock()
    session.get.return_value = SimpleNamespace(
        action=startup.AUDIT_ACTION, tenant_id="default",
        detail={"parameters": plain_request.parameters()},
    )
    settings = SimpleNamespace(dramatiq_namespace=request.startup_namespace)
    with pytest.raises(ValueError, match="parameter_conflict"):
        await startup.ensure_cutover(session, request, role="worker", settings=settings)
    with pytest.raises(ValueError, match="startup_namespace_required"):
        await startup.ensure_cutover(session, plain_request, role="api", settings=settings)


async def test_first_api_apply_checks_namespace_before_atomic_retirement(monkeypatch):
    request = parse_payload(make_payload())
    settings = SimpleNamespace(dramatiq_namespace=request.startup_namespace)
    session = AsyncMock()
    session.get.return_value = None
    sequence = Mock()
    sequence.attach_mock(session.rollback, "rollback")
    check_namespace = AsyncMock()
    apply_cutover = AsyncMock()
    sequence.attach_mock(check_namespace, "check_namespace")
    sequence.attach_mock(apply_cutover, "apply_cutover")
    monkeypatch.setattr(startup, "assert_namespace_unused", check_namespace)
    monkeypatch.setattr(startup, "run_cutover", apply_cutover)
    await startup.ensure_cutover(session, request, role="api", settings=settings)
    assert sequence.mock_calls == [
        call.rollback(), call.check_namespace(settings), call.apply_cutover(session, request),
    ]
    assert request.parameters()["namespace"] == settings.dramatiq_namespace
    assert request.parameters()["startup_contract"] == "fresh-queue-v1"


@pytest.mark.parametrize("error", [ValueError("namespace_used"), TimeoutError(), OSError()])
async def test_namespace_failure_never_starts_retirement(monkeypatch, error):
    request = parse_payload(make_payload())
    session = AsyncMock()
    session.get.return_value = None
    monkeypatch.setattr(startup, "assert_namespace_unused", AsyncMock(side_effect=error))
    apply_cutover = AsyncMock()
    monkeypatch.setattr(startup, "run_cutover", apply_cutover)
    with pytest.raises(type(error)):
        await startup.ensure_cutover(
            session, request, role="api",
            settings=SimpleNamespace(dramatiq_namespace=request.startup_namespace),
        )
    apply_cutover.assert_not_called()


@pytest.mark.parametrize("occupied", ["exact", "child", None])
async def test_namespace_check_checks_both_exact_and_child_keys(monkeypatch, occupied):
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.exists.return_value = int(occupied == "exact")

    async def scan_keys(**kwargs):
        assert kwargs == {"match": "fresh:*", "count": 500}
        if occupied == "child":
            yield b"fresh:queue"

    client.scan_iter = scan_keys
    monkeypatch.setattr(startup.Redis, "from_url", Mock(return_value=client))
    settings = SimpleNamespace(dramatiq_namespace="fresh", redis_url="redis://unused")
    if occupied:
        with pytest.raises(ValueError, match="namespace_already_used"):
            await startup.assert_namespace_unused(settings)
    else:
        await startup.assert_namespace_unused(settings)
    client.exists.assert_awaited_once_with("fresh")
    client.__aexit__.assert_awaited_once()


def test_unconfigured_startup_is_noop(monkeypatch):
    monkeypatch.delenv("WORKSPACE_QUEUE_CUTOVER", raising=False)
    monkeypatch.setattr(startup, "get_settings", lambda: pytest.fail("must remain a no-op"))
    assert startup.main(["--validate"]) == 0


def test_entrypoint_and_image_include_fail_closed_startup_hooks():
    entrypoint = Path("entrypoint.sh").read_text()
    api = entrypoint[entrypoint.index("  api)"):entrypoint.index("  worker)")]
    assert api.index("scripts.prepare_database") < api.index("--apply") < api.index("exec uvicorn")
    for branch in ("worker", "scheduler"):
        section = entrypoint.split(f"  {branch})", 1)[1].split(";;", 1)[0]
        assert section.index("scripts.assert_database_ready") < section.index("--check")
        assert section.index("--check") < section.index("exec ")
    dockerfile = Path("Dockerfile").read_text()
    assert "scripts/apply_startup_cutover.py" in dockerfile
    assert "/app/.venv/bin/python -m scripts.apply_startup_cutover --validate" in dockerfile
