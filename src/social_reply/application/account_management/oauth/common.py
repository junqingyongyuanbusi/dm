"""Short-lived OAuth state stored encrypted in Redis.

The random state value sent through the browser is only a lookup input. X OAuth
transactions use its SHA-256 digest as the Redis key; sensitive values such as
request-token secrets are encrypted with the same application key ring used for
persisted platform credentials. States are consumed atomically and expire after
ten minutes.
"""

import hashlib
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


def oauth_state_key(namespace: str, key: str) -> str:
    if namespace == "x":
        digest = hashlib.sha256(key.encode()).hexdigest()
        return f"x:oauth1:transaction:{digest}"
    return f"oauth:{namespace}:{key}"


def _oauth_state_lookup_keys(namespace: str, key: str) -> tuple[str, ...]:
    primary = oauth_state_key(namespace, key)
    if namespace == "x":
        # Consume transactions created immediately before a rolling deploy.
        return primary, f"oauth:{namespace}:{key}"
    return (primary,)


def _oauth_state_write_key(namespace: str, key: str) -> str:
    if namespace == "x" and get_settings().x_oauth_legacy_state_write:
        return f"oauth:{namespace}:{key}"
    return oauth_state_key(namespace, key)


async def store_oauth_state(namespace: str, key: str, payload: Mapping[str, Any]) -> None:
    encrypted = encrypt_secret_bundle({"payload": json.dumps(dict(payload), separators=(",", ":"))})
    if encrypted is None:
        raise ValueError("oauth_state_encryption_failed")
    redis = oauth_redis()
    try:
        await redis.set(
            _oauth_state_write_key(namespace, key),
            json.dumps(encrypted, separators=(",", ":")),
            ex=STATE_TTL_SECONDS,
        )
    finally:
        await redis.aclose()


def _decode_oauth_state(value: bytes | str | None) -> dict[str, Any] | None:
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


async def peek_oauth_state(namespace: str, key: str) -> dict[str, Any] | None:
    if not key:
        return None
    redis = oauth_redis()
    redis_keys = _oauth_state_lookup_keys(namespace, key)
    try:
        async with redis.pipeline(transaction=True) as pipe:
            for redis_key in redis_keys:
                pipe.get(redis_key)
            values = await pipe.execute()
    finally:
        await redis.aclose()
    return next(
        (payload for value in values if (payload := _decode_oauth_state(value)) is not None),
        None,
    )


async def take_oauth_state(namespace: str, key: str) -> dict[str, Any] | None:
    if not key:
        return None
    redis = oauth_redis()
    redis_keys = _oauth_state_lookup_keys(namespace, key)
    try:
        async with redis.pipeline(transaction=True) as pipe:
            for redis_key in redis_keys:
                pipe.get(redis_key)
                pipe.delete(redis_key)
            results = await pipe.execute()
    finally:
        await redis.aclose()
    return next(
        (payload for value in results[0::2] if (payload := _decode_oauth_state(value)) is not None),
        None,
    )


async def principal_from_oauth_context(context: Mapping[str, Any]) -> Principal | None:
    session_id = context.get("admin_session_id") or context.get("session_id")
    principal = await principal_from_session_id(session_id)
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
