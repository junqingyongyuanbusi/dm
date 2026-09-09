"""Pure, accessible rendering for the tenant operations dashboard."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import quote, unquote, urlsplit

from social_reply.application.account_management.templating import render_template
from social_reply.application.account_management.ui_i18n import get_locale

if TYPE_CHECKING:
    from social_reply.application.account_management.home_dashboard_data import HomeDashboardData


_COPY = {
    "title": ("让每一次对话，都有回应。", "Every conversation deserves a reply."),
    "subtitle": ("工作空间接待总览", "Your workspace at a glance"),
    "inbox": ("进入收件箱", "Open inbox"),
    "pending": ("当前待处理", "Pending now"),
    "pending_note": (
        "等待或正在人工处理的会话",
        "Conversations awaiting or receiving human support",
    ),
    "ai": ("AI 接待中", "AI handling"),
    "ai_note": ("AI 模式会话，含仅生成草稿", "AI-mode conversations, including draft-only mode"),
    "accounts": ("渠道账号", "Channel accounts"),
    "enabled": ("已启用账号", "Enabled accounts"),
    "resolved": ("已解决会话", "Resolved conversations"),
    "resolved_note": ("当前可见范围内已解决", "Resolved within your visible scope"),
    "trend": ("会话趋势", "Conversation trends"),
    "period": ("近 7 天 · UTC", "Last 7 days · UTC"),
    "received": ("入站会话", "Inbound conversations"),
    "ai_replied": ("有 AI 回复", "With AI replies"),
    "trend_note": (
        "按 UTC 日统计入站去重会话（含私信，不含内部私有记录）；"
        "AI 曲线为其中同日有 AI 回复的会话。",
        "Daily unique inbound conversations (UTC), including direct messages "
        "but excluding internal private records; "
        "the AI series is the subset with an AI reply on the same day.",
    ),
    "empty_trend": ("暂无接待记录", "No conversation activity yet"),
    "empty_trend_note": (
        "收到渠道消息后，趋势会显示在这里。",
        "Trends will appear when channel messages arrive.",
    ),
    "data_table": ("查看每日数据", "View daily data"),
    "day": ("日期（UTC）", "Date (UTC)"),
    "attention": ("需要关注", "Needs attention"),
    "attention_note": ("根据已记录的工作空间状态", "Based on recorded workspace state"),
    "view": ("查看", "View"),
    "routine": ("例行检查", "Routine check"),
    "routine_note": (
        "暂无需要关注的记录。可前往收件箱检查接待情况。",
        "No attention items are recorded. Check the inbox as part of your routine.",
    ),
    "channels": ("各渠道接待", "Conversations by channel"),
    "channel_note": (
        "近 7 天入站去重会话数；账号数量单独列示。",
        "Unique inbound conversations over 7 days; account counts are shown separately.",
    ),
    "channel_accounts": ("{count} 个账号", "{count} accounts"),
    "channel_received": ("{count} 个会话", "{count} conversations"),
    "empty_channels": ("暂无可见渠道账号", "No visible channel accounts"),
    "empty_channels_note": (
        "接入并获得账号访问权限后，可在这里查看接待分布。",
        "Channel activity appears once accounts are connected and visible to you.",
    ),
    "quality": ("接待质量待办", "Service quality checklist"),
    "quality_note": ("从知识、审核与多语言体验开始", "Knowledge, review and multilingual quality"),
    "reply_drafts": ("{count} 条回复草稿待审核", "{count} reply drafts awaiting review"),
    "reply_drafts_note": (
        "确认回复准确、合规后再发送。",
        "Review accuracy and compliance before sending.",
    ),
    "knowledge_drafts": ("{count} 条知识草稿待完善", "{count} knowledge drafts to review"),
    "knowledge_drafts_note": (
        "核对金融知识来源与适用范围，再审核发布。",
        "Verify financial sources and applicability before publishing.",
    ),
    "add_knowledge": ("完善金融知识库", "Build your financial knowledge base"),
    "view_knowledge": ("查看金融知识库", "View financial knowledge"),
    "no_knowledge": (
        "暂无可见的已发布知识。先梳理开户、出入金与风险提示问答。",
        "No published knowledge is visible. Start with account opening, payments and risk FAQs.",
    ),
    "readonly_knowledge": (
        "可查看金融知识指引，知识编辑与发布由管理员完成。",
        "Read financial knowledge guidance; an administrator manages edits and publishing.",
    ),
    "knowledge_check": ("例行检查金融知识", "Review financial knowledge"),
    "published": ("{count} 条已发布知识", "{count} published knowledge entries"),
    "languages": ("测试多语言接待", "Test multilingual replies"),
    "languages_note": (
        "在测试台检查不同语言的答复与风险提示。",
        "Check replies and risk notices across languages in the playground.",
    ),
}

_PLATFORM_LABELS = {
    "facebook": "Facebook",
    "instagram": "Instagram",
    "telegram": "Telegram",
    "x": "X / Twitter",
    "whatsapp": "WhatsApp",
    "email": "Email",
    "feishu": "Feishu",
}


@dataclass(frozen=True)
class _TrendPoint:
    day: date
    received: int
    ai: int


def _nice_maximum(maximum: int) -> int:
    """Use four equal integer intervals with a rounded 1/2/5/10 step."""
    rough_step = max(1, math.ceil(maximum / 4))
    magnitude = 10 ** (len(str(rough_step)) - 1)
    step = next(factor * magnitude for factor in (1, 2, 5, 10) if factor * magnitude >= rough_step)
    return step * 4


def _chart_context(data: HomeDashboardData, today: date) -> dict[str, object]:
    trend = data.trend or tuple(
        _TrendPoint(today - timedelta(days=6 - index), 0, 0) for index in range(7)
    )
    maximum = _nice_maximum(max(max(point.received, point.ai) for point in trend))
    points = tuple(
        {
            "day": point.day.isoformat(),
            "label": point.day.strftime("%m/%d"),
            "received": point.received,
            "ai": point.ai,
            "horizontal": round(60 + index * 620 / (len(trend) - 1), 2) if len(trend) > 1 else 370,
            "received_vertical": round(190 - point.received / maximum * 172, 2),
            "ai_vertical": round(190 - point.ai / maximum * 172, 2),
        }
        for index, point in enumerate(trend)
    )
    series = tuple(
        {
            "key": key,
            "line": "M "
            + " L ".join(f"{point['horizontal']} {point[f'{key}_vertical']}" for point in points),
            "area": f"M {points[0]['horizontal']} 190 L "
            + " L ".join(f"{point['horizontal']} {point[f'{key}_vertical']}" for point in points)
            + f" L {points[-1]['horizontal']} 190 Z",
        }
        for key in ("received", "ai")
    )
    return {
        "points": points,
        "series": series,
        "ticks": tuple(
            {"value": maximum * index // 4, "vertical": 190 - index * 43} for index in range(5)
        ),
        "empty": not any(point.received for point in trend),
    }


def _safe_attention_href(href: str, root: str) -> str:
    try:
        parsed = urlsplit(href)
    except ValueError:
        return f"{root}/inbox"
    decoded_path = unquote(parsed.path)
    if (
        not parsed.scheme
        and not parsed.netloc
        and parsed.path.startswith(f"{root}/")
        and decoded_path.startswith(f"{unquote(root)}/")
        and "\\" not in decoded_path
        and ".." not in decoded_path.split("/")
        and not any(ord(character) < 32 for character in unquote(href))
    ):
        return href
    return f"{root}/inbox"


def _quality_items(
    data: HomeDashboardData,
    root: str,
    labels: dict[str, str],
    can_manage: bool,
) -> tuple[dict[str, str], ...]:
    items: tuple[dict[str, str], ...] = ()
    if can_manage and data.pending_draft_count:
        items += (
            {
                "title": labels["reply_drafts"].format(count=data.pending_draft_count),
                "description": labels["reply_drafts_note"],
                "href": f"{root}/inbox?queue=drafts",
            },
        )
    if can_manage and data.draft_knowledge_count:
        items += (
            {
                "title": labels["knowledge_drafts"].format(count=data.draft_knowledge_count),
                "description": labels["knowledge_drafts_note"],
                "href": f"{root}/knowledge",
            },
        )
    if not data.published_knowledge_count:
        items += (
            {
                "title": labels["add_knowledge" if can_manage else "view_knowledge"],
                "description": labels["no_knowledge" if can_manage else "readonly_knowledge"],
                "href": f"{root}/knowledge",
            },
        )
    else:
        items += (
            {
                "title": labels["knowledge_check"],
                "description": labels["published"].format(count=data.published_knowledge_count),
                "href": f"{root}/knowledge",
            },
        )
    if can_manage:
        items += (
            {
                "title": labels["languages"],
                "description": labels["languages_note"],
                "href": f"{root}/playground",
            },
        )
    return items


def render_home_dashboard(
    data: HomeDashboardData,
    tenant_id: str,
    now: datetime,
    can_manage: bool,
) -> str:
    """Return an HTML body; the page shell loads /static/home-dashboard.css."""
    labels = {key: variants[get_locale() == "en"] for key, variants in _COPY.items()}
    root = f"/app/t/{quote(tenant_id, safe='')}"
    today = (now if now.tzinfo else now.replace(tzinfo=UTC)).astimezone(UTC).date()
    maximum_received = max((channel.received for channel in data.channels), default=0)
    metrics = (
        {"key": "pending", "value": data.pending_count, "note": labels["pending_note"]},
        {"key": "ai", "value": data.ai_count, "note": labels["ai_note"]},
        {
            "key": "accounts",
            "value": data.account_count,
            "note": f"{labels['enabled']}: {data.enabled_account_count}",
        },
        {"key": "resolved", "value": data.resolved_count, "note": labels["resolved_note"]},
    )
    return render_template(
        "tenant/home_dashboard.html",
        labels=labels,
        root=root,
        today=today.isoformat(),
        metrics=metrics,
        chart=_chart_context(data, today),
        attention=tuple(
            {
                "title": str(item.title),
                "description": str(item.description),
                "href": _safe_attention_href(item.href, root),
            }
            for item in data.attention
        ),
        channels=tuple(
            {
                "platform": _PLATFORM_LABELS.get(channel.platform, str(channel.platform)),
                "accounts": labels["channel_accounts"].format(count=channel.accounts),
                "received": labels["channel_received"].format(count=channel.received),
                "width": round(channel.received / maximum_received * 100, 1)
                if maximum_received
                else 0.0,
            }
            for channel in data.channels
        ),
        quality_items=_quality_items(data, root, labels, can_manage),
    )
