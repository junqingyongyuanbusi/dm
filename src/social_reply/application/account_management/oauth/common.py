"""Short-lived OAuth state stored encrypted in Redis.

The random state value sent through the browser is only a lookup key. Sensitive
values such as request-token secrets and account tokens are encrypted with the
same application key ring used for persisted platform credentials. States are
consumed atomically and expire after ten minutes.
"""

import json
from collections.abc import Mapping
from typing import Any

import redis.asyncio as aioredis
from fastapi.responses import HTMLResponse

from social_reply.application.account_management.admin import _page, html
from social_reply.application.account_management.auth import Principal, principal_from_session_id
from social_reply.infrastructure.secret_crypto import decrypt_secret_bundle, encrypt_secret_bundle
from social_reply.shared.config import get_settings

STATE_TTL_SECONDS = 600


def admin_callback_url(path: str) -> str:
    return f"{get_settings().public_base_url.rstrip('/')}{path}"


def oauth_redis():
    return aioredis.from_url(get_settings().redis_url)


async def store_oauth_state(namespace: str, key: str, payload: Mapping[str, Any]) -> None:
    encrypted = encrypt_secret_bundle(
        {"payload": json.dumps(dict(payload), separators=(",", ":"))}
    )
    if encrypted is None:
        raise ValueError("oauth_state_encryption_failed")
    redis = oauth_redis()
    try:
        await redis.set(
            f"oauth:{namespace}:{key}",
            json.dumps(encrypted, separators=(",", ":")),
            ex=STATE_TTL_SECONDS,
        )
    finally:
        await redis.aclose()


async def take_oauth_state(namespace: str, key: str) -> dict[str, Any] | None:
    if not key:
        return None
    redis = oauth_redis()
    redis_key = f"oauth:{namespace}:{key}"
    try:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.get(redis_key)
            pipe.delete(redis_key)
            value, _deleted = await pipe.execute()
    finally:
        await redis.aclose()
    if value is None:
        return None
    try:
        envelope = json.loads(value)
        if not isinstance(envelope, dict):
            return None
        decrypted = decrypt_secret_bundle(envelope)
        payload = decrypted.get("payload")
        return json.loads(payload) if isinstance(payload, str) else None
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


async def principal_from_oauth_context(context: Mapping[str, Any]) -> Principal | None:
    principal = await principal_from_session_id(context.get("session_id"))
    if principal is None or principal.must_change_password:
        return None
    tenant_id = context.get("tenant_id")
    if not isinstance(tenant_id, str) or tenant_id not in principal.allowed_tenants:
        return None
    return principal


def notice(title: str, message: str, *, status_code: int = 200) -> HTMLResponse:
    body = f"""<a class="back" href="/admin/accounts">← 返回账号页</a>
<section class="card"><h1 style="font-size:24px">{html.escape(title)}</h1>
<p>{html.escape(message)}</p></section>"""
    return HTMLResponse(_page(title, body, active="accounts"), status_code=status_code)
