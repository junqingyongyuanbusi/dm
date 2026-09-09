import uuid

import pytest

from social_reply.application.account_management.channel_workspace_view import (
    CONNECTION_LABELS,
    ChannelFilters,
    build_channel_account_view,
    render_channel_card,
    render_channel_workspace,
)
from social_reply.application.account_management.ui_i18n import reset_locale, set_locale
from social_reply.infrastructure.database.models import PlatformAccount


def make_account(**overrides: object) -> PlatformAccount:
    values = {
        "id": uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        "tenant_id": "tenant-a",
        "name": "Support Desk",
        "platform": "email",
        "brand_id": "brand-one",
        "external_account_id": "support@example.com",
        "provider_username": None,
        "owner_user_id": None,
        "status": "active",
        "config": {},
        "capability": {},
        "automation_default": "BOT_DRAFT_ONLY",
    }
    return PlatformAccount(**{**values, **overrides})


def account_view(**overrides: object):
    return build_channel_account_view(
        make_account(**overrides), owner_name="Alice", kill_switch_enabled=False
    )


def render_workspace(accounts=(), **overrides: object) -> str:
    options = {
        "accounts": accounts,
        "tenant_id": "tenant-a",
        "filters": ChannelFilters(),
        "brand_id": "brand-one",
        "requested_brand_id": "brand-one",
        "can_connect": True,
        "provider_html": '<form action="/real/oauth/start" method="post"></form>',
        "banner_html": "",
        "maintenance_html": "",
        "jobs_html": "",
        "dialogs_html": "",
        "pending_job_count": 0,
    }
    return render_channel_workspace(**{**options, **overrides})


@pytest.mark.parametrize(
    "query",
    [
        {"q": "x" * 121},
        {"platform": "all' OR 1=1"},
        {"platform": "unknown"},
        {"channel_status": "healthy"},
        {"channel_status": "x" * 21},
    ],
)
def test_invalid_filters_are_rejected(query):
    with pytest.raises(ValueError):
        ChannelFilters.from_query(query)


def test_filter_status_does_not_consume_oauth_callback_or_brand_scope():
    filters = ChannelFilters.from_query(
        {"status": "connected", "brand_id": "other-brand", "q": " DESK "}
    )
    assert filters.channel_status == "all"
    assert filters.query == "DESK"
    assert filters.matches(account_view())


def test_filter_matches_raw_identity_case_insensitively_without_mutation():
    account = account_view(name="Research & Support", external_account_id="help@Example.com")
    assert ChannelFilters(query="& support").matches(account)
    assert ChannelFilters(query="HELP@example").matches(account)
    assert not ChannelFilters(platform="telegram").matches(account)
    assert account.name == "Research & Support"


def test_active_alone_does_not_claim_connection_health():
    account = account_view()
    assert account.connection_status == "pending"
    assert account.automation_label == "仅草稿"
    assert "已连接" not in render_workspace((account,))


@pytest.mark.parametrize(
    ("configuration", "expected"),
    [
        ({"email_health_status": "READY"}, "connected"),
        ({"email_health_status": "ERROR"}, "error"),
        ({"email_health_status": "PROVISIONING"}, "pending"),
        ({"meta_health_status": "READY"}, "pending"),
    ],
)
def test_health_uses_platform_specific_recorded_evidence(configuration, expected):
    assert account_view(config=configuration).connection_status == expected


@pytest.mark.parametrize("platform", ["facebook", "instagram"])
@pytest.mark.parametrize(
    "health_status", ["REAUTH_REQUIRED", "SUBSCRIPTION_MISSING", "APP_SUBSCRIPTION_MISSING"]
)
def test_known_meta_failures_are_actionable_even_when_messaging_is_disabled(
    platform, health_status
):
    account = account_view(
        platform=platform, config={"meta_health_status": health_status}, capability={"dm": False}
    )
    assert account.connection_status == "error"
    assert ChannelFilters(channel_status="error").matches(account)


@pytest.mark.parametrize(
    "health_status", ["CREDENTIAL_INVALID", "BOT_NOT_ACTIVE", "BOT_ID_MISMATCH", "ERROR"]
)
@pytest.mark.parametrize("capability", [{}, {"dm": True}, {"dm": False}])
def test_feishu_saved_failures_take_priority_over_capabilities(health_status, capability):
    account = account_view(
        platform="feishu", config={"feishu_health_status": health_status}, capability=capability
    )
    assert account.connection_status == "error"
    assert account.health_tone == "danger"
    assert ChannelFilters(channel_status="error").matches(account)


@pytest.mark.parametrize(
    ("health_status", "expected"), [("READY", "connected"), ("FUTURE_STATUS", "pending")]
)
def test_feishu_ready_and_unknown_saved_health(health_status, expected):
    account = account_view(platform="feishu", config={"feishu_health_status": health_status})
    assert account.connection_status == expected


def test_default_brand_is_not_added_as_an_explicit_get_scope():
    html = render_workspace(brand_id="default", requested_brand_id="")
    assert 'name="brand_id"' not in html
    assert "brand_id=default" not in html


def test_disabled_and_kill_switch_are_not_automatic_reply_success():
    account = build_channel_account_view(
        make_account(status="DISABLED", automation_default="BOT_ACTIVE"),
        owner_name=None,
        kill_switch_enabled=True,
    )
    assert account.connection_status == "disabled"
    html = render_workspace((account,))
    assert "紧急阻断" in html
    assert "自动回复" in html
    assert "不代表当前可外发" not in html
    assert "组织账号" in html


def test_only_known_strict_boolean_capabilities_are_shown():
    account = account_view(
        capability={"dm": True, "comments": True, "token": "SECRET", "x_chat": "true"},
        config={"password": "SECRET"},
    )
    html = render_workspace((account,))
    assert "私信" in html
    assert "SECRET" not in html
    assert "评论" not in html
    assert "XChat" not in html


@pytest.mark.parametrize("capability_value", ["true", 1, None])
def test_truthy_nonboolean_capability_is_not_presented_as_verified(capability_value):
    account = account_view(capability={"dm": capability_value})
    assert account.capabilities == ()
    assert account.connection_status == "pending"


def test_explicitly_unavailable_messaging_remains_limited_after_ready_probe():
    account = account_view(config={"email_health_status": "READY"}, capability={"dm": False})
    assert account.connection_status == "limited"
    assert ChannelFilters(channel_status="limited").matches(account)


def test_filtered_cards_preserve_full_visible_metrics_and_new_connection_brand():
    first = account_view()
    second = account_view(name="Other account", brand_id="brand-two")
    html = render_workspace((first, second), filters=ChannelFilters(query="Other"))
    assert html.count('class="channel-workspace-metric"') == 4
    assert 'data-metric="visible">2<' in html
    assert "Other account" in html
    assert "Support Desk" not in html
    assert 'name="brand_id" value="brand-one"' in html
    assert 'action="/real/oauth/start"' in html
    assert "brand-two" in html
    assert 'id="add-channels"' in html
    assert '<dialog id="add-channels"' in html
    assert 'data-close-channel-workspace' in html
    assert "channel-workspace-steps" not in html
    assert "channel-workspace-note" not in html
    assert "kill switch" not in html


def test_hostile_names_owners_and_query_are_escaped():
    account = build_channel_account_view(
        make_account(name='<script>alert("name")</script>', brand_id="<brand>"),
        owner_name='<img src=x onerror="alert(1)">',
        kill_switch_enabled=False,
    )
    html = render_workspace((account,))
    assert '<script>alert("name")</script>' not in html
    assert "&lt;script&gt;" in html
    assert "&lt;img" in html
    assert "&lt;brand&gt;" in html
    filtered = render_workspace(filters=ChannelFilters(query='"><img src=x>'))
    assert '"><img src=x>' not in filtered
    assert "没有匹配的账号" in filtered


def test_read_only_view_has_no_connection_forms_or_dialogs():
    html = render_workspace(can_connect=False, dialogs_html='<dialog id="private"></dialog>')
    assert "/real/oauth/start" not in html
    assert 'id="private"' not in html
    assert "连接另一个账号" not in html
    assert "当前没有可见账号" in html


def test_english_workspace_localizes_copy_without_translating_account_data():
    locale_token = set_locale("en")
    try:
        account = build_channel_account_view(
            make_account(name="客服账号", brand_id="品牌原文", capability={"dm": False}),
            owner_name="负责人原文",
            kill_switch_enabled=True,
        )
        html = render_workspace((account,), filters=ChannelFilters(query="客服"))
        card_html = render_channel_card(account, tenant_id="tenant-a")
    finally:
        reset_locale(locale_token)

    for label in (
        "Channel accounts", "Checked", "Draft only", "Emergency blocks",
        "All platforms", "All statuses", "Search account name or username", "Filter",
        "Clear filters", "1 accounts", "Connected accounts", "Add channels",
        "Connect another account", "Connection history", "Recent jobs",
    ):
        assert label in html
    for label in (
        "客服账号", "品牌原文", "负责人原文", "support@example.com", "Email",
        "Messaging limited", "Direct messages", "Not enabled",
        "Emergency block", "Draft only", "Manage",
    ):
        assert label in html
        assert label in card_html
    for label in ("搜索账号", "接入步骤", "新接入归属", "管理", "未启用", "仅生成草稿"):
        assert label not in html
        assert label not in card_html


@pytest.mark.parametrize("can_connect", [True, False])
def test_english_empty_states_and_owner_fallbacks(can_connect):
    locale_token = set_locale("en")
    try:
        empty_html = render_workspace(can_connect=can_connect)
        filtered_html = render_workspace(filters=ChannelFilters(query="missing"))
        account = build_channel_account_view(
            make_account(external_account_id="", automation_default="UNKNOWN"),
            owner_name=None,
            kill_switch_enabled=False,
        )
        card_html = render_channel_card(account, tenant_id="tenant-a")
    finally:
        reset_locale(locale_token)
    assert "No visible accounts yet" in empty_html
    assert "No matching accounts" in filtered_html
    assert (
        "connect your first account" if can_connect else "contact an administrator"
    ) in empty_html
    assert "Organization account" in card_html
    assert "No identity" in card_html
    assert "Not checked" in card_html
    assert "当前没有可见账号" in render_workspace()


def test_legacy_connection_label_mapping_tracks_locale_without_mutation():
    chinese_labels = dict(CONNECTION_LABELS)
    locale_token = set_locale("en")
    try:
        assert dict(CONNECTION_LABELS) == {
            "connected": "Checked",
            "limited": "Messaging limited",
            "error": "Connection error",
            "pending": "Not checked",
            "disabled": "Disabled",
        }
        with pytest.raises(KeyError):
            CONNECTION_LABELS["unknown"]
    finally:
        reset_locale(locale_token)
    assert dict(CONNECTION_LABELS) == chinese_labels
    assert CONNECTION_LABELS["connected"] == "检测通过"
