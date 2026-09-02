import html
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from social_reply.application.account_management.auth import Principal
from social_reply.application.account_management.templating import (
    render_template,
    trusted_html,
)
from social_reply.application.account_management.ui_i18n import (
    get_locale,
    locale_switch_url,
    translate,
)


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


PageSurface = Literal["tenant", "admin", "system", "auth"]
_STATIC_ASSET_VERSION = "20260902-agent-control-plane-1"


_STATUS_PRESENTATION: dict[str, tuple[str, str]] = {
    "active": ("success", "status.active"),
    "BOT_ACTIVE": ("success", "status.bot_active"),
    "BOT_DRAFT_ONLY": ("info", "status.bot_draft_only"),
    "draft_only": ("info", "status.bot_draft_only"),
    "mixed": ("warning", "status.mixed"),
    "published": ("success", "status.published"),
    "PENDING": ("neutral", "status.pending"),
    "PROCESSING": ("info", "status.processing"),
    "COMPLETED": ("success", "status.completed"),
    "SENT": ("success", "status.sent"),
    "FAILED": ("danger", "status.failed"),
    "NEEDS_REVIEW": ("warning", "status.needs_review"),
    "WAITING": ("warning", "status.waiting"),
    "CLAIMED": ("info", "status.claimed"),
    "RESOLVED": ("success", "status.resolved"),
    "CANCELLED": ("neutral", "status.cancelled"),
    "HANDOFF_PENDING": ("warning", "status.handoff_pending"),
    "HUMAN_ACTIVE": ("info", "status.human_active"),
    "BOT_COOLDOWN": ("warning", "status.bot_cooldown"),
    "CLOSED": ("neutral", "status.closed"),
    "auto_reply": ("success", "status.auto_reply"),
    "draft": ("info", "status.draft_review"),
    "handoff": ("warning", "status.handoff"),
    "ignore": ("neutral", "status.ignore"),
    "healthy": ("success", "status.healthy"),
    "degraded": ("warning", "status.degraded"),
    "unconfigured": ("neutral", "status.unconfigured"),
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
        return translate("common.just_now")
    if elapsed_seconds < 3600:
        return translate("common.minutes_duration", count=elapsed_seconds // 60)
    if elapsed_seconds < 86400:
        return translate("common.hours_duration", count=elapsed_seconds // 3600)
    return translate("common.days_duration", count=elapsed_seconds // 86400)


def status_badge(status: str | None, *, label: str | None = None) -> str:
    normalized_status = status or "unknown"
    tone, translation_key = _STATUS_PRESENTATION.get(normalized_status, ("neutral", ""))
    default_label = (
        translate(translation_key)
        if translation_key
        else normalized_status.replace("_", " ").title()
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


def safe_json_details(value: object, *, summary: str | None = None) -> str:
    serialized = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    if len(serialized) > 12000:
        serialized = f"{serialized[:12000]}\n{translate('common.truncated')}"
    visible_summary = summary or translate("common.structured_details")
    return (
        '<details class="saas-code-details">'
        f"<summary>{escape(visible_summary)}</summary><pre>{escape(serialized)}</pre></details>"
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
        f'href="{escape(href)}"'
        f'{" aria-current=\'page\'" if key == active_key else ""}>{escape(label)}</a>'
        for key, href, label in items
    )
    return (
        f'<nav class="saas-tabs" aria-label="{escape(translate("shell.page_sections"))}">'
        f"{links}</nav>"
    )


def render_shared_page(
    *,
    title: str,
    body: str,
    surface: PageSurface,
    navigation_groups: Sequence[NavigationGroup] = (),
    active_navigation: str = "",
    principal: Principal | None = None,
    tenant_id: str | None = None,
    description: str = "",
    primary_action_html: str = "",
    breadcrumbs: Sequence[tuple[str, str | None]] = (),
    refresh_seconds: int = 0,
    legacy_content: bool = False,
    footer_link_html: str = "",
    workbench: bool = False,
) -> str:
    """Render the shared tenant, admin, system, or authentication page shell."""
    has_sidebar = surface != "auth" and bool(navigation_groups)
    navigation_html = _render_navigation(
        navigation_groups,
        active_navigation,
        legacy_content=legacy_content,
    )
    header_html = _render_header(
        surface=surface,
        principal=principal,
        tenant_id=tenant_id,
        has_sidebar=has_sidebar,
    )
    sidebar_html = _render_sidebar(
        navigation_html=navigation_html,
        surface=surface,
        tenant_id=tenant_id,
        footer_link_html=footer_link_html,
        legacy_content=legacy_content,
    )
    main_html = _render_main_content(
        title=title,
        description=description,
        body=body,
        primary_action_html=primary_action_html,
        breadcrumbs=breadcrumbs,
        has_sidebar=has_sidebar,
        legacy_content=legacy_content,
        workbench=workbench,
    )
    layout_mode = "workbench" if workbench else "page"
    return render_template(
        "shared/page.html",
        locale=get_locale(),
        title=title,
        product_name=translate("shell.product_name"),
        asset_version=_STATIC_ASSET_VERSION,
        refresh_seconds=refresh_seconds,
        surface=surface,
        layout_mode=layout_mode,
        close_navigation_label=translate("shell.close_navigation"),
        skip_to_content_label=translate("shell.skip_to_content"),
        header_html=trusted_html(header_html),
        page_layout_html=trusted_html(
            _render_page_layout(sidebar_html, main_html, has_sidebar, legacy_content)
        ),
    )


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
    workbench: bool = False,
) -> str:
    navigation_groups = (
        _system_navigation_groups()
        if system_admin
        else _tenant_navigation_groups(principal, tenant_id or "", inbox_count)
    )
    footer_link_html = _render_cross_surface_link(principal, system_admin=system_admin)
    return render_shared_page(
        title=title,
        description=description,
        body=body,
        active_navigation=active_navigation,
        navigation_groups=navigation_groups,
        principal=principal,
        tenant_id=tenant_id,
        primary_action_html=primary_action_html,
        breadcrumbs=breadcrumbs,
        surface="system" if system_admin else "tenant",
        footer_link_html=footer_link_html,
        workbench=workbench,
    )


def _tenant_navigation_groups(
    principal: Principal,
    tenant_id: str,
    inbox_count: int,
) -> tuple[NavigationGroup, ...]:
    if not tenant_id:
        return ()
    root = f"/app/t/{tenant_id}"
    if not principal.is_admin:
        return (
            NavigationGroup(
                translate("nav.group.work"),
                (
                    NavigationItem("home", root, translate("nav.home")),
                    NavigationItem(
                        "inbox",
                        f"{root}/inbox",
                        translate("nav.inbox"),
                        inbox_count or None,
                    ),
                    NavigationItem(
                        "conversations", f"{root}/conversations", translate("nav.conversations")
                    ),
                    NavigationItem("agents", f"{root}/agents", translate("nav.agents")),
                    NavigationItem(
                        "knowledge-query",
                        f"{root}/knowledge-query",
                        translate("nav.knowledge_query"),
                    ),
                    NavigationItem("activity", f"{root}/activity", translate("nav.my_activity")),
                ),
            ),
            NavigationGroup(
                translate("nav.group.configuration"),
                (NavigationItem("channels", f"{root}/channels", translate("nav.channels")),),
            ),
            NavigationGroup(
                translate("nav.group.settings"),
                (NavigationItem("profile", f"{root}/profile", translate("nav.profile")),),
            ),
        )
    return (
        NavigationGroup(
            translate("nav.group.work"),
            (
                NavigationItem("home", root, translate("nav.home")),
                NavigationItem(
                    "inbox", f"{root}/inbox", translate("nav.inbox"), inbox_count or None
                ),
                NavigationItem(
                    "conversations", f"{root}/conversations", translate("nav.conversations")
                ),
            ),
        ),
        NavigationGroup(
            translate("nav.group.configuration"),
            (
                NavigationItem("agents", f"{root}/agents", translate("nav.agents")),
                NavigationItem(
                    "knowledge",
                    f"{root}/knowledge",
                    translate("nav.documents_knowledge"),
                ),
            ),
        ),
        NavigationGroup(
            translate("nav.group.observability"),
            (
                NavigationItem("audit", f"{root}/audit", translate("nav.audit_center")),
                NavigationItem(
                    "journeys", f"{root}/journeys", translate("nav.processing_journey")
                ),
            ),
        ),
        NavigationGroup(
            translate("nav.group.settings"),
            (
                NavigationItem("channels", f"{root}/channels", translate("nav.channels")),
                NavigationItem("profile", f"{root}/profile", translate("nav.profile")),
                NavigationItem(
                    "settings", f"{root}/settings", translate("nav.workspace_settings")
                ),
            ),
        ),
    )


def _system_navigation_groups() -> tuple[NavigationGroup, ...]:
    return (
        NavigationGroup(
            translate("nav.group.system"),
            (
                NavigationItem(
                    "system-overview", "/admin/system/overview", translate("nav.system_overview")
                ),
                NavigationItem(
                    "system-users", "/admin/system/users", translate("nav.users_access")
                ),
                NavigationItem(
                    "system-safety", "/admin/system/safety", translate("nav.security_controls")
                ),
                NavigationItem(
                    "system-audit", "/admin/system/audit", translate("nav.cross_tenant_audit")
                ),
            ),
        ),
    )


_NAVIGATION_ICON_PATHS: dict[str, str] = {
    "home": (
        '<path d="M3.5 11.5 12 4l8.5 7.5"/>'
        '<path d="M5.5 10.5V20h13v-9.5M9.5 20v-6h5v6"/>'
    ),
    "inbox": (
        '<path d="M4 5h16l-1.5 14h-13L4 5Z"/>'
        '<path d="M5 13h4l1.5 2h3L15 13h4"/>'
    ),
    "conversations": (
        '<path d="M5 6.5h14v10H9l-4 3v-13Z"/>'
        '<path d="M8 10h8M8 13h5"/>'
    ),
    "agents": (
        '<rect x="5" y="7" width="14" height="12" rx="3"/>'
        '<path d="M12 3v4M8.5 12h.01M15.5 12h.01M9 16h6"/>'
    ),
    "knowledge": (
        '<path d="M4.5 5.5A3.5 3.5 0 0 1 8 4h4v15H8a3.5 3.5 0 0 0-3.5 1V5.5Z"/>'
        '<path d="M19.5 5.5A3.5 3.5 0 0 0 16 4h-4v15h4a3.5 3.5 0 0 1 3.5 1V5.5Z"/>'
    ),
    "audit": (
        '<path d="M8 4h8M9 3h6v3H9zM6 5h12v16H6z"/>'
        '<path d="m9 14 2 2 4-5"/>'
    ),
    "settings": (
        '<path d="M4 7h10M18 7h2M4 17h2M10 17h10"/>'
        '<circle cx="16" cy="7" r="2"/><circle cx="8" cy="17" r="2"/>'
    ),
    "channels": (
        '<path d="M7 8.5a5 5 0 0 0 0 7M4.5 6a8.5 8.5 0 0 0 0 12"/>'
        '<path d="M17 8.5a5 5 0 0 1 0 7M19.5 6a8.5 8.5 0 0 1 0 12"/>'
        '<circle cx="12" cy="12" r="2"/>'
    ),
    "users": (
        '<circle cx="9" cy="8" r="3"/>'
        '<path d="M3.5 20v-2.5A4.5 4.5 0 0 1 8 13h2a4.5 4.5 0 0 1 4.5 4.5V20"/>'
        '<path d="M15.5 5.5a3 3 0 0 1 0 5.5M16 14a4.5 4.5 0 0 1 4.5 4.5V20"/>'
    ),
    "health": '<path d="M3 12h4l2-5 4 10 2-5h6"/>',
    "safety": (
        '<path d="M12 3 5 6v5c0 4.7 2.8 8 7 10 4.2-2 7-5.3 7-10V6l-7-3Z"/>'
        '<path d="m9 12 2 2 4-5"/>'
    ),
    "activity": '<path d="M4 12h3l2-5 4 10 2-5h5"/><path d="M4 4v16h16"/>',
    "default": '<rect x="4" y="4" width="16" height="16" rx="3"/>',
}

_NAVIGATION_ICON_ALIASES: dict[str, str] = {
    "overview": "home",
    "system-overview": "home",
    "knowledge-query": "knowledge",
    "reply-prompt": "knowledge",
    "system-audit": "audit",
    "journeys": "activity",
    "profile": "users",
    "accounts": "channels",
    "handoff": "conversations",
    "system-health": "health",
    "system-safety": "safety",
    "system-users": "users",
}


def navigation_icon(item_key: str) -> str:
    """Render the shared outline icon for a navigation key."""
    canonical_key = _NAVIGATION_ICON_ALIASES.get(item_key, item_key)
    icon_paths = _NAVIGATION_ICON_PATHS.get(
        canonical_key,
        _NAVIGATION_ICON_PATHS["default"],
    )
    return (
        '<svg class="saas-nav-icon" aria-hidden="true" focusable="false" '
        'viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" '
        f'stroke-linecap="round" stroke-linejoin="round">{icon_paths}</svg>'
    )


def _render_navigation(
    groups: Sequence[NavigationGroup],
    active_key: str,
    *,
    legacy_content: bool,
) -> str:
    group_class = "saas-nav-group nav-group" if legacy_content else "saas-nav-group"
    label_class = "saas-nav-label nav-heading" if legacy_content else "saas-nav-label"
    item_class = "saas-nav-item nav-item" if legacy_content else "saas-nav-item"
    rendered_groups: list[str] = []
    for group in groups:
        rendered_item_rows: list[str] = []
        for item in group.items:
            count_html = (
                f'<span class="saas-nav-count">{item.count}</span>'
                if item.count is not None
                else ""
            )
            is_active = item.key == active_key
            active_class = " active" if is_active else ""
            aria_current = " aria-current='page'" if is_active else ""
            rendered_item_rows.append(
                f'<a class="{item_class}{active_class}" href="{escape(item.href)}"'
                f'{aria_current}><span class="saas-nav-item-content">'
                f'{navigation_icon(item.key)}<span class="saas-nav-text">'
                f"{escape(item.label)}</span></span>{count_html}</a>"
            )
        rendered_groups.append(
            f'<section class="{group_class}">'
            f'<span class="{label_class}">{escape(group.label)}</span>'
            f"{''.join(rendered_item_rows)}</section>"
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


def _render_header(
    *,
    surface: PageSurface,
    principal: Principal | None,
    tenant_id: str | None,
    has_sidebar: bool,
) -> str:
    context_html = _render_context(surface=surface, tenant_id=tenant_id)
    toggle_html = (
        '<button type="button" data-sidebar-toggle aria-controls="primary-navigation" '
        f'aria-label="{escape(translate("shell.open_navigation"))}">'
        '<svg aria-hidden="true" focusable="false" viewBox="0 0 24 24" fill="none" '
        'stroke="currentColor" stroke-width="1.8" stroke-linecap="round">'
        '<path d="M4 7h16M4 12h16M4 17h16"/></svg></button>'
        if has_sidebar
        else ""
    )
    user_actions_html = _render_user_actions(principal, tenant_id=tenant_id, surface=surface)
    if has_sidebar:
        return f"""<header class="saas-topbar saas-workspace-topbar" data-workspace-topbar>
    <div class="saas-context">{toggle_html}{context_html}</div>
    <nav class="saas-user-actions" aria-label="{escape(translate('shell.user_actions'))}">
      {user_actions_html}
    </nav>
  </header>"""
    return f"""<header class="saas-card-header saas-auth-header">
    {_render_brand(surface)}
    <div class="saas-context">{context_html}</div>
    <nav class="saas-user-actions" aria-label="{escape(translate('shell.user_actions'))}">
      {user_actions_html}
    </nav>
  </header>"""


def _render_brand(surface: PageSurface) -> str:
    brand_href = {
        "tenant": "/app",
        "admin": "/admin",
        "system": "/admin/system/overview",
        "auth": "/auth/login",
    }[surface]
    return (
        f'<a class="saas-brand" href="{escape(brand_href)}">'
        '<span class="saas-brand-mark">RC</span>'
        f'<span>{escape(translate("shell.product_name"))}</span></a>'
    )


def _render_context(*, surface: PageSurface, tenant_id: str | None) -> str:
    if surface == "tenant":
        return f'<span class="saas-context-label">{escape(translate("shell.workspace"))}</span>'
    context_key = {
        "admin": "shell.admin_control_plane",
        "system": "shell.system_control_plane",
        "auth": "shell.secure_access",
    }[surface]
    return f'<span class="saas-context-label">{escape(translate(context_key))}</span>'


def _render_user_actions(
    principal: Principal | None,
    *,
    tenant_id: str | None,
    surface: PageSurface,
) -> str:
    del tenant_id, surface
    toolbar_controls = [
        _render_language_control(),
        _render_theme_control(),
    ]
    if principal is not None:
        sign_out_label = f"{translate('nav.sign_out')}: {principal.username}"
        toolbar_controls.append(
            '<a class="saas-toolbar-button" href="/auth/logout" '
            f'aria-label="{escape(sign_out_label)}" title="{escape(sign_out_label)}">'
            '<svg aria-hidden="true" focusable="false" viewBox="0 0 24 24" fill="none" '
            'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" '
            'stroke-linejoin="round"><path '
            'd="M14 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h7'
            'a2 2 0 0 0 2-2v-3"/>'
            '<path d="M10 12h11m-4-4 4 4-4 4"/></svg>'
            '<span class="sr-only">'
            f"{escape(sign_out_label)}</span></a>"
        )
    return f'<div class="saas-toolbar" data-toolbar>{"".join(toolbar_controls)}</div>'


def _render_language_control() -> str:
    current_locale = get_locale()
    language_label = translate("toolbar.language")
    options = (
        ("zh-CN", translate("toolbar.language_chinese")),
        ("en", translate("toolbar.language_english")),
    )
    rendered_options = "".join(
        '<a class="saas-popover-option" role="menuitemradio" '
        f'aria-checked="{str(locale == current_locale).lower()}" '
        f'href="{escape(locale_switch_url(locale))}" lang="{locale}">'
        f'<span>{escape(label)}</span>{_render_selected_check(locale == current_locale)}</a>'
        for locale, label in options
    )
    return (
        '<div class="saas-toolbar-control">'
        '<button class="saas-toolbar-button" type="button" data-popover-trigger="language-menu" '
        'aria-controls="language-menu" aria-expanded="false" aria-haspopup="menu" '
        f'aria-label="{escape(language_label)}" title="{escape(language_label)}">'
        '<svg aria-hidden="true" focusable="false" viewBox="0 0 24 24" fill="none" '
        'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" '
        'stroke-linejoin="round"><circle cx="12" cy="12" r="9"/>'
        '<path d="M3 12h18M12 3a15 15 0 0 1 0 18M12 3a15 15 0 0 0 0 18"/>'
        '</svg></button>'
        '<div class="saas-popover saas-language-popover" id="language-menu" '
        f'data-popover="language-menu" role="menu" aria-label="{escape(language_label)}" hidden>'
        f'<div class="saas-popover-title">{escape(language_label)}</div>{rendered_options}</div>'
        '</div>'
    )


def _render_theme_control() -> str:
    theme_label = translate("toolbar.theme")
    theme_options = (
        ("system", translate("toolbar.theme_system")),
        ("light", translate("toolbar.theme_light")),
        ("dark", translate("toolbar.theme_dark")),
    )
    rendered_options = "".join(
        '<button class="saas-theme-option" type="button" role="menuitemradio" '
        f'data-theme-option="{theme}" aria-checked="false">'
        f'<span class="saas-theme-preview saas-theme-preview-{theme}" aria-hidden="true">'
        '<span class="saas-theme-preview-sidebar"></span>'
        '<span class="saas-theme-preview-header"></span>'
        '<span class="saas-theme-preview-line line-one"></span>'
        '<span class="saas-theme-preview-line line-two"></span></span>'
        f'<span class="saas-theme-option-label">{escape(label)}</span></button>'
        for theme, label in theme_options
    )
    return (
        '<div class="saas-toolbar-control">'
        '<button class="saas-toolbar-button" type="button" data-popover-trigger="theme-menu" '
        'aria-controls="theme-menu" aria-expanded="false" aria-haspopup="menu" '
        f'aria-label="{escape(theme_label)}" title="{escape(theme_label)}">'
        '<svg aria-hidden="true" focusable="false" viewBox="0 0 24 24" fill="none" '
        'stroke="currentColor" stroke-width="1.8" stroke-linecap="round">'
        '<circle cx="12" cy="12" r="3.5"/><path '
        'd="M12 2v2.2M12 19.8V22M4.93 4.93l1.56 1.56M17.51 17.51l1.56 1.56'
        'M2 12h2.2M19.8 12H22M4.93 19.07l1.56-1.56M17.51 6.49l1.56-1.56"/>'
        '</svg></button>'
        '<div class="saas-popover saas-theme-popover" id="theme-menu" '
        f'data-popover="theme-menu" role="menu" aria-label="{escape(theme_label)}" hidden>'
        f'<div class="saas-popover-title">{escape(theme_label)}</div>'
        f'<div class="saas-theme-options">{rendered_options}</div></div></div>'
    )


def _render_selected_check(selected: bool) -> str:
    if not selected:
        return '<span class="saas-option-check" aria-hidden="true"></span>'
    return (
        '<svg class="saas-option-check" aria-hidden="true" focusable="false" '
        'viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" '
        'stroke-linecap="round" stroke-linejoin="round"><path d="m5 10 3 3 7-7"/></svg>'
    )


def _render_sidebar(
    *,
    navigation_html: str,
    surface: PageSurface,
    tenant_id: str | None,
    footer_link_html: str,
    legacy_content: bool,
) -> str:
    if not navigation_html:
        return ""
    admin_banner = (
        '<div class="saas-admin-banner">'
        f'<strong>{escape(translate("shell.system_admin_label"))}</strong>'
        f'<span>{escape(translate("shell.privileged_cross_tenant"))}</span></div>'
        if surface == "system"
        else ""
    )
    close_button = (
        '<button type="button" data-sidebar-close '
        f'aria-label="{escape(translate("shell.close_navigation"))}">'
        '<svg aria-hidden="true" focusable="false" viewBox="0 0 24 24" fill="none" '
        'stroke="currentColor" stroke-width="1.8" stroke-linecap="round">'
        '<path d="m6 6 12 12M18 6 6 18"/></svg></button>'
    )
    footer_html = (
        f'<div class="saas-sidebar-footer">{footer_link_html}</div>'
        if footer_link_html
        else ""
    )
    workspace_name = (
        tenant_id
        if surface == "tenant" and tenant_id
        else translate(
            {
                "tenant": "shell.workspace",
                "admin": "shell.admin_control_plane",
                "system": "shell.system_control_plane",
                "auth": "shell.secure_access",
            }[surface]
        )
    )
    sidebar_header = (
        '<div class="saas-sidebar-header" data-sidebar-header>'
        f'{_render_brand(surface)}<span class="saas-sidebar-workspace">'
        f"{escape(workspace_name)}</span>{close_button}</div>"
    )
    sidebar_contents = (
        f"{sidebar_header}{admin_banner}"
        '<nav id="primary-navigation" '
        f'aria-label="{escape(translate("shell.primary_navigation"))}">'
        f"{navigation_html}</nav>{footer_html}"
    )
    if legacy_content:
        return f'<aside class="sidebar"><div data-sidebar>{sidebar_contents}</div></aside>'
    return f'<aside class="saas-sidebar" data-sidebar>{sidebar_contents}</aside>'


def _render_main_content(
    *,
    title: str,
    description: str,
    body: str,
    primary_action_html: str,
    breadcrumbs: Sequence[tuple[str, str | None]],
    has_sidebar: bool,
    legacy_content: bool,
    workbench: bool,
) -> str:
    if legacy_content:
        return f'<main id="main-content">{body}</main>'
    if workbench:
        main_class = "saas-main saas-main-workbench" if has_sidebar else "app-shell-auth"
        return (
            f'<div class="{main_class}" id="main-content" role="main" '
            f'data-page-layout="workbench" data-workbench-main>{body}</div>'
        )
    breadcrumb_html = _render_breadcrumbs(breadcrumbs)
    page_header_html = (
        f'<header class="saas-page-header"><div><h1>{escape(title)}</h1>'
        f"<p>{escape(description)}</p></div>{primary_action_html}</header>"
    )
    main_class = "saas-main" if has_sidebar else "app-shell-auth"
    return (
        f'<main class="{main_class}" id="main-content" data-page-layout="page">'
        '<div class="saas-content">'
        f"{breadcrumb_html}{page_header_html}{body}</div></main>"
    )


def _render_page_layout(
    sidebar_html: str,
    main_html: str,
    has_sidebar: bool,
    legacy_content: bool,
) -> str:
    if legacy_content:
        shell_class = "app-shell app-shell-nav" if has_sidebar else "app-shell app-shell-auth"
        return f'<div class="{shell_class}">{sidebar_html}{main_html}</div>'
    return f"{sidebar_html}{main_html}"


def _render_cross_surface_link(principal: Principal, *, system_admin: bool) -> str:
    if not principal.is_superadmin:
        return ""
    target = "/app" if system_admin else "/admin/system/overview"
    label = (
        translate("nav.open_tenant_workspace")
        if system_admin
        else translate("nav.system_overview")
    )
    return (
        f'<a class="saas-nav-item" href="{target}">'
        f'<span class="saas-nav-item-content">{navigation_icon("home")}'
        f'<span class="saas-nav-text">{escape(label)}</span></span></a>'
    )
