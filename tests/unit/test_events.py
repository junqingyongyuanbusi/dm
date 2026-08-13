from social_reply.domain.messages.events import build_dm_conversation_key


def test_dm_conversation_key_format():
    key = build_dm_conversation_key(
        platform="telegram", platform_account_id="acc-uuid", external_user_id="tg_123"
    )
    assert key == "telegram:acc-uuid:tg_123"
