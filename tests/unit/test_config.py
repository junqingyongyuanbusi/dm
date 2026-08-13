# 配置校验测试（Plan 2c Task 0）：生产环境拒绝默认/空凭证
import ast
import re
from pathlib import Path

import pytest

from social_reply.shared.config import Settings

# 绕过 .env 与环境变量干扰的相关变量名
_ENV_KEYS = [
    "CHATWOOT_ENABLED",
    "CHATWOOT_WEBHOOK_SECRET",
    "CHATWOOT_API_TOKEN",
    "CONTROL_API_KEY",
    "LLM_PROVIDER",
    "OPENAI_API_KEY",
    "PLATFORM_SECRET_KEYS",
    "X_API_KEY",
    "X_API_SECRET",
    "X_LEGACY_DM_ENABLED",
    "X_ACTIVITY_ENABLED",
    "XCHAT_ENABLED",
    "X_PUBLIC_REPLY_ENABLED",
    "X_OAUTH_LEGACY_STATE_WRITE",
    "SCHEDULER_TICK_SECONDS",
    "SCHEDULER_CORE_INTERVAL_SECONDS",
    "SCHEDULER_CORE_WARN_AFTER_SECONDS",
    "SCHEDULER_INSPECTION_WARN_AFTER_SECONDS",
    "CHATWOOT_RECONCILE_INTERVAL_SECONDS",
    "X_DM_POLL_INTERVAL_SECONDS",
    "X_WEBHOOK_CHECK_INTERVAL_SECONDS",
    "XCHAT_POLL_INTERVAL_SECONDS",
    "XCHAT_MAX_CONVERSATIONS_PER_POLL",
    "XCHAT_SUBSCRIPTION_CHECK_INTERVAL_SECONDS",
    "XCHAT_RECOVERY_SWEEP_INTERVAL_SECONDS",
    "XCHAT_READY_PROBE_INTERVAL_SECONDS",
    "XCHAT_PENDING_PROBE_INTERVAL_SECONDS",
    "FACEBOOK_MESSENGER_ENABLED",
    "INSTAGRAM_MESSAGING_ENABLED",
    "WHATSAPP_ENABLED",
    "FEISHU_ENABLED",
    "EMAIL_ENABLED",
    "EMAIL_AUTO_REPLY_ENABLED",
    "EMAIL_POLL_INTERVAL_SECONDS",
    "EMAIL_MAX_MESSAGES_PER_POLL",
    "EMAIL_PER_SENDER_DAILY_REPLY_LIMIT",
    "EMAIL_NETWORK_TIMEOUT_SECONDS",
    "EMAIL_ALLOWED_HOSTS",
    "FEISHU_HEALTH_CHECK_INTERVAL_SECONDS",
    "META_AUTO_REPLY_ENABLED",
    "FACEBOOK_APP_ID",
    "FACEBOOK_APP_SECRET",
    "META_VERIFY_TOKEN",
    "INSTAGRAM_APP_ID",
    "INSTAGRAM_APP_SECRET",
    "INSTAGRAM_VERIFY_TOKEN",
    "ADMIN_ALLOWED_TENANTS",
    "CONVERSATION_HISTORY_LIMIT",
    "CONVERSATION_HISTORY_MAX_CHARS",
    "TESTING",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # 清空相关环境变量，避免本机 .env/环境影响构造参数
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _make(**kwargs: object) -> Settings:
    kwargs.setdefault("platform_secret_keys", "Wm5wbamjBFvTmkGIU2NskIKCrJfsb4AdUBDZR-m1-CM=")
    # 显式关闭 env 文件读取，纯用构造参数
    return Settings(_env_file=None, **kwargs)  # type: ignore[call-arg]


_EMAIL_ENV_DEFAULTS = {
    "EMAIL_ENABLED": "false",
    "EMAIL_AUTO_REPLY_ENABLED": "false",
    "EMAIL_POLL_INTERVAL_SECONDS": "60",
    "EMAIL_MAX_MESSAGES_PER_POLL": "100",
    "EMAIL_PER_SENDER_DAILY_REPLY_LIMIT": "5",
    "EMAIL_NETWORK_TIMEOUT_SECONDS": "10",
    "EMAIL_ALLOWED_HOSTS": "imap.larksuite.com,smtp.larksuite.com",
}
_ENV_ASSIGNMENT = re.compile(r"^(?P<key>[A-Z][A-Z0-9_]*)=(?P<value>.*)$")
_MARKDOWN_ENV_ROW = re.compile(r"^\| `(?P<key>[A-Z][A-Z0-9_]*)` \|")


def _parse_env_template(path: Path) -> dict[str, str]:
    assignments: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text().splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _ENV_ASSIGNMENT.fullmatch(line)
        if match is None:
            continue
        key = match.group("key")
        if key in assignments:
            raise AssertionError(f"duplicate env key {key} in {path}:{line_number}")
        assignments[key] = match.group("value")
    return assignments


def _configuration_email_keys(path: Path) -> set[str]:
    lines = path.read_text().splitlines()
    start = lines.index("## Email integration") + 1
    section = []
    for line in lines[start:]:
        if line.startswith("## "):
            break
        section.append(line)
    return {
        match.group("key")
        for line in section
        if (match := _MARKDOWN_ENV_ROW.match(line)) is not None
    }


def _migration_heads(versions_dir: Path) -> set[str]:
    revisions: set[str] = set()
    parents: set[str] = set()
    for path in versions_dir.glob("*.py"):
        values: dict[str, object] = {}
        for node in ast.parse(path.read_text()).body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name) and target.id in {"revision", "down_revision"}:
                    values[target.id] = ast.literal_eval(node.value)
        revision = values.get("revision")
        down_revision = values.get("down_revision")
        if not isinstance(revision, str):
            continue
        revisions.add(revision)
        if isinstance(down_revision, str):
            parents.add(down_revision)
        elif isinstance(down_revision, tuple):
            parents.update(parent for parent in down_revision if isinstance(parent, str))
    return revisions - parents


def test_testing_true_默认值可用() -> None:
    settings = _make(testing=True)
    assert settings.chatwoot_enabled is False
    assert settings.chatwoot_api_token == "dev-local-token"
    assert settings.openai_api_key.get_secret_value() == ""
    assert settings.openai_base_url == "https://api.openai.com/v1"
    assert settings.openai_model == "gpt-4o-mini"
    assert settings.openai_timeout_seconds == 30.0
    assert settings.prompt_version == "v1-wikifx-multilingual"
    assert settings.x_legacy_dm_enabled is True
    assert settings.x_activity_enabled is True
    assert settings.xchat_enabled is True
    assert settings.x_integration_enabled is True
    assert settings.scheduler_tick_seconds == 0.5
    assert settings.scheduler_core_interval_seconds == 3
    assert settings.scheduler_core_warn_after_seconds == 30
    assert settings.scheduler_inspection_warn_after_seconds == 300
    assert settings.chatwoot_reconcile_interval_seconds == 3
    assert settings.x_dm_poll_interval_seconds == 90
    assert settings.x_webhook_check_interval_seconds == 600
    assert settings.xchat_poll_interval_seconds == 900
    assert settings.xchat_max_conversations_per_poll == 10
    assert settings.xchat_subscription_check_interval_seconds == 600
    assert settings.xchat_recovery_sweep_interval_seconds == 30
    assert settings.xchat_ready_probe_interval_seconds == 21600
    assert settings.xchat_pending_probe_interval_seconds == 600
    assert settings.facebook_messenger_enabled is True
    assert settings.instagram_messaging_enabled is True
    assert settings.whatsapp_enabled is True
    assert settings.feishu_enabled is False
    assert settings.email_enabled is False
    assert settings.email_auto_reply_enabled is False
    assert settings.email_poll_interval_seconds == 60
    assert settings.email_max_messages_per_poll == 100
    assert settings.email_per_sender_daily_reply_limit == 5
    assert settings.email_network_timeout_seconds == 10.0
    assert settings.email_allowed_hosts == frozenset({"imap.larksuite.com", "smtp.larksuite.com"})
    assert settings.meta_auto_reply_enabled is False
    assert settings.meta_health_check_interval_seconds == 600
    assert settings.feishu_health_check_interval_seconds == 600


def test_非测试环境_默认_chatwoot_api_token_拒绝() -> None:
    with pytest.raises(ValueError, match="CHATWOOT_API_TOKEN"):
        _make(
            testing=False,
            chatwoot_enabled=True,
            chatwoot_webhook_secret="real-secret",
        )


def test_非测试环境_空_chatwoot_api_token_拒绝() -> None:
    with pytest.raises(ValueError, match="CHATWOOT_API_TOKEN"):
        _make(
            testing=False,
            chatwoot_enabled=True,
            chatwoot_webhook_secret="real-secret",
            chatwoot_api_token="",
        )


def test_非测试环境_禁用_chatwoot_无需其凭证() -> None:
    settings = _make(
        testing=False,
        chatwoot_enabled=False,
        chatwoot_webhook_secret="",
        chatwoot_api_token="",
        control_api_key="control-token",
        llm_provider="openai",
        openai_api_key="sk-test",
    )
    assert settings.chatwoot_enabled is False


def test_非测试环境_openai_provider_空_key_拒绝() -> None:
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        _make(
            testing=False,
            chatwoot_webhook_secret="real-secret",
            chatwoot_api_token="real-token",
            control_api_key="control-token",
            llm_provider="openai",
        )


def test_非测试环境_凭证齐全通过() -> None:
    settings = _make(
        testing=False,
        chatwoot_enabled=True,
        chatwoot_webhook_secret="real-secret",
        chatwoot_api_token="real-token",
        control_api_key="control-token",
        llm_provider="openai",
        openai_api_key="sk-test",
    )
    assert settings.openai_api_key.get_secret_value() == "sk-test"
    assert "sk-test" not in repr(settings)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("conversation_history_limit", -1),
        ("conversation_history_limit", 51),
        ("conversation_history_max_chars", -1),
        ("conversation_history_max_chars", 50001),
    ],
)
def test_conversation_history_bounds_are_validated(name: str, value: int) -> None:
    with pytest.raises(ValueError):
        _make(testing=True, **{name: value})


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("scheduler_tick_seconds", 0.049),
        ("scheduler_tick_seconds", 10.01),
        ("scheduler_core_interval_seconds", 0.49),
        ("scheduler_core_interval_seconds", 60.01),
        ("scheduler_core_warn_after_seconds", 0.99),
        ("scheduler_core_warn_after_seconds", 3600.01),
        ("scheduler_inspection_warn_after_seconds", 0.99),
        ("scheduler_inspection_warn_after_seconds", 7200.01),
        ("chatwoot_reconcile_interval_seconds", 0),
        ("chatwoot_reconcile_interval_seconds", 3601),
        ("x_dm_poll_interval_seconds", -1),
        ("x_dm_poll_interval_seconds", 86401),
        ("x_webhook_check_interval_seconds", -1),
        ("x_webhook_check_interval_seconds", 86401),
        ("xchat_poll_interval_seconds", -1),
        ("xchat_poll_interval_seconds", 86401),
        ("xchat_max_conversations_per_poll", 0),
        ("xchat_max_conversations_per_poll", 1001),
        ("xchat_subscription_check_interval_seconds", -1),
        ("xchat_subscription_check_interval_seconds", 86401),
        ("xchat_recovery_sweep_interval_seconds", -1),
        ("xchat_recovery_sweep_interval_seconds", 3601),
        ("xchat_ready_probe_interval_seconds", -1),
        ("xchat_ready_probe_interval_seconds", 604801),
        ("xchat_pending_probe_interval_seconds", -1),
        ("xchat_pending_probe_interval_seconds", 86401),
        ("email_poll_interval_seconds", 4),
        ("email_poll_interval_seconds", 3601),
        ("email_max_messages_per_poll", 0),
        ("email_max_messages_per_poll", 1001),
        ("email_per_sender_daily_reply_limit", 0),
        ("email_per_sender_daily_reply_limit", 101),
        ("email_network_timeout_seconds", 0.99),
        ("email_network_timeout_seconds", 120.01),
        ("feishu_health_check_interval_seconds", 59),
        ("feishu_health_check_interval_seconds", 86401),
    ],
)
def test_scheduler_and_platform_reconciliation_bounds(name: str, value: float) -> None:
    with pytest.raises(ValueError):
        _make(testing=True, **{name: value})


def test_email_allowed_hosts_are_canonical_and_required_when_enabled() -> None:
    settings = _make(
        testing=True,
        email_enabled=True,
        email_allowed_hosts=" IMAP.LarkSuite.com.,smtp.larksuite.com,imap.larksuite.com ",
    )
    assert settings.email_allowed_hosts == frozenset({"imap.larksuite.com", "smtp.larksuite.com"})

    with pytest.raises(ValueError, match="EMAIL_ALLOWED_HOSTS"):
        _make(testing=True, email_enabled=True, email_allowed_hosts="")


def test_x_reconciliation_intervals_accept_zero() -> None:
    settings = _make(
        testing=True,
        x_dm_poll_interval_seconds=0,
        x_webhook_check_interval_seconds=0,
        xchat_poll_interval_seconds=0,
        xchat_subscription_check_interval_seconds=0,
        xchat_recovery_sweep_interval_seconds=0,
        xchat_ready_probe_interval_seconds=0,
        xchat_pending_probe_interval_seconds=0,
    )
    assert settings.x_dm_poll_interval_seconds == 0
    assert settings.x_webhook_check_interval_seconds == 0
    assert settings.xchat_poll_interval_seconds == 0
    assert settings.xchat_subscription_check_interval_seconds == 0
    assert settings.xchat_recovery_sweep_interval_seconds == 0
    assert settings.xchat_ready_probe_interval_seconds == 0
    assert settings.xchat_pending_probe_interval_seconds == 0


def test_x_app_credentials_must_be_configured_as_a_pair() -> None:
    with pytest.raises(ValueError, match="X_API_KEY"):
        _make(testing=True, x_api_key="key-only")

    settings = _make(testing=True, x_api_key="key", x_api_secret="secret")
    assert settings.x_app_credentials == ("key", "secret")


def test_x_feature_flags_are_independent() -> None:
    settings = _make(
        testing=True,
        x_legacy_dm_enabled=False,
        x_activity_enabled=False,
        xchat_enabled=True,
    )
    assert settings.x_legacy_dm_enabled is False
    assert settings.x_activity_enabled is False
    assert settings.xchat_enabled is True
    assert settings.x_integration_enabled is True

    disabled = _make(
        testing=True,
        x_legacy_dm_enabled=False,
        x_activity_enabled=False,
        xchat_enabled=False,
    )
    assert disabled.x_integration_enabled is False


def test_x_oauth_legacy_state_write_is_typed_setting() -> None:
    assert _make(testing=True).x_oauth_legacy_state_write is False
    assert _make(testing=True, x_oauth_legacy_state_write=True).x_oauth_legacy_state_write is True


def test_future_platform_flags_are_independent() -> None:
    settings = _make(
        testing=True,
        facebook_messenger_enabled=False,
        instagram_messaging_enabled=True,
        whatsapp_enabled=False,
        feishu_enabled=True,
        email_enabled=True,
    )
    assert settings.platform_integration_enabled("facebook") is False
    assert settings.platform_integration_enabled("instagram") is True
    assert settings.platform_integration_enabled("whatsapp") is False
    assert settings.platform_integration_enabled("feishu") is True
    assert settings.platform_integration_enabled("email") is True
    assert settings.platform_disabled_code("facebook") == "FACEBOOK_MESSENGER_DISABLED"
    assert settings.platform_disabled_code("instagram") is None
    assert settings.platform_disabled_code("whatsapp") == "WHATSAPP_DISABLED"
    assert settings.platform_disabled_code("feishu") is None
    assert settings.platform_disabled_code("email") is None
    assert _make(testing=True).platform_disabled_code("feishu") == "FEISHU_DISABLED"
    assert _make(testing=True).platform_disabled_code("email") == "EMAIL_DISABLED"
    assert settings.platform_integration_enabled("telegram") is True


def test_local_environment_template_disables_future_platforms() -> None:
    root = Path(__file__).resolve().parents[2]
    assignments = _parse_env_template(root / ".env.example")
    assert assignments["FACEBOOK_MESSENGER_ENABLED"] == "false"
    assert assignments["INSTAGRAM_MESSAGING_ENABLED"] == "false"
    assert assignments["WHATSAPP_ENABLED"] == "false"
    assert assignments["FEISHU_ENABLED"] == "false"
    assert {key: assignments[key] for key in _EMAIL_ENV_DEFAULTS} == _EMAIL_ENV_DEFAULTS
    assert assignments["META_HEALTH_CHECK_INTERVAL_SECONDS"] == "600"
    assert assignments["FEISHU_HEALTH_CHECK_INTERVAL_SECONDS"] == "600"
    assert assignments["SCHEDULER_TICK_SECONDS"] == "0.5"
    assert assignments["SCHEDULER_CORE_INTERVAL_SECONDS"] == "3"
    assert assignments["SCHEDULER_CORE_WARN_AFTER_SECONDS"] == "30"
    assert assignments["SCHEDULER_INSPECTION_WARN_AFTER_SECONDS"] == "300"
    assert assignments["CHATWOOT_RECONCILE_INTERVAL_SECONDS"] == "3"
    assert assignments["XCHAT_RECOVERY_SWEEP_INTERVAL_SECONDS"] == "30"
    assert assignments["XCHAT_READY_PROBE_INTERVAL_SECONDS"] == "21600"
    assert assignments["XCHAT_PENDING_PROBE_INTERVAL_SECONDS"] == "600"


def test_email_documentation_and_migration_head_contract() -> None:
    root = Path(__file__).resolve().parents[2]
    assert _configuration_email_keys(root / "docs/configuration.md") == set(_EMAIL_ENV_DEFAULTS)
    assert _migration_heads(root / "migrations/versions") == {"e9a1c4f7b620"}

    production_migration = (root / "docs/production-migration.md").read_text()
    docs_readme = (root / "docs/README.md").read_text()
    root_readme = (root / "README.md").read_text()
    assert re.search(
        r"current Alembic graph has one head: `e9a1c4f7b620`",
        production_migration,
    )
    assert re.search(
        r"Alembic graph has one current head:\s*`e9a1c4f7b620`",
        docs_readme,
    )
    assert re.search(
        r"current revision 等于唯一 head `e9a1c4f7b620`",
        root_readme,
    )


def test_meta_app_credentials_must_be_configured_as_pairs() -> None:
    with pytest.raises(ValueError, match="FACEBOOK_APP_ID"):
        _make(testing=True, facebook_app_id="facebook-only")
    with pytest.raises(ValueError, match="INSTAGRAM_APP_ID"):
        _make(testing=True, instagram_app_secret="instagram-secret-only")

    settings = _make(
        testing=True,
        facebook_app_id="facebook-app",
        facebook_app_secret="facebook-secret",
        instagram_app_id="instagram-app",
        instagram_app_secret="instagram-secret",
    )
    assert settings.facebook_app_credentials == ("facebook-app", "facebook-secret")
    assert settings.instagram_app_credentials == ("instagram-app", "instagram-secret")


def test_非测试环境_空_admin_allowed_tenants_拒绝() -> None:
    with pytest.raises(ValueError, match="ADMIN_ALLOWED_TENANTS"):
        _make(
            testing=False,
            chatwoot_webhook_secret="real-secret",
            chatwoot_api_token="real-token",
            control_api_key="control-token",
            admin_session_secret="x" * 32,
            admin_username="admin",
            admin_password="password",
            public_base_url="https://reply.example.com",
            admin_allowed_tenants="",
            llm_provider="openai",
            openai_api_key="sk-test",
        )


def test_非测试环境_stub_provider_拒绝() -> None:
    with pytest.raises(ValueError, match="LLM_PROVIDER=stub"):
        _make(
            testing=False,
            chatwoot_webhook_secret="real-secret",
            chatwoot_api_token="real-token",
            control_api_key="control-token",
            admin_session_secret="x" * 32,
            admin_username="admin",
            admin_password="password",
            public_base_url="https://reply.example.com",
            llm_provider="stub",
        )


def test_非测试环境_空_control_api_key_拒绝() -> None:
    with pytest.raises(ValueError, match="CONTROL_API_KEY"):
        _make(
            testing=False,
            chatwoot_webhook_secret="real-secret",
            chatwoot_api_token="real-token",
        )


def test_meta_账号默认必须草稿除非显式开启自动回复() -> None:
    locked = _make(testing=True)
    assert locked.automation_default_allowed("facebook", "BOT_DRAFT_ONLY") is True
    assert locked.automation_default_allowed("facebook", "BOT_ACTIVE") is False
    assert locked.automation_default_allowed("instagram", "BOT_ACTIVE") is False
    # 非 Meta 平台不受这个发布范围约束
    assert locked.automation_default_allowed("telegram", "BOT_ACTIVE") is True
    assert locked.automation_default_allowed("x", "BOT_ACTIVE") is True


def test_显式开启后_meta_账号可用_bot_active() -> None:
    unlocked = _make(testing=True, meta_auto_reply_enabled=True)
    assert unlocked.automation_default_allowed("facebook", "BOT_ACTIVE") is True
    assert unlocked.automation_default_allowed("instagram", "BOT_ACTIVE") is True
    # 开关只解锁 BOT_ACTIVE，草稿始终允许
    assert unlocked.automation_default_allowed("facebook", "BOT_DRAFT_ONLY") is True


@pytest.mark.parametrize(
    ("email_enabled", "email_auto_reply_enabled", "expected"),
    [
        (False, False, False),
        (False, True, False),
        (True, False, False),
        (True, True, True),
    ],
)
def test_automation_default_allowed_requires_both_email_gates_for_bot_active(
    email_enabled, email_auto_reply_enabled, expected
):
    settings = _make(
        testing=True,
        email_enabled=email_enabled,
        email_auto_reply_enabled=email_auto_reply_enabled,
    )
    assert settings.automation_default_allowed("email", "BOT_ACTIVE") is expected
    assert settings.automation_default_allowed("email", "BOT_DRAFT_ONLY") is True
    assert settings.automation_default_allowed("email", "HUMAN_ACTIVE") is True
    assert settings.automation_default_allowed("telegram", "BOT_ACTIVE") is True


def test_x_public_reply_defaults_off() -> None:
    settings = _make(testing=True)
    assert settings.x_public_reply_enabled is False
    # mention 走 Activity webhook，两个开关都开才算启用
    assert settings.x_mention_ingest_enabled is False


def test_x_mention_ingest_requires_both_activity_and_public_reply() -> None:
    assert (
        _make(testing=True, x_activity_enabled=True, x_public_reply_enabled=True)
    ).x_mention_ingest_enabled is True
    assert (
        _make(testing=True, x_activity_enabled=False, x_public_reply_enabled=True)
    ).x_mention_ingest_enabled is False
    assert (
        _make(testing=True, x_activity_enabled=True, x_public_reply_enabled=False)
    ).x_mention_ingest_enabled is False
