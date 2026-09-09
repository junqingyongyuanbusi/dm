from datetime import UTC, date, datetime, timedelta, timezone
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest

from social_reply.application.account_management.home_dashboard_view import render_home_dashboard
from social_reply.application.account_management.ui_i18n import reset_locale, set_locale


def dashboard_data(**overrides):
    return SimpleNamespace(**{
        "pending_count": 0, "ai_count": 0, "account_count": 0,
        "enabled_account_count": 0, "resolved_count": 0, "trend": (), "channels": (),
        "attention": (), "published_knowledge_count": 0, "draft_knowledge_count": 0,
        "pending_draft_count": 0, **overrides,
    })


def render(data=None, *, locale="zh-CN", can_manage=True, now=None):
    token = set_locale(locale)
    try:
        return render_home_dashboard(
            data or dashboard_data(), "tenant-a", now or datetime(2026, 9, 8, tzinfo=UTC),
            can_manage=can_manage,
        )
    finally:
        reset_locale(token)


def chart_from_html(html):
    start = html.index('<svg class="home-dashboard-chart"')
    end = html.index("</svg>", start) + len("</svg>")
    return ElementTree.fromstring(html[start:end])


def test_empty_dashboard_keeps_four_metrics_and_chart():
    html = render()
    assert "让每一次对话，都有回应。" in html
    assert html.count('class="home-dashboard-metric"') == 4
    assert html.count('class="home-dashboard-metric-value">0<') == 4
    assert "暂无接待记录" in html
    assert "例行检查" in html
    assert "检测到问题" not in html
    assert "<details" in html and "<table" in html
    chart = chart_from_html(html)
    assert chart.attrib["viewBox"] == "0 0 700 230"
    assert chart.attrib["aria-labelledby"] == "home-trend-title home-trend-description"


@pytest.mark.parametrize("point_count", [1, 7])
@pytest.mark.parametrize("received", [0, 1, 8, 101, 1_000_000])
def test_chart_geometry_is_finite_bounded_and_keyboard_accessible(point_count, received):
    trend = tuple(
        SimpleNamespace(day=date(2026, 9, 2) + timedelta(days=index),
                        received=received, ai=received // 2)
        for index in range(point_count)
    )
    html = render(dashboard_data(trend=trend))
    chart = chart_from_html(html)
    assert "nan" not in ElementTree.tostring(chart).decode().lower()
    circles = chart.findall(".//circle")
    assert len(circles) == point_count * 2
    for circle in circles:
        assert 0 <= float(circle.attrib["cx"]) <= 700
        assert 0 <= float(circle.attrib["cy"]) <= 230
        assert circle.attrib["tabindex"] == "0"
        assert circle.find("title") is not None
    assert len(chart.findall('.//line[@class="home-dashboard-gridline"]')) == 5
    assert html.count('scope="row"') == point_count


def test_attention_escapes_content_and_rejects_non_tenant_links():
    records = tuple(
        SimpleNamespace(title='<img src=x onerror="alert(1)">', description="<script>x</script>",
                        href=href, kind='danger" onclick="alert(1)')
        for href in ("javascript:alert(1)", "//evil.example", "//[", "https://[invalid",
                     "/app/t/tenant-b/inbox",
                     "/app/t/tenant-a/inbox?filter=pending&limit=5")
    )
    html = render(dashboard_data(attention=records))
    assert "<img" not in html and "<script" not in html
    assert "&lt;script&gt;" in html
    assert 'href="javascript:' not in html
    assert 'href="//evil' not in html
    assert 'href="/app/t/tenant-b/' not in html
    assert 'href="/app/t/tenant-a/inbox?filter=pending&amp;limit=5"' in html


def test_channel_bars_measure_received_not_accounts():
    data = dashboard_data(channels=(
        SimpleNamespace(platform="telegram", accounts=100, received=2),
        SimpleNamespace(platform="whatsapp", accounts=1, received=8),
    ))
    html = render(data, locale="en")
    assert 'style="--app-channel-share: 25.0%"' in html
    assert "100 accounts" in html
    assert "8 conversations" in html


def test_quality_actions_are_role_aware_and_based_on_real_counts():
    data = dashboard_data(pending_draft_count=5, draft_knowledge_count=7)
    admin_html = render(data, locale="en")
    member_html = render(data, locale="en", can_manage=False)
    assert "5 reply drafts" in admin_html
    assert "7 knowledge drafts" in admin_html
    assert "/app/t/tenant-a/inbox?queue=drafts" in admin_html
    assert "/playground" in admin_html
    assert "queue=drafts" not in member_html
    assert "7 knowledge drafts" not in member_html
    assert "/playground" not in member_html
    assert "View financial knowledge" in member_html


def test_locale_and_utc_date_are_preserved():
    now = datetime(2026, 9, 8, 2, tzinfo=timezone(timedelta(hours=8)))
    html = render(locale="en", now=now)
    assert "Every conversation deserves a reply." in html
    assert "2026-09-07" in html and "UTC" in html
    assert "Enabled accounts" in html
    assert "healthy" not in html.lower()
