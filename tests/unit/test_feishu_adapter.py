import json

from social_reply.connectors.feishu.adapter import FeishuWebhookAdapter
from social_reply.domain.messages.canonical import ChannelType


def _payload(
    *,
    message_id="om_1",
    chat_id="oc_1",
    chat_type="p2p",
    sender_open_id="ou_user_1",
    sender_type="user",
    message_type="text",
    content=None,
    mentions=None,
    thread_id=None,
    root_id=None,
    event_type="im.message.receive_v1",
):
    message = {
        "message_id": message_id,
        "chat_id": chat_id,
        "chat_type": chat_type,
        "message_type": message_type,
        "content": json.dumps(content if content is not None else {"text": "hello"}),
    }
    if mentions is not None:
        message["mentions"] = mentions
    if thread_id is not None:
        message["thread_id"] = thread_id
    if root_id is not None:
        message["root_id"] = root_id
    return {
        "schema": "2.0",
        "header": {
            "event_id": "evt_1",
            "event_type": event_type,
            "create_time": "1785729600000",
            "token": "must-not-be-copied-to-metadata",
            "app_id": "cli_fixture",
            "tenant_key": "tenant-key",
        },
        "event": {
            "sender": {
                "sender_id": {"open_id": sender_open_id},
                "sender_type": sender_type,
            },
            "message": message,
        },
    }


def _adapter():
    return FeishuWebhookAdapter(
        account_id="account-1",
        bot_open_id="ou_bot",
        group_mode="mentions_only",
    )


def test_p2p_text_normalizes_to_dm_contract():
    event = _adapter().normalize(_payload())[0]
    assert event.external_event_id == "om_1"
    assert event.external_user_id == "ou_user_1"
    assert event.conversation_key == "feishu:account-1:oc_1:ou_user_1"
    assert event.channel_type is ChannelType.DM
    assert event.event_namespace == "im.message.receive_v1"
    assert event.text == "hello"
    assert event.reply_target == {
        "kind": "dm",
        "message_id": "om_1",
        "chat_id": "oc_1",
        "chat_type": "p2p",
        "sender_open_id": "ou_user_1",
    }
    assert "token" not in event.event_metadata


def test_group_bot_mention_is_required_and_only_bot_key_is_stripped():
    mentions = [
        {"key": "@_user_1", "id": {"open_id": "ou_bot"}},
        {"key": "@_user_2", "id": {"open_id": "ou_human"}},
    ]
    event = _adapter().normalize(
        _payload(
            chat_type="group",
            content={"text": "@_user_1 hello @_user_2"},
            mentions=mentions,
        )
    )[0]
    assert event.text == "hello @_user_2"
    assert event.channel_type is ChannelType.MENTION
    assert event.reply_target["kind"] == "mention"
    assert _adapter().normalize(_payload(chat_type="group", mentions=[])) == []


def test_group_and_thread_conversations_are_isolated_by_sender_and_thread():
    mentions = [{"key": "@bot", "id": {"open_id": "ou_bot"}}]
    first = _adapter().normalize(
        _payload(chat_type="group", mentions=mentions, sender_open_id="ou_1")
    )[0]
    second = _adapter().normalize(
        _payload(chat_type="group", mentions=mentions, sender_open_id="ou_2")
    )[0]
    threaded = _adapter().normalize(
        _payload(
            chat_type="group",
            mentions=mentions,
            sender_open_id="ou_1",
            thread_id="omt_1",
            root_id="om_root",
        )
    )[0]
    assert first.conversation_key != second.conversation_key
    same_thread = _adapter().normalize(
        _payload(
            message_id="om_2",
            chat_type="group",
            mentions=mentions,
            sender_open_id="ou_1",
            thread_id="omt_1",
            root_id="om_root",
        )
    )[0]
    assert first.conversation_key != threaded.conversation_key
    assert threaded.conversation_key == same_thread.conversation_key
    assert threaded.reply_target["thread_id"] == "omt_1"
    assert threaded.reply_target["root_id"] == "om_root"


def test_bot_app_and_unsupported_events_are_ignored():
    assert _adapter().normalize(_payload(sender_open_id="ou_bot")) == []
    assert _adapter().normalize(_payload(sender_type="app")) == []
    assert _adapter().normalize(_payload(event_type="contact.user.created_v3")) == []
    unsupported_schema = _payload()
    unsupported_schema["schema"] = "1.0"
    assert _adapter().normalize(unsupported_schema) == []


def test_non_text_message_is_preserved_as_safe_attachment():
    event = _adapter().normalize(
        _payload(
            message_type="file",
            content={
                "file_key": "file_1",
                "file_name": "guide.pdf",
                "mime_type": "application/pdf",
                "secret": "do-not-copy",
            },
        )
    )[0]
    assert event.text is None
    assert event.attachments == [
        {
            "type": "file",
            "platform_id": "file_1",
            "metadata": {
                "file_name": "guide.pdf",
                "mime_type": "application/pdf",
            },
        }
    ]


def test_blank_mention_only_and_malformed_content_are_ignored():
    mentions = [{"key": "@bot", "id": {"open_id": "ou_bot"}}]
    assert (
        _adapter().normalize(
            _payload(chat_type="group", mentions=mentions, content={"text": " @bot "})
        )
        == []
    )
    payload = _payload()
    payload["event"]["message"]["content"] = "not-json"
    assert _adapter().normalize(payload) == []
