from dataclasses import dataclass
from datetime import datetime

from social_reply.domain.reply.guard import redact_pii

_MAX_SUMMARY_CHARS = 280
_REASON_LABELS = {
    "RISK_WORD": "高风险内容",
    "UNSUPPORTED_ATTACHMENT": "不支持的附件",
    "INSUFFICIENT_KNOWLEDGE": "知识不足",
    "LLM_SCHEMA_FAIL": "AI 输出校验失败",
    "LLM_UNAVAILABLE": "AI 服务不可用",
    "HANDOFF": "需要人工判断",
}


@dataclass(frozen=True)
class HandoffCardSnapshot:
    notification_public_id: str
    action_nonce: str
    work_version: int
    card_revision: int
    card_state: str
    platform: str
    account_name: str
    channel_type: str
    contact_label: str
    reason_code: str
    latest_message: str
    work_created_at: datetime
    due_at: datetime | None
    rendered_at: datetime
    assigned_actor: str | None
    claimed_at: datetime | None
    resolved_at: datetime | None
    restored_automation_state: str | None
    conversation_url: str


def _safe_text(value: str, *, limit: int) -> str:
    normalized = " ".join(redact_pii(value or "").split())
    if len(normalized) > limit:
        return normalized[: limit - 1] + "…"
    return normalized or "—"


def _markdown(value: str, *, limit: int) -> str:
    safe = _safe_text(value, limit=limit)
    for character in ("\\", "`", "*", "_", "[", "]", "<", ">", "~"):
        safe = safe.replace(character, f"\\{character}")
    return safe


def _reason(reason_code: str) -> str:
    label = _REASON_LABELS.get(reason_code, "需要人工处理")
    return f"{label} (`{_markdown(reason_code, limit=80)}`)"


def _elapsed_minutes(snapshot: HandoffCardSnapshot) -> int:
    return max(0, int((snapshot.rendered_at - snapshot.work_created_at).total_seconds() // 60))


def _action_value(snapshot: HandoffCardSnapshot, action: str) -> dict[str, object]:
    return {
        "contract_version": 1,
        "notification_id": snapshot.notification_public_id,
        "action": action,
        "expected_work_version": snapshot.work_version,
        "expected_card_revision": snapshot.card_revision,
        "action_nonce": snapshot.action_nonce,
    }


def _open_button(snapshot: HandoffCardSnapshot) -> dict[str, object]:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": "打开会话"},
        "type": "default",
        "url": snapshot.conversation_url,
    }


def render_handoff_card(snapshot: HandoffCardSnapshot) -> dict[str, object]:
    state = snapshot.card_state
    if state == "WAITING":
        title = "新的人工接管请求"
        template = "orange"
    elif state == "CLAIMED":
        title = "已由客服认领"
        template = "blue"
    elif state == "RESOLVED":
        title = "已处理"
        template = "green"
    elif state == "CANCELLED":
        title = "接管请求已取消"
        template = "grey"
    else:
        raise ValueError("handoff_card_state_invalid")

    lines = [
        f"**来源**：{_markdown(snapshot.platform, limit=40)} / "
        f"{_markdown(snapshot.account_name, limit=80)}",
        f"**会话**：{_markdown(snapshot.channel_type, limit=40)}",
        f"**客户**：{_markdown(snapshot.contact_label, limit=80)}",
        f"**原因**：{_reason(snapshot.reason_code)}",
        f"**消息摘要**：{_markdown(snapshot.latest_message, limit=_MAX_SUMMARY_CHARS)}",
        f"**已等待**：{_elapsed_minutes(snapshot)} 分钟",
    ]
    if snapshot.due_at is not None:
        lines.append(f"**SLA 时间**：{snapshot.due_at.isoformat(timespec='minutes')}")
    if state in {"CLAIMED", "RESOLVED"}:
        lines.append(f"**认领客服**：{_markdown(snapshot.assigned_actor or '未知', limit=100)}")
    if state == "RESOLVED":
        restored = snapshot.restored_automation_state or "账号当前策略"
        lines.extend(
            (
                f"**恢复策略**：{_markdown(restored, limit=40)}",
                "只有下一条新客户消息会重新进入 Bot 流程，人工期间消息不会补答。",
            )
        )

    actions = [_open_button(snapshot)]
    if state == "WAITING":
        actions.insert(
            0,
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "认领"},
                "type": "primary",
                "value": _action_value(snapshot, "claim"),
            },
        )
    elif state == "CLAIMED":
        actions.insert(
            0,
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "已回复，恢复 Bot"},
                "type": "primary",
                "value": _action_value(snapshot, "resolve"),
                "confirm": {
                    "title": {"tag": "plain_text", "content": "确认恢复 Bot"},
                    "text": {
                        "tag": "plain_text",
                        "content": (
                            "Reply Core 无法自动验证外部社媒回复。继续表示你确认已完成"
                            "客户回复，并同意恢复该会话的账号自动化策略。"
                        ),
                    },
                },
            },
        )

    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": template,
        },
        # Feishu card V2 removed the <action> module: buttons must be standalone
        # body elements (as top-level element modules) or the card is rejected.
        "body": {"elements": [{"tag": "markdown", "content": "\n".join(lines)}, *actions]},
    }
