from social_reply.connectors.chatwoot.normalizer import (
    ChatwootMessage,
    EventClass,
    classify,
    parse_message_created,
)

BASE = {
    "event": "message_created",
    "id": 55,
    "content": "你好",
    "message_type": "incoming",
    "private": False,
    "created_at": "2026-07-14T10:00:00Z",
    "sender": {"id": 9, "type": "contact"},
    "conversation": {"id": 77, "inbox_id": 101, "status": "pending"},
    "account": {"id": 1},
}


def _payload(**overrides) -> dict:
    p = {**BASE, **overrides}
    return p


def test_parse_extracts_fields():
    msg = parse_message_created(_payload())
    assert msg == ChatwootMessage(
        chatwoot_message_id=55,
        content="你好",
        message_type="incoming",
        private=False,
        sender_id="9",
        sender_type="contact",
        chatwoot_conversation_id=77,
        chatwoot_inbox_id=101,
        chatwoot_account_id=1,
        occurred_at_iso="2026-07-14T10:00:00Z",
    )


def test_incoming_public_is_inbound_user():
    assert classify(parse_message_created(_payload())) is EventClass.INBOUND_USER


def test_incoming_without_text_is_not_reply_eligible():
    assert classify(parse_message_created(_payload(content=None))) is EventClass.IGNORE


def test_agent_outgoing_public_flips_human():
    p = _payload(message_type="outgoing", sender={"id": 3, "type": "user"})
    assert classify(parse_message_created(p)) is EventClass.AGENT_PUBLIC_REPLY


def test_bot_outgoing_is_reconcile_only():
    p = _payload(message_type="outgoing", sender={"id": 2, "type": "agent_bot"})
    assert classify(parse_message_created(p)) is EventClass.BOT_ECHO


def test_private_note_ignored():
    # 私有备注不触发任何状态变更
    p = _payload(message_type="outgoing", private=True, sender={"id": 3, "type": "user"})
    assert classify(parse_message_created(p)) is EventClass.IGNORE


def test_integer_message_type_from_api_payload():
    # Chatwoot 部分 payload 用整数 0=incoming/1=outgoing，需容错
    p = _payload(message_type=0)
    assert classify(parse_message_created(p)) is EventClass.INBOUND_USER
