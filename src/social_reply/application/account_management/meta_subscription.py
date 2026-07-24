import httpx

from social_reply.connectors.meta.client import appsecret_proof

_FACEBOOK_FIELDS = ("messages", "feed")
_INSTAGRAM_FIELDS = ("messages", "comments")


def _subscription_endpoint(
    *,
    platform: str,
    external_account_id: str,
    instagram_login_mode: str,
) -> str:
    return f"/{external_account_id}/subscribed_apps"


def _subscription_base_url(
    *,
    platform: str,
    instagram_login_mode: str,
    graph_base_url: str,
) -> str:
    if platform == "instagram" and instagram_login_mode == "instagram_login":
        return "https://graph.instagram.com"
    return graph_base_url


def _subscription_client(
    *,
    platform: str,
    access_token: str,
    app_secret: str,
    instagram_login_mode: str,
    graph_base_url: str,
    api_version: str,
    transport: httpx.AsyncBaseTransport | None,
) -> httpx.AsyncClient:
    base_url = _subscription_base_url(
        platform=platform,
        instagram_login_mode=instagram_login_mode,
        graph_base_url=graph_base_url,
    )
    return httpx.AsyncClient(
        base_url=f"{base_url.rstrip('/')}/{api_version.strip('/')}",
        timeout=15,
        transport=transport,
        headers={"Authorization": f"Bearer {access_token}"},
        params={"appsecret_proof": appsecret_proof(access_token, app_secret)},
    )


async def subscribe_meta_account(
    *,
    platform: str,
    access_token: str,
    app_secret: str,
    external_account_id: str,
    instagram_login_mode: str,
    graph_base_url: str,
    api_version: str,
    enable_dm: bool,
    enable_comments: bool,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[str, ...]:
    """Install the app's configured webhook fields for one authorized account."""
    fields = meta_subscription_fields(
        platform=platform,
        enable_dm=enable_dm,
        enable_comments=enable_comments,
    )
    if not fields:
        return ()
    endpoint = _subscription_endpoint(
        platform=platform,
        external_account_id=external_account_id,
        instagram_login_mode=instagram_login_mode,
    )
    async with _subscription_client(
        platform=platform,
        access_token=access_token,
        app_secret=app_secret,
        instagram_login_mode=instagram_login_mode,
        graph_base_url=graph_base_url,
        api_version=api_version,
        transport=transport,
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


async def get_meta_subscription_fields(
    *,
    platform: str,
    access_token: str,
    app_secret: str,
    app_id: str | None,
    external_account_id: str,
    instagram_login_mode: str,
    graph_base_url: str,
    api_version: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[str, ...]:
    endpoint = _subscription_endpoint(
        platform=platform,
        external_account_id=external_account_id,
        instagram_login_mode=instagram_login_mode,
    )
    async with _subscription_client(
        platform=platform,
        access_token=access_token,
        app_secret=app_secret,
        instagram_login_mode=instagram_login_mode,
        graph_base_url=graph_base_url,
        api_version=api_version,
        transport=transport,
    ) as client:
        response = await client.get(endpoint, params={"fields": "subscribed_fields"})
        response.raise_for_status()
        payload = response.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return ()
    selected = [item for item in data if isinstance(item, dict)]
    if app_id:
        selected = [item for item in selected if str(item.get("id") or "") == app_id]
    elif len(selected) > 1:
        return ()
    fields: set[str] = set()
    for item in selected:
        subscribed = item.get("subscribed_fields")
        if isinstance(subscribed, list):
            fields.update(str(field) for field in subscribed if isinstance(field, str))
    return tuple(sorted(fields))


def meta_subscription_fields(
    *,
    platform: str,
    enable_dm: bool,
    enable_comments: bool,
) -> tuple[str, ...]:
    available = _FACEBOOK_FIELDS if platform == "facebook" else _INSTAGRAM_FIELDS
    wanted = []
    for field in available:
        if field == "messages" and enable_dm:
            wanted.append(field)
        elif field in {"feed", "comments"} and enable_comments:
            wanted.append(field)
    return tuple(wanted)
