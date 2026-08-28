"""Short-lived OAuth state stored encrypted in Redis.

The random state value sent through the browser is only a lookup input. X OAuth
transactions use its SHA-256 digest as the Redis key; sensitive values such as
request-token secrets are encrypted with the same application key ring used for
persisted platform credentials. States are consumed atomically and expire after
ten minutes.
"""

import hashlib
import json
import secrets
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlencode

import redis.asyncio as aioredis
from fastapi import Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from social_reply.application.account_management.admin import _page, html
from social_reply.application.account_management.auth import Principal, principal_from_session_id
from social_reply.infrastructure.secret_crypto import decrypt_secret_bundle, encrypt_secret_bundle
from social_reply.shared.config import get_settings

STATE_TTL_SECONDS = 600
OAUTH_CONTEXT_VERSION = 1
_CONTEXT_MAX_AGE = timedelta(seconds=STATE_TTL_SECONDS + 60)
_ADMIN_RETURN_PATHS = {
    "/admin/accounts",
    "/admin/integrations/accounts",
}


def admin_callback_url(path: str) -> str:
    return f"{get_settings().public_base_url.rstrip('/')}{path}"


def safe_oauth_return_to(
    *,
    surface: str,
    tenant_id: str,
    return_to: object = None,
) -> str:
    candidate = str(return_to or "")
    channels_path = f"/app/t/{quote(tenant_id, safe='')}/channels"
    if surface == "channels":
        return channels_path if candidate in {"", channels_path} else channels_path
    return candidate if candidate in _ADMIN_RETURN_PATHS else "/admin/accounts"


def build_oauth_context(
    *,
    principal: Principal,
    provider: str,
    tenant_id: str,
    surface: str,
    return_to: object = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if surface not in {"admin", "channels"}:
        raise ValueError("invalid_oauth_surface")
    if tenant_id not in principal.allowed_tenants:
        raise ValueError("oauth_tenant_not_allowed")
    context = {
        "context_version": OAUTH_CONTEXT_VERSION,
        "provider": provider,
        "tenant_id": tenant_id,
        "initiator_user_id": (
            str(principal.user_id) if principal.user_id is not None else None
        ),
        "initiator_session_id": str(principal.session_id),
        "surface": surface,
        "return_to": safe_oauth_return_to(
            surface=surface,
            tenant_id=tenant_id,
            return_to=return_to,
        ),
        "issued_at": datetime.now(UTC).isoformat(),
        "nonce": secrets.token_urlsafe(24),
    }
    if extra:
        context.update(dict(extra))
    return context


def oauth_result_response(
    context: Mapping[str, Any],
    *,
    provider: str,
    status_value: str,
    code: str | None = None,
) -> RedirectResponse:
    tenant_id = str(context.get("tenant_id") or "")
    surface = str(context.get("surface") or "admin")
    return_to = safe_oauth_return_to(
        surface=surface,
        tenant_id=tenant_id,
        return_to=context.get("return_to"),
    )
    query = {"provider": provider, "status": status_value}
    if code:
        query["code"] = code[:64]
    return RedirectResponse(
        f"{return_to}?{urlencode(query)}",
        status_code=status.HTTP_303_SEE_OTHER,
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
        },
    )


def oauth_error_response(
    *,
    surface: str,
    tenant_id: str,
    provider: str,
    code: str,
    title: str,
    message: str,
    status_code: int,
) -> Response:
    if surface == "channels":
        return oauth_result_response(
            {
                "surface": surface,
                "tenant_id": tenant_id,
                "return_to": safe_oauth_return_to(
                    surface=surface,
                    tenant_id=tenant_id,
                ),
            },
            provider=provider,
            status_value="error",
            code=code,
        )
    return notice(title, message, status_code=status_code)


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
    session_id = (
        context.get("initiator_session_id")
        or context.get("admin_session_id")
        or context.get("session_id")
    )
    principal = await principal_from_session_id(session_id)
    if principal is None or principal.must_change_password:
        return None
    tenant_id = context.get("tenant_id")
    if not isinstance(tenant_id, str) or tenant_id not in principal.allowed_tenants:
        return None
    if context.get("context_version") is not None:
        if context.get("context_version") != OAUTH_CONTEXT_VERSION:
            return None
        if not context.get("nonce"):
            return None
        issued_at = context.get("issued_at")
        try:
            issued = datetime.fromisoformat(str(issued_at).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if issued.tzinfo is None:
            issued = issued.replace(tzinfo=UTC)
        age = datetime.now(UTC) - issued
        if not timedelta(seconds=-60) <= age <= _CONTEXT_MAX_AGE:
            return None
        expected_user_id = (
            str(principal.user_id) if principal.user_id is not None else None
        )
        if context.get("initiator_user_id") != expected_user_id:
            return None
        if context.get("surface") not in {"admin", "channels"}:
            return None
        if context.get("provider") not in {"x", "facebook", "instagram"}:
            return None
    return principal


def notice(title: str, message: str, *, status_code: int = 200) -> HTMLResponse:
    body = f"""<a class="back" href="/admin/integrations/accounts">← 返回平台账号</a>
<section class="card"><h1 style="font-size:24px">{html.escape(title)}</h1>
<p>{html.escape(message)}</p></section>"""
    return HTMLResponse(_page(title, body, active="accounts"), status_code=status_code)
