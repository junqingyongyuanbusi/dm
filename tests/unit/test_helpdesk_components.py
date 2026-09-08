from social_reply.application.account_management.templating import render_template, trusted_html


def test_section_header_escapes_content_and_renders_trusted_actions() -> None:
    html = render_template(
        "components/section_header.html",
        eyebrow="<unsafe-eyebrow>",
        title="<unsafe-title>",
        description="<unsafe-description>",
        actions_html='<a href="/safe">Open</a>',
    )

    assert "<unsafe-eyebrow>" not in html
    assert "<unsafe-title>" not in html
    assert "<unsafe-description>" not in html
    assert "&lt;unsafe-title&gt;" in html
    assert '<a href="/safe">Open</a>' not in html


def test_placeholder_panel_marks_unavailable_work_as_read_only() -> None:
    html = render_template(
        "components/placeholder_panel.html",
        title="趋势数据",
        description="数据即将提供",
        availability_label="功能待开发",
    )

    assert 'data-placeholder-panel="true"' in html
    assert "趋势数据" in html
    assert "数据即将提供" in html
    assert "功能待开发" in html
    assert "<button" not in html


def test_home_template_prioritizes_work_over_lifecycle_decoration() -> None:
    html = render_template(
        "tenant/home.html",
        next_action_html=trusted_html('<section class="saas-next-action">Next work</section>'),
        queue_summary_html=trusted_html(
            '<section class="saas-status-summary">Queue summary</section>'
        ),
        channel_alerts=(),
        attention_eyebrow="处理队列",
        attention_title="需要处理",
        attention_description="优先处理最紧急的客户工作。",
        attention_cards_html=trusted_html('<div class="saas-grid three">Queue cards</div>'),
        automation_eyebrow="自动化",
        automation_title="Agent 与渠道就绪度",
        automation_description="查看真实配置状态和阻塞项。",
        view_agents_action_html=trusted_html('<a href="/agents">Agents</a>'),
        readiness_rows_html=trusted_html('<ul class="saas-progress-list"><li>Ready</li></ul>'),
        automation_metrics_html=trusted_html(
            '<div class="saas-grid three">Automation metrics</div>'
        ),
        today_eyebrow="今日",
        today_title="今日概览",
        today_description="仅显示已有数据。",
        message_count=5,
        message_count_label="今日消息",
        today_placeholder_html=trusted_html(
            '<section data-placeholder-panel="true">Soon</section>'
        ),
        recent_activity_eyebrow="记录",
        recent_activity_title="最近活动",
        recent_activity_description="查看近期重要变更。",
        view_all_action_html=trusted_html('<a href="/audit">All activity</a>'),
        business_activities=(),
        recent_activity_empty_html=trusted_html('<section>Empty</section>'),
        time_label="时间",
        event_label="事件",
    )

    assert "saas-next-action" in html
    assert "saas-status-summary" in html
    assert 'data-placeholder-panel="true"' not in html
    assert "saas-home-agents" not in html
    assert "saas-home-stream" not in html
    assert ">5</span>" in html
    assert "saas-lifecycle" not in html
