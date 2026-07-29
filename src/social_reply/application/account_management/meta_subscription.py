from dataclasses import dataclass

import httpx

from social_reply.connectors.meta.client import appsecret_proof

_FACEBOOK_FIELDS = ("messages", "feed")
_INSTAGRAM_FIELDS = ("messages", "comments")

# Meta 只在「App 级 Webhooks 产品」和「账号级订阅」都列出某个字段时才投递事件。
# App 级订阅挂在 Meta App 上，即使账号流量走 graph.instagram.com 也不例外。
_APP_SUBSCRIPTION_BASE_URL = "https://graph.facebook.com"
_APP_SUBSCRIPTION_OBJECTS = {
    "facebook": "page",
    "instagram": "instagram",
    "whatsapp": "whatsapp_business_account",
}


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
        instagram_login_mode=instagram_login_mode,
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
    instagram_login_mode: str = "facebook_login",
) -> tuple[str, ...]:
    available = _FACEBOOK_FIELDS if platform == "facebook" else _INSTAGRAM_FIELDS
    # facebook_login 下 Instagram 的订阅写在关联 Page 上，而 Page 的 subscribed_fields
    # 没有 comments（只有 feed/mention 那一套），提交会被 Graph 直接拒掉。
    # 这种模式下 IG 评论靠 App 级 instagram 对象的 comments 字段投递。
    if platform == "instagram" and instagram_login_mode == "facebook_login":
        available = tuple(field for field in available if field != "comments")
    wanted = []
    for field in available:
        if field == "messages" and enable_dm:
            wanted.append(field)
        elif field in {"feed", "comments"} and enable_comments:
            wanted.append(field)
    return tuple(wanted)


def meta_app_subscription_object(platform: str) -> str:
    """Map a platform onto the webhook `object` its app-level subscription uses."""
    try:
        return _APP_SUBSCRIPTION_OBJECTS[platform]
    except KeyError:
        raise ValueError(f"unsupported_meta_platform:{platform}") from None


@dataclass(frozen=True)
class MetaAppSubscription:
    object_type: str
    callback_url: str
    active: bool
    fields: tuple[str, ...]


def _app_subscription_client(
    *,
    app_id: str,
    app_secret: str,
    api_version: str,
    transport: httpx.AsyncBaseTransport | None,
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=f"{_APP_SUBSCRIPTION_BASE_URL}/{api_version.strip('/')}",
        timeout=15,
        transport=transport,
        params={"access_token": f"{app_id}|{app_secret}"},
    )


async def get_meta_app_subscription(
    *,
    app_id: str,
    app_secret: str,
    object_type: str,
    api_version: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> MetaAppSubscription | None:
    """Read the app-level webhook subscription installed for one object, if any."""
    async with _app_subscription_client(
        app_id=app_id,
        app_secret=app_secret,
        api_version=api_version,
        transport=transport,
    ) as client:
        response = await client.get(f"/{app_id}/subscriptions")
        response.raise_for_status()
        payload = response.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return None
    for item in data:
        if not isinstance(item, dict) or item.get("object") != object_type:
            continue
        raw_fields = item.get("fields")
        fields = (
            tuple(
                sorted(
                    str(entry["name"])
                    for entry in raw_fields
                    if isinstance(entry, dict) and entry.get("name")
                )
            )
            if isinstance(raw_fields, list)
            else ()
        )
        return MetaAppSubscription(
            object_type=object_type,
            callback_url=str(item.get("callback_url") or ""),
            active=bool(item.get("active")),
            fields=fields,
        )
    return None


async def reconcile_meta_app_subscription(
    *,
    app_id: str,
    app_secret: str,
    object_type: str,
    desired_fields: tuple[str, ...],
    callback_url: str,
    verify_token: str,
    api_version: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[str, ...]:
    """Install the app-level webhook fields Meta needs before it delivers anything.

    An app-level subscription whose callback URL is registered but whose field list is
    empty looks healthy in the App Dashboard yet drops every event, so reconcile on the
    field set rather than on the subscription's existence.
    """
    if not desired_fields:
        return ()
    current = await get_meta_app_subscription(
        app_id=app_id,
        app_secret=app_secret,
        object_type=object_type,
        api_version=api_version,
        transport=transport,
    )
    if (
        current is not None
        and current.active
        and current.callback_url == callback_url
        and set(desired_fields).issubset(current.fields)
    ):
        return current.fields
    # POST replaces the whole object subscription, so union the desired fields with what
    # is already installed; another account on this app may depend on the extras.
    merged = tuple(sorted(set(desired_fields) | set(current.fields if current else ())))
    async with _app_subscription_client(
        app_id=app_id,
        app_secret=app_secret,
        api_version=api_version,
        transport=transport,
    ) as client:
        response = await client.post(
            f"/{app_id}/subscriptions",
            data={
                "object": object_type,
                "callback_url": callback_url,
                "fields": ",".join(merged),
                "verify_token": verify_token,
                "include_values": "true",
            },
        )
        response.raise_for_status()
        result = response.json()
    if result.get("success") is not True:
        raise RuntimeError(f"meta_app_subscription_failed:{result}")
    return merged
