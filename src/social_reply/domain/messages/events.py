def build_dm_conversation_key(
    *, platform: str, platform_account_id: str, external_user_id: str
) -> str:
    return f"{platform}:{platform_account_id}:{external_user_id}"
