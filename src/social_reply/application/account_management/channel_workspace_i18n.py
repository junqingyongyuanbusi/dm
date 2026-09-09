"""Read-only channel workspace copy, selected from the request locale."""

from collections.abc import Iterator, Mapping
from types import MappingProxyType

from social_reply.application.account_management.ui_i18n import get_locale

_COPY = MappingProxyType({
    "all_platforms": ("全部平台", "All platforms"),
    "all_statuses": ("全部状态", "All statuses"),
    "connected": ("检测通过", "Checked"),
    "limited": ("消息能力受限", "Messaging limited"),
    "error": ("连接异常", "Connection error"),
    "pending": ("待检测", "Not checked"),
    "disabled": ("已停用", "Disabled"),
    "dm": ("私信", "Direct messages"),
    "email": ("邮件", "Email"),
    "feishu": ("飞书", "Feishu"),
    "close": ("关闭", "Close"),
    "comments": ("评论", "Comments"),
    "session_messages": ("会话消息", "Session messages"),
    "templates": ("模板消息", "Template messages"),
    "x_chat": ("XChat", "XChat"),
    "mentions": ("提及", "Mentions"),
    "BOT_DRAFT_ONLY": ("仅草稿", "Draft only"),
    "BOT_ACTIVE": ("自动回复", "Auto-reply"),
    "HANDOFF_PENDING": ("等待人工接管", "Waiting for human handoff"),
    "HUMAN_ACTIVE": ("人工接待模式", "Human support mode"),
    "BOT_COOLDOWN": ("自动回复冷却中", "Auto-reply cooldown"),
    "CLOSED": ("自动化已关闭", "Automation closed"),
    "automation_unknown": ("待检测", "Not checked"),
    "organization_account": ("组织账号", "Organization account"),
    "owner_unavailable": ("未分配", "Unassigned"),
    "identity_unavailable": ("未提供标识", "No identity"),
    "unknown_platform": ("未知平台", "Unknown platform"),
    "visible": ("渠道账号", "Channel accounts"),
    "draft": ("仅草稿", "Draft only"),
    "blocked": ("紧急阻断", "Emergency blocks"),
    "capabilities": ("消息能力", "Messaging capabilities"),
    "not_enabled": ("未启用", "Not enabled"),
    "capabilities_unknown": ("待检测", "Not checked"),
    "brand": ("所属 Agent", "Agent"),
    "owner": ("账号授权人", "Authorized by"),
    "sending_blocked": ("紧急阻断", "Emergency block"),
    "manage": ("管理", "Manage"),
    "overview": ("当前可见账号概况", "Visible account overview"),
    "platform_filter": ("筛选渠道平台", "Filter channel platforms"),
    "search": ("搜索渠道账号", "Search channel accounts"),
    "search_placeholder": ("搜索账号名称或用户名", "Search account name or username"),
    "status_filter": ("筛选连接状态", "Filter connection status"),
    "filter": ("筛选", "Filter"),
    "clear_filters": ("清除筛选", "Clear filters"),
    "result_count": ("{count} 个账号", "{count} accounts"),
    "accounts": ("渠道账号", "Connected accounts"),
    "connect_another": ("连接另一个账号", "Connect another account"),
    "platform_separator": ("、", ", "),
    "platform_or": ("或", " or "),
    "no_matches": ("没有匹配的账号", "No matching accounts"),
    "no_accounts": ("当前没有可见账号", "No visible accounts yet"),
    "empty_connect": (
        "调整筛选条件，或连接你的第一个账号。",
        "Adjust your filters, or connect your first account.",
    ),
    "empty_readonly": (
        "调整筛选条件，或联系管理员确认账号访问权限。",
        "Adjust your filters, or contact an administrator to confirm account access.",
    ),
    "add_channels": ("添加渠道账号", "Add channels"),
    "progress": ("接入记录", "Connection history"),
    "recent_jobs": ("最近任务 · {count} 个进行中", "Recent jobs · {count} in progress"),
})


def channel_copy(key: str) -> str:
    return _COPY[key][1 if get_locale() == "en" else 0]


class ConnectionLabels(Mapping[str, str]):
    """Keep legacy mapping consumers localized without changing shared state."""

    __slots__ = ()

    def __getitem__(self, key: str) -> str:
        if key not in ("connected", "limited", "error", "pending", "disabled"):
            raise KeyError(key)
        return channel_copy(key)

    def __iter__(self) -> Iterator[str]:
        return iter(("connected", "limited", "error", "pending", "disabled"))

    def __len__(self) -> int:
        return 5


CONNECTION_LABELS: Mapping[str, str] = ConnectionLabels()
