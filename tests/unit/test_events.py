from social_reply.domain.messages.events import build_dm_conversation_key


def test_dm_conversation_key_format():
    # PLAN.md §七：普通私信 = platform + platform_account + external_user
    key = build_dm_conversation_key(
        platform="telegram", platform_account_id="acc-uuid", external_user_id="tg_123"
    )
    assert key == "telegram:acc-uuid:tg_123"
