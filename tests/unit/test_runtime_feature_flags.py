import os
import subprocess
import sys

from apps.scheduler import main as scheduler
from social_reply.application.event_ingestion import x_webhook_health, xchat_subscription
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
                "{'sweep_provisioning_jobs', 'sweep_decision_jobs', 'sweep_outbox', "
                "'poll_x_direct_messages', 'poll_xchat_messages'}"
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


def test_scheduler_sweeps_follow_feature_flags():
    base = [name for name, _sweep in scheduler._build_sweeps(_settings())]
    chatwoot = [name for name, _sweep in scheduler._build_sweeps(_settings(chatwoot=True))]
    no_legacy = [name for name, _sweep in scheduler._build_sweeps(_settings(legacy=False))]
    no_activity = [name for name, _sweep in scheduler._build_sweeps(_settings(activity=False))]
    no_xchat = [name for name, _sweep in scheduler._build_sweeps(_settings(xchat=False))]

    assert chatwoot[0] == "reconcile_chatwoot_messages"
    assert chatwoot[1:] == base
    assert "poll_x_direct_messages" in no_legacy
    assert "poll_xchat_messages" in no_xchat
    assert "ensure_xchat_subscriptions" not in no_xchat
    assert "ensure_x_webhooks_valid" not in no_activity
    assert "ensure_xchat_subscriptions" not in no_activity
    assert "poll_x_direct_messages" in no_activity
    assert "poll_xchat_messages" in no_activity
    assert base.index("poll_x_direct_messages") < base.index("sweep_outbox")
    assert base.index("poll_xchat_messages") < base.index("sweep_outbox")
