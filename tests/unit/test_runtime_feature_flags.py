import os
import subprocess
import sys
from types import SimpleNamespace

from social_reply.application.event_ingestion import (
    x_dm_poll,
    x_webhook_health,
    xchat_poll,
    xchat_recovery,
    xchat_subscription,
)
from social_reply.shared.config import Settings


def _settings(
    *,
    chatwoot: bool = False,
    legacy: bool = True,
    activity: bool = True,
    xchat: bool = True,
    facebook: bool = True,
    instagram: bool = True,
) -> Settings:
    return Settings(
        _env_file=None,
        testing=True,
        chatwoot_enabled=chatwoot,
        x_legacy_dm_enabled=legacy,
        x_activity_enabled=activity,
        xchat_enabled=xchat,
        facebook_messenger_enabled=facebook,
        instagram_messaging_enabled=instagram,
        platform_secret_keys="Wm5wbamjBFvTmkGIU2NskIKCrJfsb4AdUBDZR-m1-CM=",
    )


def test_x_reconciliation_modules_do_not_parse_settings_at_import_time():
    env = os.environ.copy()
    env.update(
        {
            "X_DM_POLL_INTERVAL_SECONDS": "not-an-integer",
            "XCHAT_POLL_INTERVAL_SECONDS": "not-an-integer",
            "XCHAT_READY_PROBE_INTERVAL_SECONDS": "not-an-integer",
        }
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import social_reply.application.event_ingestion.x_dm_poll; "
                "import social_reply.application.event_ingestion.x_webhook_health; "
                "import social_reply.application.event_ingestion.xchat_poll; "
                "import social_reply.application.event_ingestion.xchat_recovery; "
                "import social_reply.application.event_ingestion.xchat_subscription"
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
                "assert not hasattr(scheduler, '_SWEEPS')"
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


async def test_x_modules_read_one_settings_snapshot_per_public_invocation(monkeypatch):
    calls = {"dm": 0, "poll": 0, "recovery": 0, "subscription": 0, "webhook": 0}
    settings = _settings(activity=False).model_copy(
        update={
            "x_dm_poll_interval_seconds": 0,
            "xchat_poll_interval_seconds": 0,
        }
    )

    def settings_loader(name):
        def load():
            calls[name] += 1
            return settings

        return load

    async def no_accounts(_platform):
        return []

    monkeypatch.setattr(x_dm_poll, "get_settings", settings_loader("dm"))
    monkeypatch.setattr(xchat_poll, "get_settings", settings_loader("poll"))
    monkeypatch.setattr(xchat_recovery, "get_settings", settings_loader("recovery"))
    monkeypatch.setattr(xchat_subscription, "get_settings", settings_loader("subscription"))
    monkeypatch.setattr(x_webhook_health, "get_settings", settings_loader("webhook"))
    monkeypatch.setattr(x_dm_poll, "list_active_accounts_by_platform", no_accounts)
    monkeypatch.setattr(xchat_poll, "list_active_accounts_by_platform", no_accounts)
    x_dm_poll._last_poll_at = None
    xchat_poll._last_poll_at = None
    xchat_recovery._last_sweep_at = 100
    monkeypatch.setattr(xchat_recovery.time, "monotonic", lambda: 100)

    await x_dm_poll.poll_x_direct_messages()
    await xchat_poll.poll_xchat_messages()
    await xchat_recovery.sweep_xchat_recovery()
    await xchat_subscription.ensure_xchat_subscriptions()
    await x_webhook_health.ensure_x_webhooks_valid()

    assert calls == {"dm": 1, "poll": 1, "recovery": 1, "subscription": 1, "webhook": 1}


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
