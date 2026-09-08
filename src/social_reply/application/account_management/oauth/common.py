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
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlencode

import redis.asyncio as aioredis
from fastapi import HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from social_reply.application.account_management.access import require_reauthorization
from social_reply.application.account_management.admin import _page, html
from social_reply.application.account_management.auth import Principal, principal_from_session_id
from social_reply.application.account_management.ui_i18n import translate
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.secret_crypto import decrypt_secret_bundle, encrypt_secret_bundle
from social_reply.shared.config import get_settings

STATE_TTL_SECONDS = 600
OAUTH_CONTEXT_VERSION = 1
_CONTEXT_MAX_AGE = timedelta(seconds=STATE_TTL_SECONDS + 60)
_ADMIN_RETURN_PATHS = {
    "/admin/accounts",
    "/admin/integrations/accounts",
}

_OAUTH_CONTEXT_CANONICAL_KEYS = frozenset(
    {
        "context_version",
        "provider",
        "platform",
        "tenant_id",
        "initiator_user_id",
        "initiator_session_id",
        "surface",
        "return_to",
        "issued_at",
        "nonce",
        "operation",
        "target_account_id",
        "expected_config_version",
        "target_external_account_id",
    }
)
_OAUTH_TARGET_PROVIDERS = frozenset({"x", "facebook", "instagram"})

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
    operation: str = "CONNECT_ACCOUNT",
    target_account_id: uuid.UUID | str | None = None,
    expected_config_version: int | None = None,
    target_external_account_id: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if surface not in {"admin", "channels"}:
        raise ValueError("invalid_oauth_surface")
    if provider not in _OAUTH_TARGET_PROVIDERS:
        raise ValueError("invalid_oauth_provider")
    if tenant_id not in principal.allowed_tenants:
        raise ValueError("oauth_tenant_not_allowed")
    if operation not in {"CONNECT_ACCOUNT", "REAUTHORIZE"}:
        raise ValueError("invalid_oauth_operation")
    if operation == "REAUTHORIZE" and target_account_id is None:
        raise ValueError("oauth_reauthorization_target_required")
    if target_account_id is None and expected_config_version is not None:
        raise ValueError("oauth_expected_version_without_target")
    if expected_config_version is not None and expected_config_version < 1:
        raise ValueError("invalid_oauth_expected_config_version")
    if target_external_account_id is not None and not str(target_external_account_id).strip():
        raise ValueError("invalid_oauth_target_external_id")
    if extra:
        reserved = _OAUTH_CONTEXT_CANONICAL_KEYS.intersection(extra)
        if reserved:
            raise ValueError("oauth_context_reserved_field")
    target_id = str(target_account_id) if target_account_id is not None else None
    context = {
        "context_version": OAUTH_CONTEXT_VERSION,
        "provider": provider,
        "platform": provider,
        "tenant_id": tenant_id,
        "initiator_user_id": (str(principal.user_id) if principal.user_id is not None else None),
        "initiator_session_id": str(principal.session_id),
        "surface": surface,
        "return_to": safe_oauth_return_to(
            surface=surface,
            tenant_id=tenant_id,
            return_to=return_to,
        ),
        "issued_at": datetime.now(UTC).isoformat(),
        "nonce": secrets.token_urlsafe(24),
        "operation": operation,
        "target_account_id": target_id,
        "expected_config_version": expected_config_version,
        "target_external_account_id": (
            str(target_external_account_id) if target_external_account_id is not None else None
        ),
    }
    if extra:
        context.update(dict(extra))
    return context


async def resolve_oauth_target(
    request: Request,
    *,
    principal: Principal,
    provider: str,
    tenant_id: str,
) -> dict[str, Any]:
    """Bind an optional browser target to a live account and its version."""
    raw_target = (request.query_params.get("target_account_id") or "").strip()
    raw_version = (request.query_params.get("expected_config_version") or "").strip()
    if not raw_target:
        if raw_version:
            raise HTTPException(status_code=422, detail="expected_config_version_requires_target")
        return {
            "operation": "CONNECT_ACCOUNT",
            "target_account_id": None,
            "expected_config_version": None,
            "target_external_account_id": None,
            "brand_id": None,
        }
    try:
        target_account_id = uuid.UUID(raw_target)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="target_account_id_invalid") from exc
    if provider not in _OAUTH_TARGET_PROVIDERS:
        raise HTTPException(status_code=422, detail="target_provider_invalid")
    if raw_version:
        try:
            expected_config_version = int(raw_version)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="expected_config_version_invalid") from exc
        if expected_config_version < 1:
            raise HTTPException(status_code=422, detail="expected_config_version_invalid")
    else:
        expected_config_version = None
    async with get_session_factory()() as session:
        account = (
            await session.execute(
                select(models.PlatformAccount)
                .where(models.PlatformAccount.id == target_account_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if account is None or account.tenant_id != tenant_id:
            raise HTTPException(status_code=404, detail="target_account_not_found")
        if account.platform != provider:
            raise HTTPException(status_code=409, detail="target_provider_mismatch")
        if not account.external_account_id:
            raise HTTPException(status_code=409, detail="target_account_identity_missing")
        snapshot_version = expected_config_version or account.config_version
        await require_reauthorization(
            session,
            principal=principal,
            account=account,
            expected_config_version=snapshot_version,
        )
        await session.commit()
        return {
            "operation": "REAUTHORIZE",
            "target_account_id": str(account.id),
            "expected_config_version": snapshot_version,
            "target_external_account_id": account.external_account_id,
            "brand_id": account.brand_id,
        }

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


def oauth_provisioning_error_response(
    context: Mapping[str, Any],
    exc: Exception,
) -> Response:
    error = str(exc)
    if isinstance(exc, PermissionError) and error in {
        "account_reauthorization_denied",
        "reauthorization_requires_session",
    }:
        code, status_code, message = (
            "account_reauthorization_denied",
            403,
            "当前会话无权重新授权该平台账号。",
        )
    elif error == "account_reauthorization_version_conflict":
        code, status_code, message = (
            "account_version_conflict",
            409,
            "平台账号配置已变化，请重新开始授权。",
        )
    elif error == "platform_account_target_mismatch":
        code, status_code, message = (
            "oauth_target_identity_mismatch",
            409,
            "授权账号与目标平台账号不一致。",
        )
    elif error == "platform_account_target_not_found":
        code, status_code, message = (
            "target_account_not_found",
            404,
            "目标平台账号不存在。",
        )
    elif isinstance(exc, PermissionError) and error in {
        "initiator_session_invalid",
        "admin_session_invalid",
    }:
        code, status_code, message = (
            "initiator_session_invalid",
            403,
            "授权发起会话已失效。",
        )
    else:
        code, status_code, message = (
            "provisioning_submit_failed",
            409,
            "授权请求已失效，请重新开始。",
        )
    return oauth_error_response(
        surface=str(context.get("surface") or "admin"),
        tenant_id=str(context.get("tenant_id") or ""),
        provider=str(context.get("provider") or context.get("platform") or "oauth"),
        code=code,
        title=translate("oauth.cannot_complete.title"),
        message=message,
        status_code=status_code,
    )

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
    surface = context.get("surface", "admin")
    if surface == "admin" and not principal.is_workspace_admin:
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
        expected_user_id = str(principal.user_id) if principal.user_id is not None else None
        if context.get("initiator_user_id") != expected_user_id:
            return None
        if surface not in {"admin", "channels"}:
            return None
        if context.get("provider") not in {"x", "facebook", "instagram"}:
            return None
        operation = context.get("operation", "CONNECT_ACCOUNT")
        target_account_id = context.get("target_account_id")
        expected_config_version = context.get("expected_config_version")
        target_external_account_id = context.get("target_external_account_id")
        if operation == "REAUTHORIZE":
            try:
                uuid.UUID(str(target_account_id))
                if int(expected_config_version) < 1:
                    return None
            except (TypeError, ValueError):
                return None
            if not isinstance(target_external_account_id, str) or not target_external_account_id:
                return None
        elif operation != "CONNECT_ACCOUNT":
            return None
        elif any(
            value is not None
            for value in (target_account_id, expected_config_version, target_external_account_id)
        ):
            return None
    return principal


def notice(title: str, message: str, *, status_code: int = 200) -> HTMLResponse:
    back_label = translate("oauth.back_to_accounts")
    body = f"""<a class="back" href="/admin/integrations/accounts">← {back_label}</a>
<header><h1>{html.escape(title)}</h1></header>
<section class="card"><p>{html.escape(message)}</p></section>"""
    return HTMLResponse(_page(title, body, active="accounts"), status_code=status_code)
