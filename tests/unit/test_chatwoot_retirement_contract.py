import ast
from pathlib import Path

from apps.api.main import create_app
from apps.scheduler.main import _build_sweep_specs
from social_reply.shared.config import Settings

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_CHATWOOT_RUNTIME_SETTINGS = {
    "chatwoot_enabled",
    "chatwoot_webhook_secret",
    "chatwoot_signature_tolerance_seconds",
    "chatwoot_base_url",
    "chatwoot_api_token",
    "chatwoot_reconcile_interval_seconds",
}
_ACTIVE_RUNTIME_FILES = (
    "apps/api/main.py",
    "apps/worker/main.py",
    "apps/scheduler/main.py",
    "src/social_reply/application/event_ingestion/raw_recovery.py",
    "src/social_reply/application/reply_decision/persist.py",
    "src/social_reply/application/reply_decision/jobs.py",
    "src/social_reply/application/message_delivery/intents.py",
    "src/social_reply/application/message_delivery/outbox.py",
    "src/social_reply/application/message_delivery/sweep.py",
)


def _settings(**updates: object) -> Settings:
    return Settings(
        _env_file=None,
        testing=True,
        platform_secret_keys="Wm5wbamjBFvTmkGIU2NskIKCrJfsb4AdUBDZR-m1-CM=",
        **updates,
    )


def test_chatwoot_webhook_is_not_part_of_the_runtime_route_contract() -> None:
    routes = create_app().openapi()["paths"]

    assert "/webhooks/chatwoot" not in routes


def test_scheduler_has_no_chatwoot_reconciliation_sweep() -> None:
    sweep_names = {spec.name for spec in _build_sweep_specs(_settings())}

    assert "reconcile_chatwoot_messages" not in sweep_names


def test_settings_have_no_chatwoot_runtime_surface() -> None:
    assert _CHATWOOT_RUNTIME_SETTINGS.isdisjoint(Settings.model_fields)


def test_active_runtime_files_do_not_reference_chatwoot() -> None:
    for relative_path in _ACTIVE_RUNTIME_FILES:
        source = (_REPOSITORY_ROOT / relative_path).read_text()
        parsed = ast.parse(source)
        imported_modules = {
            alias.name
            for node in ast.walk(parsed)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module or ""
            for node in ast.walk(parsed)
            if isinstance(node, ast.ImportFrom)
        }

        assert not any(
            "chatwoot" in module.casefold() for module in imported_modules
        ), relative_path
        assert "DEFERRED_CHATWOOT" not in source, relative_path
        assert "DECISION_DEFERRED" not in source, relative_path
        assert "CHATWOOT_DISABLED" not in source, relative_path
        assert "chatwoot_conversation" not in source, relative_path
