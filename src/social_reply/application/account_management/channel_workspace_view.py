"""Pure, scoped presentation for the channel workspace; never grants account access."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import quote, urlencode

from social_reply.application.account_management.channel_workspace_i18n import (
    CONNECTION_LABELS,
    channel_copy,
)
from social_reply.application.account_management.templating import render_template, trusted_html
from social_reply.domain.platform_accounts import (
    PLATFORM_CAPABILITY_SPECS,
    SUPPORTED_ACCOUNT_PLATFORMS,
)
from social_reply.infrastructure.database.models import PlatformAccount

CHANNEL_QUERY_MAX_LENGTH = 120
PLATFORM_LABELS = (
    ("facebook", "Facebook"),
    ("instagram", "Instagram"),
    ("telegram", "Telegram"),
    ("x", "X"),
    ("whatsapp", "WhatsApp"),
    ("email", "邮件"),
    ("feishu", "飞书"),
)
CONNECTION_TONES = {
    "connected": "success",
    "limited": "warning",
    "error": "danger",
    "pending": "neutral",
    "disabled": "neutral",
}
HEALTH_CONFIG_KEYS = {
    "facebook": "meta_health_status",
    "instagram": "meta_health_status",
    "email": "email_health_status",
    "feishu": "feishu_health_status",
}
CAPABILITY_KEYS = ("dm", "comments", "session_messages", "templates", "x_chat", "mentions")
AUTOMATION_STATES = frozenset({
    "BOT_DRAFT_ONLY", "BOT_ACTIVE", "HANDOFF_PENDING", "HUMAN_ACTIVE", "BOT_COOLDOWN", "CLOSED",
})


@dataclass(frozen=True)
class ChannelFilters:
    query: str = ""
    platform: str = "all"
    channel_status: str = "all"

    def __post_init__(self) -> None:
        if len(self.query) > CHANNEL_QUERY_MAX_LENGTH:
            raise ValueError("channel_query_too_long")
        if len(self.platform) > 16 or self.platform not in {"all", *SUPPORTED_ACCOUNT_PLATFORMS}:
            raise ValueError("invalid_channel_platform")
        if len(self.channel_status) > 20 or self.channel_status not in {"all", *CONNECTION_TONES}:
            raise ValueError("invalid_channel_status")

    @classmethod
    def from_query(cls, query: Mapping[str, str]) -> "ChannelFilters":
        raw_query = query.get("q", "")
        if len(raw_query) > CHANNEL_QUERY_MAX_LENGTH:
            raise ValueError("channel_query_too_long")
        return cls(
            query=raw_query.strip(),
            platform=query.get("platform", "all"),
            channel_status=query.get("channel_status", "all"),
        )

    def matches(self, account: "ChannelAccountView") -> bool:
        identity = f"{account.name} {account.identity}".casefold()
        return (
            (self.platform == "all" or self.platform == account.platform)
            and (self.channel_status == "all" or self.channel_status == account.connection_status)
            and self.query.casefold() in identity
        )


@dataclass(frozen=True)
class ChannelAccountView:
    account_id: str
    name: str
    identity: str
    platform: str
    platform_label: str
    brand_id: str
    owner_name: str
    capabilities: tuple[tuple[str, bool], ...]
    connection_status: str
    health_label: str
    health_tone: str
    automation_label: str
    draft_only: bool
    kill_switch_enabled: bool


def channel_connection_status(account: PlatformAccount) -> str:
    if account.status != "active":
        return "disabled"
    health_key = HEALTH_CONFIG_KEYS.get(account.platform)
    health_status = (account.config or {}).get(health_key) if health_key else None
    if health_status in (
        "ERROR", "REAUTH_REQUIRED", "SUBSCRIPTION_MISSING", "APP_SUBSCRIPTION_MISSING",
        "CREDENTIAL_INVALID", "BOT_NOT_ACTIVE", "BOT_ID_MISMATCH",
    ):
        return "error"
    capability = account.capability or {}
    messaging_keys = ("dm", "x_chat") if account.platform == "x" else ("dm",)
    if all(capability.get(key) is False for key in messaging_keys):
        return "limited"
    # READY describes the saved platform probe, not live authorization or send eligibility.
    if health_status == "READY":
        return "connected"
    return "pending"


def build_channel_account_view(
    account: PlatformAccount, *, owner_name: str | None, kill_switch_enabled: bool
) -> ChannelAccountView:
    connection_status = channel_connection_status(account)
    specification = PLATFORM_CAPABILITY_SPECS.get(account.platform)
    known_keys = specification.boolean_keys if specification else frozenset()
    capability = account.capability or {}
    capabilities = tuple(
        (channel_copy(key), capability[key])
        for key in CAPABILITY_KEYS
        if key in known_keys and isinstance(capability.get(key), bool)
    )
    owner_label = owner_name or channel_copy(
        "organization_account" if account.owner_user_id is None else "owner_unavailable"
    )
    return ChannelAccountView(
        account_id=str(account.id),
        name=account.name,
        identity=(
            account.provider_username or account.external_account_id
            or channel_copy("identity_unavailable")
        ),
        platform=account.platform,
        platform_label=(
            channel_copy(account.platform) if account.platform in {"email", "feishu"}
            else dict(PLATFORM_LABELS).get(account.platform, channel_copy("unknown_platform"))
        ),
        brand_id=account.brand_id,
        owner_name=owner_label,
        capabilities=capabilities,
        connection_status=connection_status,
        health_label=CONNECTION_LABELS[connection_status],
        health_tone=CONNECTION_TONES[connection_status],
        automation_label=channel_copy(
            account.automation_default
            if account.automation_default in AUTOMATION_STATES else "automation_unknown"
        ),
        draft_only=account.automation_default == "BOT_DRAFT_ONLY",
        kill_switch_enabled=kill_switch_enabled,
    )


def render_channel_card(account: ChannelAccountView, *, tenant_id: str) -> str:
    return render_template(
        "tenant/channel_workspace.html",
        copy=channel_copy,
        card_only=account,
        root=f"/app/t/{quote(tenant_id, safe='')}",
    )


def render_channel_workspace(
    *,
    accounts: Sequence[ChannelAccountView],
    tenant_id: str,
    filters: ChannelFilters,
    brand_id: str,
    requested_brand_id: str,
    can_connect: bool,
    provider_html: str,
    banner_html: str,
    maintenance_html: str,
    jobs_html: str,
    dialogs_html: str,
    pending_job_count: int,
) -> str:
    root = f"/app/t/{quote(tenant_id, safe='')}"
    filtered_accounts = tuple(account for account in accounts if filters.matches(account))
    brand_query = {"brand_id": brand_id} if requested_brand_id else {}
    metrics = (
        ("visible", channel_copy("visible"), len(accounts)),
        (
            "checked", channel_copy("connected"),
            sum(account.connection_status == "connected" for account in accounts),
        ),
        (
            "draft", channel_copy("draft"), sum(account.draft_only for account in accounts),
        ),
        (
            "blocked", channel_copy("blocked"),
            sum(account.kill_switch_enabled for account in accounts),
        ),
    )
    platform_links = tuple(
        {
            "value": value,
            "label": channel_copy(value) if value in {"email", "feishu"} else label,
            "count": sum(value == "all" or account.platform == value for account in accounts),
            "url": f"{root}/channels?{urlencode({
                'q': filters.query, 'platform': value,
                'channel_status': filters.channel_status, **brand_query,
            })}",
        }
        for value, label in (("all", channel_copy("all_platforms")), *PLATFORM_LABELS)
    )
    return render_template(
        "tenant/channel_workspace.html",
        copy=channel_copy,
        card_only=None,
        root=root,
        accounts=filtered_accounts,
        total_accounts=len(accounts),
        filters=filters,
        metrics=metrics,
        platform_links=platform_links,
        status_options=(
            ("all", channel_copy("all_statuses")),
            *CONNECTION_LABELS.items(),
        ),
        brand_id=brand_id,
        requested_brand_id=requested_brand_id,
        can_connect=can_connect,
        reset_url=f"{root}/channels" + (f"?{urlencode(brand_query)}" if brand_query else ""),
        provider_html=trusted_html(provider_html) if can_connect else "",
        banner_html=trusted_html(banner_html),
        maintenance_html=trusted_html(maintenance_html),
        jobs_html=trusted_html(jobs_html),
        dialogs_html=trusted_html(dialogs_html) if can_connect else "",
        pending_job_count=pending_job_count,
    )
