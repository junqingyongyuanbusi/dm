import html
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from social_reply.application.account_management.auth import Principal


@dataclass(frozen=True)
class NavigationItem:
    key: str
    href: str
    label: str
    count: int | None = None


@dataclass(frozen=True)
class NavigationGroup:
    label: str
    items: tuple[NavigationItem, ...]


_STATUS_PRESENTATION: dict[str, tuple[str, str]] = {
    "active": ("success", "运行中"),
    "BOT_ACTIVE": ("success", "自动回复"),
    "BOT_DRAFT_ONLY": ("info", "仅生成草稿"),
    "draft_only": ("info", "仅生成草稿"),
    "mixed": ("warning", "混合模式"),
    "published": ("success", "已发布"),
    "PENDING": ("neutral", "等待处理"),
    "PROCESSING": ("info", "处理中"),
    "COMPLETED": ("success", "已完成"),
    "SENT": ("success", "已发送"),
    "FAILED": ("danger", "失败"),
    "NEEDS_REVIEW": ("warning", "需要人工核实"),
    "WAITING": ("warning", "等待认领"),
    "CLAIMED": ("info", "处理中"),
    "RESOLVED": ("success", "已解决"),
    "CANCELLED": ("neutral", "已取消"),
    "HANDOFF_PENDING": ("warning", "等待人工"),
    "HUMAN_ACTIVE": ("info", "人工接管中"),
    "BOT_COOLDOWN": ("warning", "自动化冷却中"),
    "CLOSED": ("neutral", "已关闭"),
    "auto_reply": ("success", "自动回复"),
    "draft": ("info", "草稿审核"),
    "handoff": ("warning", "转人工"),
    "ignore": ("neutral", "忽略"),
    "healthy": ("success", "正常"),
    "degraded": ("warning", "需要处理"),
    "unconfigured": ("neutral", "尚未配置"),
}


def escape(value: object) -> str:
    return html.escape(str(value if value is not None else ""))


def format_datetime(value: datetime | None, *, include_year: bool = False) -> str:
    if value is None:
        return "—"
    localized = value.astimezone(UTC) if value.tzinfo else value
    date_format = "%Y-%m-%d %H:%M" if include_year else "%m-%d %H:%M"
    return localized.strftime(date_format)


def format_age(value: datetime | None, *, now: datetime | None = None) -> str:
    if value is None:
        return "—"
    current_time = now or datetime.now(UTC)
    comparable_value = value if value.tzinfo else value.replace(tzinfo=UTC)
    elapsed_seconds = max(0, int((current_time - comparable_value).total_seconds()))
    if elapsed_seconds < 60:
        return "刚刚"
    if elapsed_seconds < 3600:
        return f"{elapsed_seconds // 60} 分钟"
    if elapsed_seconds < 86400:
        return f"{elapsed_seconds // 3600} 小时"
    return f"{elapsed_seconds // 86400} 天"


def status_badge(status: str | None, *, label: str | None = None) -> str:
    normalized_status = status or "unknown"
    tone, default_label = _STATUS_PRESENTATION.get(
        normalized_status,
        ("neutral", normalized_status.replace("_", " ").title()),
    )
    visible_label = label or default_label
    return (
        f'<span class="saas-status {tone}" title="{escape(normalized_status)}">'
        f"{escape(visible_label)}</span>"
    )


def primary_action(href: str, label: str) -> str:
    return f'<a class="saas-button primary" href="{escape(href)}">{escape(label)}</a>'


def secondary_action(href: str, label: str, *, small: bool = False) -> str:
    size_class = " small" if small else ""
    return f'<a class="saas-button{size_class}" href="{escape(href)}">{escape(label)}</a>'


def empty_state(title: str, description: str, *, action_html: str = "") -> str:
    return (
        '<section class="saas-card saas-empty">'
        f"<h2>{escape(title)}</h2><p>{escape(description)}</p>{action_html}</section>"
    )


def safe_json_details(value: object, *, summary: str = "查看结构化详情") -> str:
    serialized = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    if len(serialized) > 12000:
        serialized = f"{serialized[:12000]}\n... 已截断"
    return (
        '<details class="saas-code-details">'
        f"<summary>{escape(summary)}</summary><pre>{escape(serialized)}</pre></details>"
    )


def metric_card(value: object, label: str, *, detail: str = "") -> str:
    detail_html = f'<div class="saas-muted">{escape(detail)}</div>' if detail else ""
    return (
        '<section class="saas-card saas-metric">'
        f'<span class="saas-metric-value">{escape(value)}</span>'
        f'<span class="saas-metric-label">{escape(label)}</span>{detail_html}</section>'
    )


def definition_list(items: Iterable[tuple[str, object]]) -> str:
    rows = "".join(
        f"<dt>{escape(label)}</dt><dd>{escape(value) if value not in (None, '') else '—'}</dd>"
        for label, value in items
    )
    return f'<dl class="saas-definition-list">{rows}</dl>'


def tabs(items: Sequence[tuple[str, str, str]], active_key: str) -> str:
    links = "".join(
        f'<a class="saas-tab{" active" if key == active_key else ""}" '
        f'href="{escape(href)}">{escape(label)}</a>'
        for key, href, label in items
    )
    return f'<nav class="saas-tabs" aria-label="页面分区">{links}</nav>'


def render_saas_page(
    *,
    principal: Principal,
    title: str,
    description: str,
    body: str,
    active_navigation: str,
    tenant_id: str | None,
    primary_action_html: str = "",
    breadcrumbs: Sequence[tuple[str, str | None]] = (),
    inbox_count: int = 0,
    system_admin: bool = False,
) -> str:
    page_title = f"{escape(title)} · Reply Core"
    navigation_groups = (
        _system_navigation_groups()
        if system_admin
        else _tenant_navigation_groups(tenant_id or "", inbox_count)
    )
    navigation_html = _render_navigation(navigation_groups, active_navigation)
    breadcrumb_html = _render_breadcrumbs(breadcrumbs)
    context_html = _render_context(tenant_id=tenant_id, system_admin=system_admin)
    admin_banner = (
        '<div class="saas-admin-banner"><strong>SYSTEM ADMIN</strong>'
        "<span>跨租户特权区域</span></div>"
        if system_admin
        else ""
    )
    footer_link = (
        '<a class="saas-nav-item" href="/app">进入租户工作区 <span>→</span></a>'
        if system_admin
        else (
            '<a class="saas-nav-item" href="/admin/system/overview">'
            "打开系统后台 <span>→</span></a>"
            if principal.is_superadmin
            else '<a class="saas-nav-item" href="/admin">兼容后台 <span>→</span></a>'
        )
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <title>{page_title}</title>
  <link rel="stylesheet" href="/static/saas.css">
</head>
<body>
  <a class="saas-button" href="#main-content" style="position:absolute;left:-9999px">跳到主内容</a>
  <header class="saas-topbar">
    <a class="saas-brand" href="{escape('/admin/system/overview' if system_admin else '/app')}">
      <span class="saas-brand-mark">RC</span><span>Reply Core</span>
    </a>
    <div class="saas-context">{context_html}
      <input class="saas-global-search" type="search" placeholder="搜索对话、事件或资源 ID" disabled
    </div>
    <nav class="saas-user-actions" aria-label="用户操作">
      <a href="/help">帮助</a><span class="saas-user-name">{escape(principal.username)}</span>
      <a href="/admin/logout">退出</a>
    </nav>
  </header>
  <aside class="saas-sidebar">{admin_banner}{navigation_html}
    <div class="saas-sidebar-footer">{footer_link}</div>
  </aside>
  <main class="saas-main" id="main-content"><div class="saas-content">
    {breadcrumb_html}
    <header class="saas-page-header"><div><h1>{escape(title)}</h1><p>{escape(description)}</p></div>
      {primary_action_html}
    </header>
    {body}
  </div></main>
</body>
</html>"""


def _tenant_navigation_groups(tenant_id: str, inbox_count: int) -> tuple[NavigationGroup, ...]:
    root = f"/app/t/{tenant_id}"
    return (
        NavigationGroup(
            "工作区",
            (
                NavigationItem("home", root, "首页"),
                NavigationItem("agents", f"{root}/agents", "Agents"),
                NavigationItem("knowledge", f"{root}/knowledge", "文档与知识"),
                NavigationItem("inbox", f"{root}/inbox", "收件箱", inbox_count or None),
            ),
        ),
        NavigationGroup(
            "可观测性",
            (
                NavigationItem("audit", f"{root}/audit", "审计中心"),
                NavigationItem("journeys", f"{root}/journeys", "Processing Journey"),
            ),
        ),
        NavigationGroup(
            "设置",
            (
                NavigationItem("settings", f"{root}/settings", "工作区设置"),
            ),
        ),
    )


def _system_navigation_groups() -> tuple[NavigationGroup, ...]:
    return (
        NavigationGroup(
            "系统",
            (
                NavigationItem("system-overview", "/admin/system/overview", "系统总览"),
                NavigationItem("system-health", "/admin/system/health", "系统健康"),
                NavigationItem("system-safety", "/admin/system/safety", "安全控制"),
            ),
        ),
        NavigationGroup(
            "访问与审计",
            (
                NavigationItem("system-users", "/admin/system/users", "用户与访问"),
                NavigationItem("system-audit", "/admin/system/audit", "跨租户审计"),
            ),
        ),
    )


def _render_navigation(groups: Sequence[NavigationGroup], active_key: str) -> str:
    rendered_groups: list[str] = []
    for group in groups:
        rendered_item_rows: list[str] = []
        for item in group.items:
            count_html = (
                f'<span class="saas-nav-count">{item.count}</span>' if item.count else ""
            )
            active_class = " active" if item.key == active_key else ""
            rendered_item_rows.append(
                f'<a class="saas-nav-item{active_class}" href="{escape(item.href)}">'
                f"<span>{escape(item.label)}</span>{count_html}</a>"
            )
        rendered_items = "".join(rendered_item_rows)
        rendered_groups.append(
            '<section class="saas-nav-group">'
            f'<span class="saas-nav-label">{escape(group.label)}</span>{rendered_items}</section>'
        )
    return "".join(rendered_groups)


def _render_breadcrumbs(items: Sequence[tuple[str, str | None]]) -> str:
    if not items:
        return ""
    rendered_items: list[str] = []
    for label, href in items:
        rendered_items.append(
            f'<a href="{escape(href)}">{escape(label)}</a>' if href else escape(label)
        )
    return f'<nav class="saas-breadcrumbs">{" / ".join(rendered_items)}</nav>'


def _render_context(*, tenant_id: str | None, system_admin: bool) -> str:
    if system_admin:
        return '<span class="saas-context-label">系统控制面</span>'
    return (
        '<span class="saas-context-label">Tenant</span>'
        f'<a class="saas-tenant-switcher" href="/app">{escape(tenant_id or "选择 Tenant")} '
        "<span>⌄</span></a>"
    )
