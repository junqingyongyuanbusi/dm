"""Workspace settings presentation without invented configuration or audit outcomes."""

from collections.abc import Mapping
from urllib.parse import quote

from social_reply.application.account_management.templating import render_template
from social_reply.application.account_management.ui_i18n import get_locale, locale_switch_url

SETTINGS_SECTIONS = ("general", "hours", "notifications", "integration")

_COPY = {
    "general": ("基础信息", "Basic information"),
    "hours": ("接待时间", "Service hours"),
    "notifications": ("通知偏好", "Notification preferences"),
    "integration": ("应用与凭证", "Apps and credentials"),
    "navigation": ("设置分区", "Settings sections"),
    "settings_description": (
        "管理基础信息、接待时间与消息提醒。",
        "Manage workspace preferences and alerts.",
    ),
    "audit_description": (
        "追踪账号、自动回复与权限变更。",
        "Track account, automation and access changes.",
    ),
    "audit_empty": ("没有匹配的记录。", "No matching records."),
    "details": ("详情", "Details"),
    "name": ("工作空间名称", "Workspace name"),
    "language": ("界面语言", "Interface language"),
    "timezone": ("默认时区", "Default timezone"),
    "retention": ("会话保留时间", "Conversation retention"),
    "unavailable": ("未配置", "Not configured"),
    "business_hours": ("周一至周五", "Monday to Friday"),
    "weekend_hours": ("周六、周日", "Saturday and Sunday"),
    "off_hours": ("非工作时间策略", "Outside-hours policy"),
    "channels": ("渠道账号", "Channel accounts"),
    "handoff_alert": ("待人工提醒", "Handoff alerts"),
    "channel_alert": ("渠道异常提醒", "Channel alerts"),
    "feishu": ("飞书通知", "Feishu notifications"),
    "configure": ("配置", "Configure"),
    "health": ("系统健康", "System health"),
    "knowledge": ("知识管理", "Knowledge management"),
    "instructions": ("Agent 业务指令", "Agent instructions"),
    "audit": ("审计日志", "Audit log"),
    "journeys": ("处理旅程", "Processing journeys"),
    "readonly": ("只读记录", "Read-only records"),
    "actor": ("操作人", "Actor"),
    "event": ("事件", "Event"),
    "resource": ("对象", "Resource"),
    "result": ("结果", "Result"),
    "success": ("成功", "Success"),
    "failure": ("失败", "Failed"),
    "blocked": ("已阻断", "Blocked"),
    "denied": ("已拒绝", "Denied"),
}


def settings_copy(key: str) -> str:
    return _COPY[key][1 if get_locale() == "en" else 0]


def audit_result_label(detail: object) -> str:
    """Only display explicit outcomes, never infer success from the event name."""
    if not isinstance(detail, Mapping):
        return "—"
    value = detail.get("outcome", detail.get("status"))
    if not isinstance(value, str):
        return "—"
    result_key = {
        "success": "success", "succeeded": "success",
        "failure": "failure", "failed": "failure", "error": "failure",
        "blocked": "blocked", "denied": "denied",
    }.get(value.casefold())
    return settings_copy(result_key) if result_key else "—"


def validate_settings_section(section: str) -> str:
    if section not in SETTINGS_SECTIONS:
        raise ValueError("invalid_settings_section")
    return section


def render_workspace_settings(
    *,
    tenant_id: str,
    section: str,
    instructions_url: str,
) -> str:
    """Render existing configuration links and unavailable fields without fake defaults."""
    return render_template(
        "tenant/workspace_settings.html",
        section=validate_settings_section(section),
        sections=SETTINGS_SECTIONS,
        labels={key: settings_copy(key) for key in _COPY},
        root=f"/app/t/{quote(tenant_id, safe='')}",
        instructions_url=instructions_url,
        locale=get_locale(),
        chinese_url=locale_switch_url("zh-CN"),
        english_url=locale_switch_url("en"),
    )
