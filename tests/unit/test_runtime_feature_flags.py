import os
import subprocess
import sys

from apps.scheduler import main as scheduler


def test_direct_only_production_modules_import_without_chatwoot_credentials():
    env = os.environ.copy()
    env.update(
        {
            "TESTING": "false",
            "CHATWOOT_ENABLED": "false",
            "CHATWOOT_WEBHOOK_SECRET": "",
            "CHATWOOT_API_TOKEN": "",
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
                "assert 'reconcile_chatwoot_messages' not in "
                "{name for name, _ in scheduler._SWEEPS}"
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


def test_scheduler_sweeps_follow_chatwoot_flag():
    disabled = [name for name, _sweep in scheduler._build_sweeps(False)]
    enabled = [name for name, _sweep in scheduler._build_sweeps(True)]

    assert "reconcile_chatwoot_messages" not in disabled
    assert enabled[0] == "reconcile_chatwoot_messages"
    assert enabled[1:] == disabled
