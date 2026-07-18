import hashlib
import hmac

from social_reply.connectors.meta.adapter import MetaWebhookAdapter
from social_reply.connectors.meta.signature import (
    verify_meta_challenge,
    verify_meta_signature,
)
from social_reply.connectors.whatsapp.adapter import WhatsAppWebhookAdapter
from social_reply.connectors.x.adapter import XWebhookAdapter
from social_reply.connectors.x.signature import crc_response, verify_x_signature


def test_meta_signature_challenge_and_dm_normalization():
    body = b'{"object":"page"}'
    signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert verify_meta_signature(app_secret="secret", body=body, signature=signature)
    assert (
        verify_meta_challenge(
            verify_token="verify", mode="subscribe", token="verify", challenge="123"
        )
        == "123"
    )
    events = MetaWebhookAdapter().normalize(
        {
            "object": "page",
            "entry": [
                {
                    "id": "page-1",
                    "messaging": [
                        {
                            "sender": {"id": "user-1"},
                            "timestamp": 1784180000000,
                            "message": {"mid": "m-1", "text": "hello"},
                        }
                    ],
                }
            ],
        }
    )
    assert events[0].platform == "facebook"
    assert events[0].conversation_key == "facebook_dm:page-1:user-1"
    assert events[0].reply_target == {"kind": "dm", "recipient_id": "user-1"}


def test_meta_comment_conversation_key_is_thread_and_user_scoped():
    event = MetaWebhookAdapter(
        platform="instagram",
        account_id="account-uuid",
        external_account_id="ig-1",
    ).normalize(
        {
            "object": "instagram",
            "entry": [
                {
                    "id": "ig-1",
                    "changes": [
                        {
                            "field": "comments",
                            "value": {
                                "id": "comment-2",
                                "media_id": "media-1",
                                "parent_id": "comment-root",
                                "from": {"id": "user-1"},
                                "text": "hello",
                            },
                        }
                    ],
                }
            ],
        }
    )[0]
    assert event.conversation_key == ("instagram_comment:account-uuid:media-1:comment-root:user-1")


def test_x_self_echo_uses_external_account_id():
    events = XWebhookAdapter(account_id="account-uuid", external_account_id="x-bot").normalize(
        {
            "dm_events": [
                {"id": "echo", "sender_id": "x-bot", "message_create": {"text": "echo"}},
                {"id": "inbound", "sender_id": "user-1", "message_create": {"text": "hi"}},
            ]
        }
    )
    assert [event.external_event_id for event in events] == ["inbound"]


def test_whatsapp_normalization():
    events = WhatsAppWebhookAdapter(account_id="account-1", phone_number_id="phone-1").normalize(
        {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "metadata": {"phone_number_id": "phone-1"},
                                "messages": [
                                    {
                                        "id": "wamid-1",
                                        "from": "15551234567",
                                        "timestamp": "1784180000",
                                        "text": {"body": "hello"},
                                    }
                                ],
                            }
                        }
                    ]
                }
            ]
        }
    )
    assert events[0].platform_account_key == "account-1"
    assert events[0].conversation_key == "whatsapp:account-1:15551234567"
    assert events[0].reply_target == {
        "kind": "session_message",
        "phone_number_id": "phone-1",
        "to": "15551234567",
    }


def test_x_crc_signature_and_dm_normalization():
    token = crc_response(consumer_secret="secret", crc_token="challenge")
    assert token.startswith("sha256=")
    body = b'{"dm_events":[]}'
    signature = crc_response(consumer_secret="secret", crc_token=body.decode())
    expected = (
        "sha256="
        + __import__("base64")
        .b64encode(hmac.new(b"secret", body, hashlib.sha256).digest())
        .decode()
    )
    assert signature == expected
    assert verify_x_signature(consumer_secret="secret", body=body, signature=expected)
    events = XWebhookAdapter(account_id="bot-1").normalize(
        {
            "dm_events": [
                {
                    "id": "dm-1",
                    "sender_id": "user-1",
                    "message_create": {"text": "hello"},
                }
            ]
        }
    )
    assert events[0].conversation_key == "x_dm:bot-1:user-1"
    assert events[0].reply_target == {"kind": "dm", "participant_id": "user-1"}


def test_x_v2_direct_message_events_format():
    """X Account Activity v2 实际推送格式：direct_message_events + 嵌套 message_create。"""
    events = XWebhookAdapter(
        account_id="bot-1", external_account_id="1740258119773458432"
    ).normalize(
        {
            "for_user_id": "1740258119773458432",
            "direct_message_events": [
                {
                    "id": "dm-v2-1",
                    "type": "message_create",
                    "message_create": {
                        "target": {"recipient_id": "1740258119773458432"},
                        "sender_id": "2041798240056598528",
                        "message_data": {"text": "hi"},
                    },
                }
            ],
        }
    )
    assert len(events) == 1
    assert events[0].external_event_id == "dm-v2-1"
    assert events[0].external_user_id == "2041798240056598528"
    assert events[0].text == "hi"
    assert events[0].conversation_key == "x_dm:bot-1:2041798240056598528"


def test_x_v2_dm_from_self_is_ignored():
    """自己发出的 DM（sender == 账号自身）不应触发回复，避免自我回环。"""
    events = XWebhookAdapter(
        account_id="bot-1", external_account_id="1740258119773458432"
    ).normalize(
        {
            "direct_message_events": [
                {
                    "id": "dm-self",
                    "message_create": {
                        "sender_id": "1740258119773458432",
                        "message_data": {"text": "自己发的"},
                    },
                }
            ]
        }
    )
    assert events == []
