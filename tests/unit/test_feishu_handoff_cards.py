from datetime import UTC, datetime, timedelta

from social_reply.application.handoff_notifications.cards import (
    HandoffCardSnapshot,
    render_handoff_card,
)


def _snapshot(**updates) -> HandoffCardSnapshot:
    created_at = datetime(2026, 8, 5, 10, 0, tzinfo=UTC)
    values = {
        "notification_public_id": "11111111-1111-1111-1111-111111111111",
        "action_nonce": "22222222-2222-2222-2222-222222222222",
        "work_version": 3,
        "card_revision": 4,
        "card_state": "WAITING",
        "platform": "instagram",
        "account_name": "WikiFX Support",
        "channel_type": "dm",
        "contact_label": "Customer 13800138000",
        "reason_code": "RISK_WORD",
        "latest_message": "Call me at 13800138000 or customer@example.com *urgent*",
        "work_created_at": created_at,
        "due_at": created_at + timedelta(minutes=30),
        "rendered_at": created_at + timedelta(minutes=7),
        "assigned_actor": None,
        "claimed_at": None,
        "resolved_at": None,
        "restored_automation_state": None,
        "conversation_url": "https://reply.example.com/admin/conversations/33333333",
        **updates,
    }
    return HandoffCardSnapshot(**values)


def _actions(card: dict) -> list[dict]:
    return [element for element in card["body"]["elements"][1:] if element["tag"] == "button"]


def test_waiting_card_redacts_customer_data_and_emits_versioned_claim_action():
    card = render_handoff_card(_snapshot())

    assert card["header"]["title"]["content"] == "新的人工接管请求"
    content = card["body"]["elements"][0]["content"]
    assert "13800138000" not in content
    assert "customer@example.com" not in content
    assert "\\*urgent\\*" in content
    claim = _actions(card)[0]
    assert claim["text"]["content"] == "认领"
    assert claim["value"] == {
        "contract_version": 1,
        "notification_id": "11111111-1111-1111-1111-111111111111",
        "action": "claim",
        "expected_work_version": 3,
        "expected_card_revision": 4,
        "action_nonce": "22222222-2222-2222-2222-222222222222",
    }


def test_claimed_card_has_confirmed_resolve_action_for_current_revision():
    card = render_handoff_card(
        _snapshot(
            card_state="CLAIMED",
            assigned_actor="feishu_operator:agent-1",
            claimed_at=datetime(2026, 8, 5, 10, 7, tzinfo=UTC),
        )
    )

    resolve = _actions(card)[0]
    assert resolve["text"]["content"] == "已回复，恢复 Bot"
    assert resolve["value"]["action"] == "resolve"
    assert "无法自动验证外部社媒回复" in resolve["confirm"]["text"]["content"]


def test_resolved_card_has_no_state_changing_action_and_names_restored_policy():
    card = render_handoff_card(
        _snapshot(
            card_state="RESOLVED",
            assigned_actor="feishu_operator:agent-1",
            resolved_at=datetime(2026, 8, 5, 10, 12, tzinfo=UTC),
            restored_automation_state="BOT_DRAFT_ONLY",
        )
    )

    assert card["header"]["title"]["content"] == "已处理"
    assert [action["text"]["content"] for action in _actions(card)] == ["打开会话"]
    content = card["body"]["elements"][0]["content"]
    assert "BOT\\_DRAFT\\_ONLY" in content
    assert "下一条新客户消息" in content
