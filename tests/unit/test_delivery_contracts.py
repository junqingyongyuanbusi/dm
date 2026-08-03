import uuid
from datetime import UTC, datetime, timedelta

import pytest

from social_reply.application.message_delivery.contracts import (
    SendContractError,
    build_direct_reply_destination,
    parse_direct_text_command,
)


@pytest.mark.parametrize(
    ("destination_type", "platform", "external_id", "target"),
    [
        ("telegram_dm", "telegram", None, {"chat_id": 42}),
        ("meta_messenger_dm", "facebook", "page-1", {"kind": "dm", "recipient_id": "u1"}),
        ("meta_instagram_dm", "instagram", "ig-1", {"kind": "dm", "recipient_id": "u1"}),
        (
            "meta_public_comment",
            "facebook",
            "page-1",
            {"kind": "comment", "comment_id": "c1"},
        ),
        (
            "meta_private_reply",
            "instagram",
            "ig-1",
            {"kind": "private_reply", "comment_id": "c1"},
        ),
        (
            "whatsapp_session_message",
            "whatsapp",
            "phone-1",
            {"kind": "session_message", "phone_number_id": "phone-1", "to": "1555"},
        ),
        ("x_dm", "x", "x-1", {"kind": "dm", "participant_id": "u1"}),
        (
            "x_chat_message",
            "x",
            "x-1",
            {"kind": "x_chat", "conversation_id": "u1-u2", "conversation_token": "t1"},
        ),
        (
            "x_post_reply",
            "x",
            "x-1",
            {"kind": "reply", "in_reply_to_post_id": "p1"},
        ),
        (
            "feishu_p2p_reply",
            "feishu",
            "app-1",
            {
                "kind": "dm",
                "message_id": "om_1",
                "chat_id": "oc_1",
                "chat_type": "p2p",
                "sender_open_id": "ou_1",
            },
        ),
        (
            "feishu_group_reply",
            "feishu",
            "app-1",
            {
                "kind": "mention",
                "message_id": "om_2",
                "chat_id": "oc_2",
                "chat_type": "group",
                "sender_open_id": "ou_2",
                "thread_id": "omt_1",
                "root_id": "om_root",
            },
        ),
    ],
)
def test_parse_direct_text_command_accepts_canonical_targets(
    destination_type, platform, external_id, target
):
    outbox_id = uuid.uuid4()
    command = parse_direct_text_command(
        destination_type=destination_type,
        message_type="text",
        payload={"text": "hello", "target": target},
        destination_id="conversation:42",
        account_platform=platform,
        account_external_id=external_id,
        source_target=target,
        conversation_external_user_id=str(
            target.get("recipient_id")
            or target.get("participant_id")
            or target.get("to")
            or "user-1"
        ),
        outbox_id=outbox_id,
    )

    assert command.text == "hello"
    assert command.platform == platform
    if destination_type == "x_chat_message":
        assert command.target["message_id"] == str(outbox_id)
        assert command.target["conversation_token"] == "t1"
    elif platform == "feishu":
        assert command.target == {**target, "uuid": str(outbox_id)}
    else:
        assert command.target == target


def test_parse_direct_text_command_rejects_wrong_recipient_with_valid_shape():
    with pytest.raises(SendContractError) as error:
        parse_direct_text_command(
            destination_type="x_dm",
            message_type="text",
            payload={
                "text": "hello",
                "target": {"kind": "dm", "participant_id": "user-2"},
            },
            destination_id="x:account:user-1",
            account_platform="x",
            account_external_id="x-1",
            source_target={"kind": "dm", "participant_id": "user-1"},
            conversation_external_user_id="user-1",
            outbox_id=uuid.uuid4(),
        )
    assert error.value.code == "DELIVERY_TARGET_INVALID"


@pytest.mark.parametrize("field", ["message_id", "chat_id", "sender_open_id", "thread_id"])
def test_parse_feishu_group_reply_rejects_changed_immutable_fields(field):
    source_target = {
        "kind": "mention",
        "message_id": "om_1",
        "chat_id": "oc_1",
        "chat_type": "group",
        "sender_open_id": "ou_1",
        "thread_id": "omt_1",
        "root_id": "om_root",
    }
    target = {**source_target, field: "changed"}

    with pytest.raises(SendContractError) as error:
        parse_direct_text_command(
            destination_type="feishu_group_reply",
            message_type="text",
            payload={"text": "hello", "target": target},
            destination_id="feishu:account:chat",
            account_platform="feishu",
            account_external_id="app-1",
            source_target=source_target,
            conversation_external_user_id="ou_1",
            outbox_id=uuid.uuid4(),
        )

    assert error.value.code == "DELIVERY_TARGET_INVALID"


def test_parse_feishu_reply_ignores_caller_uuid_and_uses_outbox_id():
    outbox_id = uuid.uuid4()
    target = {
        "kind": "dm",
        "message_id": "om_1",
        "chat_id": "oc_1",
        "chat_type": "p2p",
        "sender_open_id": "ou_1",
    }
    command = parse_direct_text_command(
        destination_type="feishu_p2p_reply",
        message_type="text",
        payload={"text": "hello", "target": target, "uuid": "caller-controlled"},
        destination_id="feishu:account:chat",
        account_platform="feishu",
        account_external_id="app-1",
        source_target=target,
        conversation_external_user_id="ou_1",
        outbox_id=outbox_id,
    )
    assert command.target["uuid"] == str(outbox_id)


def test_parse_feishu_reply_rejects_unknown_target_fields():
    target = {
        "kind": "dm",
        "message_id": "om_1",
        "chat_id": "oc_1",
        "chat_type": "p2p",
        "sender_open_id": "ou_1",
        "tenant_key": "must-not-pass-through",
    }
    with pytest.raises(SendContractError, match="feishu_target_unknown_keys"):
        parse_direct_text_command(
            destination_type="feishu_p2p_reply",
            message_type="text",
            payload={"text": "hello", "target": target},
            destination_id="feishu:account:chat",
            account_platform="feishu",
            account_external_id="app-1",
            source_target=target,
            conversation_external_user_id="ou_1",
            outbox_id=uuid.uuid4(),
        )


def test_parse_direct_text_command_preserves_telegram_legacy_fallback():
    command = parse_direct_text_command(
        destination_type="telegram_dm",
        message_type="text",
        payload={"text": "hello"},
        destination_id="telegram:account:42",
        account_platform="telegram",
        account_external_id=None,
        source_target={},
        conversation_external_user_id="user-1",
        outbox_id=uuid.uuid4(),
    )
    assert command.target == {"chat_id": 42}


@pytest.mark.parametrize(
    ("destination_type", "platform", "external_id", "target"),
    [
        ("x_dm", "x", "x-1", {"kind": "reply", "in_reply_to_post_id": "p1"}),
        ("x_post_reply", "x", "x-1", {"kind": "dm", "participant_id": "u1"}),
        ("meta_messenger_dm", "facebook", "page-1", {"kind": "comment", "comment_id": "c1"}),
        ("meta_public_comment", "facebook", "page-1", {"kind": "dm", "recipient_id": "u1"}),
        (
            "whatsapp_session_message",
            "whatsapp",
            "phone-1",
            {"kind": "session_message", "phone_number_id": "phone-2", "to": "1555"},
        ),
        ("x_chat_message", "x", "x-1", {"kind": "x_chat", "conversation_id": ""}),
    ],
)
def test_parse_direct_text_command_rejects_mismatched_targets(
    destination_type, platform, external_id, target
):
    with pytest.raises(SendContractError) as error:
        parse_direct_text_command(
            destination_type=destination_type,
            message_type="text",
            payload={"text": "hello", "target": target},
            destination_id="conversation:42",
            account_platform=platform,
            account_external_id=external_id,
            source_target=target,
            conversation_external_user_id=str(
                target.get("recipient_id")
                or target.get("participant_id")
                or target.get("to")
                or "user-1"
            ),
            outbox_id=uuid.uuid4(),
        )
    assert error.value.code == "DELIVERY_TARGET_INVALID"


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (None, "DELIVERY_PAYLOAD_INVALID"),
        ({"target": {"chat_id": 42}}, "DELIVERY_TEXT_INVALID"),
        ({"text": 123, "target": {"chat_id": 42}}, "DELIVERY_TEXT_INVALID"),
        ({"text": "   ", "target": {"chat_id": 42}}, "DELIVERY_TEXT_INVALID"),
        ({"text": "hello", "target": []}, "DELIVERY_TARGET_INVALID"),
    ],
)
def test_parse_direct_text_command_rejects_malformed_payload(payload, code):
    with pytest.raises(SendContractError) as error:
        parse_direct_text_command(
            destination_type="telegram_dm",
            message_type="text",
            payload=payload,
            destination_id="telegram:account:42",
            account_platform="telegram",
            account_external_id=None,
            source_target={"chat_id": 42},
            conversation_external_user_id="user-1",
            outbox_id=uuid.uuid4(),
        )
    assert error.value.code == code


def test_build_direct_reply_destination_rejects_private_x_post_reply():
    with pytest.raises(ValueError, match="x_post_reply_requires_public_visibility"):
        build_direct_reply_destination(
            platform="x",
            reply_target={"kind": "reply", "in_reply_to_post_id": "post-1"},
            visibility="private",
            occurred_at=None,
            now=datetime.now(UTC),
        )


def test_build_direct_reply_destination_centralizes_target_and_window_mapping():
    now = datetime(2026, 7, 24, 10, 0, tzinfo=UTC)
    occurred_at = now - timedelta(hours=2)

    dm = build_direct_reply_destination(
        platform="instagram",
        reply_target={"kind": "dm", "recipient_id": "u1"},
        visibility="public",
        occurred_at=occurred_at,
        now=now,
    )
    assert dm.destination_type == "meta_instagram_dm"
    assert dm.valid_until == occurred_at + timedelta(hours=24)

    private_reply = build_direct_reply_destination(
        platform="facebook",
        reply_target={"kind": "comment", "comment_id": "c1"},
        visibility="private",
        occurred_at=occurred_at,
        now=now,
    )
    assert private_reply.destination_type == "meta_private_reply"
    assert private_reply.target == {"kind": "private_reply", "comment_id": "c1"}
    assert private_reply.valid_until == now + timedelta(days=7)

    feishu_p2p_target = {
        "kind": "dm",
        "message_id": "om_1",
        "chat_id": "oc_1",
        "chat_type": "p2p",
        "sender_open_id": "ou_1",
    }
    feishu_p2p = build_direct_reply_destination(
        platform="feishu",
        reply_target=feishu_p2p_target,
        visibility="private",
        occurred_at=occurred_at,
        now=now,
    )
    assert feishu_p2p.destination_type == "feishu_p2p_reply"
    assert feishu_p2p.target == feishu_p2p_target
    assert feishu_p2p.valid_until is None

    feishu_group_target = {
        "kind": "mention",
        "message_id": "om_2",
        "chat_id": "oc_2",
        "chat_type": "group",
        "sender_open_id": "ou_2",
        "thread_id": "omt_1",
        "root_id": "om_root",
    }
    feishu_group = build_direct_reply_destination(
        platform="feishu",
        reply_target=feishu_group_target,
        visibility="public",
        occurred_at=occurred_at,
        now=now,
    )
    assert feishu_group.destination_type == "feishu_group_reply"
    assert feishu_group.target == feishu_group_target
    assert feishu_group.valid_until is None


def test_build_direct_reply_destination_rejects_incomplete_feishu_target():
    with pytest.raises(SendContractError) as error:
        build_direct_reply_destination(
            platform="feishu",
            reply_target={"kind": "mention", "chat_type": "group"},
            visibility="public",
            occurred_at=None,
            now=datetime.now(UTC),
        )
    assert error.value.code == "DELIVERY_TARGET_INVALID"
