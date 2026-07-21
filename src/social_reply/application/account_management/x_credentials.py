from social_reply.application.platform_accounts import PlatformAccountRuntime
from social_reply.shared.config import get_settings


def x_credentials(account: PlatformAccountRuntime) -> dict[str, str]:
    credentials = account.credential_bundle
    configured = get_settings().x_app_credentials
    stored = (
        credentials.get("consumer_key", ""),
        credentials.get("consumer_secret", ""),
    )
    if configured is not None and (not all(stored) or configured[0] == stored[0]):
        consumer_key, consumer_secret = configured
    else:
        consumer_key, consumer_secret = stored
    if not consumer_key or not consumer_secret:
        raise ValueError("x_app_credentials_missing")
    return {
        **credentials,
        "consumer_key": consumer_key,
        "consumer_secret": consumer_secret,
    }
