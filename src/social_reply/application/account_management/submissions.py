from dataclasses import dataclass
from typing import Any

from social_reply.domain.platform_accounts import AccountPlatform


@dataclass(frozen=True)
class PlatformSubmissionSpec:
    public_fields: frozenset[str]
    secret_fields: frozenset[str]

    def split(self, values: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        public = {
            key: value
            for key, value in values.items()
            if key in self.public_fields and key != "idempotency_key" and value is not None
        }
        secrets = {
            key: str(value)
            for key, value in values.items()
            if key in self.secret_fields and value is not None
        }
        return public, secrets


_COMMON_PUBLIC = frozenset({"public_id", "name", "automation_default", "idempotency_key"})
_META_APP_PUBLIC = frozenset(
    {"external_account_id", "app_id", "app_public_id", "app_name", "api_version"}
)

PLATFORM_SUBMISSIONS = {
    AccountPlatform.TELEGRAM.value: PlatformSubmissionSpec(
        public_fields=_COMMON_PUBLIC | {"drop_pending_updates", "rotate_webhook_secret"},
        secret_fields=frozenset({"token"}),
    ),
    AccountPlatform.FACEBOOK.value: PlatformSubmissionSpec(
        public_fields=_COMMON_PUBLIC | _META_APP_PUBLIC | {"enable_dm", "enable_comments"},
        secret_fields=frozenset({"access_token", "app_secret", "verify_token"}),
    ),
    AccountPlatform.INSTAGRAM.value: PlatformSubmissionSpec(
        public_fields=_COMMON_PUBLIC
        | _META_APP_PUBLIC
        | {"enable_dm", "enable_comments", "instagram_login_mode", "page_id"},
        secret_fields=frozenset({"access_token", "app_secret", "verify_token"}),
    ),
    AccountPlatform.WHATSAPP.value: PlatformSubmissionSpec(
        public_fields=_COMMON_PUBLIC | _META_APP_PUBLIC,
        secret_fields=frozenset({"access_token", "app_secret", "verify_token"}),
    ),
    AccountPlatform.X.value: PlatformSubmissionSpec(
        public_fields=_COMMON_PUBLIC | {"environment"},
        secret_fields=frozenset(
            {
                "consumer_key",
                "consumer_secret",
                "access_token",
                "access_token_secret",
                "xchat_pin",
            }
        ),
    ),
}


def split_submission(
    platform: str, values: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, str]]:
    try:
        spec = PLATFORM_SUBMISSIONS[platform]
    except KeyError as exc:
        raise ValueError(f"unsupported_platform:{platform}") from exc
    return spec.split(values)
