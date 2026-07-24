import asyncio
import os
import subprocess
import sys
from types import SimpleNamespace

from apps.scheduler import main as scheduler
from social_reply.application.event_ingestion import (
    x_dm_poll,
    x_webhook_health,
    xchat_poll,
    xchat_subscription,
)
from social_reply.shared.config import Settings


def _settings(
    *,
    chatwoot: bool = False,
    legacy: bool = True,
    activity: bool = True,
    xchat: bool = True,
) -> Settings:
    return Settings(
        _env_file=None,
        testing=True,
        chatwoot_enabled=chatwoot,
        x_legacy_dm_enabled=legacy,
        x_activity_enabled=activity,
        xchat_enabled=xchat,
        platform_secret_keys="Wm5wbamjBFvTmkGIU2NskIKCrJfsb4AdUBDZR-m1-CM=",
    )


def test_direct_only_production_modules_import_without_chatwoot_credentials():
    env = os.environ.copy()
    env.update(
        {
            "TESTING": "false",
            "CHATWOOT_ENABLED": "false",
            "CHATWOOT_WEBHOOK_SECRET": "",
            "CHATWOOT_API_TOKEN": "",
            "X_LEGACY_DM_ENABLED": "false",
            "X_ACTIVITY_ENABLED": "false",
            "XCHAT_ENABLED": "false",
            "FACEBOOK_MESSENGER_ENABLED": "false",
            "INSTAGRAM_MESSAGING_ENABLED": "false",
            "WHATSAPP_ENABLED": "false",
            "CONTROL_API_KEY": "control-token",
            "ADMIN_SESSION_SECRET": "x" * 32,
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "strong-password",
            "ADMIN_ALLOWED_TENANTS": "default",
            "PUBLIC_BASE_URL": "https://reply.example.com",
            "PLATFORM_SECRET_KEYS": "Wm5wbamjBFvTmkGIU2NskIKCrJfsb4AdUBDZR-m1-CM=",
            "LLM_PROVIDER": "openai",
            "OPENAI_API_KEY": "sk-test",
        }
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "import apps.api.main; "
                "import apps.worker.main; "
                "import apps.scheduler.main as scheduler; "
                "assert 'social_reply.application.event_ingestion.actors' in sys.modules; "
                "assert 'social_reply.application.event_ingestion.reconcile' not in sys.modules; "
                "assert 'social_reply.connectors.x.router' not in sys.modules; "
                "assert 'social_reply.application.event_ingestion.xchat_actors' in sys.modules; "
                "assert {name for name, _ in scheduler._SWEEPS} == "
                "{'sweep_provisioning_jobs', 'sweep_initial_raw_events', "
                "'sweep_decision_jobs', 'sweep_outbox'}"
            ),
        ],
        cwd=os.getcwd(),
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr


async def test_disabled_activity_sweeps_do_not_load_accounts(monkeypatch):
    async def unexpected_accounts(_platform):
        raise AssertionError("disabled sweep must not query accounts")

    monkeypatch.setattr(
        xchat_subscription,
        "get_settings",
        lambda: _settings(activity=False, xchat=False),
    )
    monkeypatch.setattr(
        x_webhook_health,
        "get_settings",
        lambda: _settings(activity=False),
    )
    monkeypatch.setattr(
        xchat_subscription,
        "list_active_accounts_by_platform",
        unexpected_accounts,
    )
    monkeypatch.setattr(
        x_webhook_health,
        "list_active_accounts_by_platform",
        unexpected_accounts,
    )

    assert await xchat_subscription.ensure_xchat_subscriptions() == []
    assert await x_webhook_health.ensure_x_webhooks_valid() == []


async def test_reconciliation_sweeps_run_immediately_after_process_start(monkeypatch):
    called: list[str] = []

    def account_loader(name):
        async def load(_platform):
            called.append(name)
            return []

        return load

    monkeypatch.setattr(x_dm_poll.time, "monotonic", lambda: 1.0)
    monkeypatch.setattr(x_dm_poll, "get_settings", lambda: _settings())
    monkeypatch.setattr(xchat_subscription, "get_settings", lambda: _settings())
    monkeypatch.setattr(x_webhook_health, "get_settings", lambda: _settings())
    monkeypatch.setattr(x_dm_poll, "list_active_accounts_by_platform", account_loader("legacy"))
    monkeypatch.setattr(xchat_poll, "list_active_accounts_by_platform", account_loader("xchat"))
    monkeypatch.setattr(
        xchat_subscription,
        "list_active_accounts_by_platform",
        account_loader("subscriptions"),
    )
    monkeypatch.setattr(
        x_webhook_health,
        "list_active_accounts_by_platform",
        account_loader("health"),
    )
    x_dm_poll._last_poll_at = None
    xchat_poll._last_poll_at = None
    xchat_subscription._last_check_at = None
    x_webhook_health._last_check_at = None

    await x_dm_poll.poll_x_direct_messages()
    await xchat_poll.poll_xchat_messages()
    await xchat_subscription.ensure_xchat_subscriptions()
    await x_webhook_health.ensure_x_webhooks_valid()

    assert called == ["legacy", "xchat", "subscriptions", "health"]


async def test_zero_monotonic_timestamp_is_still_throttled(monkeypatch):
    calls = 0

    async def list_accounts(_platform):
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setattr(x_dm_poll.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(x_dm_poll, "get_settings", lambda: _settings())
    monkeypatch.setattr(x_dm_poll, "list_active_accounts_by_platform", list_accounts)
    x_dm_poll._last_poll_at = None

    await x_dm_poll.poll_x_direct_messages()
    await x_dm_poll.poll_x_direct_messages()

    assert calls == 1


async def test_xchat_poll_requires_strict_capability(monkeypatch):
    accounts = [
        SimpleNamespace(capability={}),
        SimpleNamespace(capability={"x_chat": False}),
        SimpleNamespace(capability={"x_chat": "false"}),
    ]

    async def list_accounts(_platform):
        return accounts

    def unexpected_credentials(_account):
        raise AssertionError("invalid XChat capability must not read credentials")

    monkeypatch.setattr(xchat_poll, "list_active_accounts_by_platform", list_accounts)
    monkeypatch.setattr(xchat_poll, "x_credentials", unexpected_credentials)
    monkeypatch.setattr(xchat_poll.time, "monotonic", lambda: 1000.0)
    xchat_poll._last_poll_at = None

    assert await xchat_poll.poll_xchat_messages() == []


def test_scheduler_cycle_isolates_sweep_failures(monkeypatch):
    calls: list[str] = []

    async def broken():
        calls.append("broken")
        raise RuntimeError("boom")

    async def healthy():
        calls.append("healthy")
        return ["recovered"]

    monkeypatch.setattr(scheduler, "run_on_actor_loop", asyncio.run)
    scheduler._run_sweep_cycle((("broken", broken), ("healthy", healthy)))

    assert calls == ["broken", "healthy"]


def test_scheduler_sweeps_follow_feature_flags():
    base = [name for name, _sweep in scheduler._build_sweeps(_settings())]
    chatwoot = [name for name, _sweep in scheduler._build_sweeps(_settings(chatwoot=True))]
    no_legacy = [name for name, _sweep in scheduler._build_sweeps(_settings(legacy=False))]
    no_activity = [name for name, _sweep in scheduler._build_sweeps(_settings(activity=False))]
    no_xchat = [name for name, _sweep in scheduler._build_sweeps(_settings(xchat=False))]

    assert chatwoot[0] == "reconcile_chatwoot_messages"
    assert chatwoot[1:] == base
    assert base.index("sweep_provisioning_jobs") < base.index("sweep_initial_raw_events")
    assert base.index("sweep_initial_raw_events") < base.index("sweep_decision_jobs")
    assert "poll_x_direct_messages" not in no_legacy
    assert "poll_xchat_messages" not in no_xchat
    assert "ensure_xchat_subscriptions" in no_xchat
    assert "sweep_xchat_recovery" not in no_xchat
    assert "ensure_x_webhooks_valid" not in no_activity
    assert "ensure_xchat_subscriptions" not in no_activity
    assert "poll_x_direct_messages" in no_activity
    assert "poll_xchat_messages" in no_activity
    assert base.index("poll_x_direct_messages") < base.index("sweep_outbox")
    assert base.index("poll_xchat_messages") < base.index("sweep_outbox")
