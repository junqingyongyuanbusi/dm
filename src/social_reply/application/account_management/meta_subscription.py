import httpx

_FACEBOOK_FIELDS = ("messages", "feed")
_INSTAGRAM_FIELDS = ("messages", "comments")


async def subscribe_meta_account(
    *,
    platform: str,
    access_token: str,
    external_account_id: str,
    instagram_login_mode: str,
    graph_base_url: str,
    api_version: str,
    enable_dm: bool,
    enable_comments: bool,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[str, ...]:
    """Install the app's configured webhook fields for one authorized account."""
    fields = _subscription_fields(
        platform=platform,
        enable_dm=enable_dm,
        enable_comments=enable_comments,
    )
    if not fields:
        return ()
    base_url = (
        "https://graph.instagram.com"
        if platform == "instagram" and instagram_login_mode == "instagram_login"
        else graph_base_url
    )
    endpoint = (
        "/me/subscribed_apps"
        if platform == "instagram" and instagram_login_mode == "instagram_login"
        else f"/{external_account_id}/subscribed_apps"
    )
    async with httpx.AsyncClient(
        base_url=f"{base_url.rstrip('/')}/{api_version.strip('/')}",
        timeout=15,
        transport=transport,
        headers={"Authorization": f"Bearer {access_token}"},
    ) as client:
        response = await client.post(
            endpoint,
            params={"subscribed_fields": ",".join(fields)},
        )
        response.raise_for_status()
        result = response.json()
    if result.get("success") is not True:
        raise RuntimeError(f"meta_subscription_failed:{result}")
    return fields


def _subscription_fields(
    *, platform: str, enable_dm: bool, enable_comments: bool
) -> tuple[str, ...]:
    available = _FACEBOOK_FIELDS if platform == "facebook" else _INSTAGRAM_FIELDS
    wanted = []
    for field in available:
        if field == "messages" and enable_dm:
            wanted.append(field)
        elif field in {"feed", "comments"} and enable_comments:
            wanted.append(field)
    return tuple(wanted)
