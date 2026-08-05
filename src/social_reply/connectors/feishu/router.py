import asyncio
import secrets
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from social_reply.application.event_ingestion.raw_recovery import (
    direct_dispatch_context,
    dispatch_initial_raw_event,
)
from social_reply.application.handoff_notifications.callbacks import (
    FeishuCardActionError,
    callback_request_digest,
    handle_feishu_card_action,
)
from social_reply.application.platform_accounts import find_platform_account_by_public_id
from social_reply.connectors.feishu.adapter import FeishuWebhookAdapter
from social_reply.connectors.feishu.contracts import nonblank_string_or_none
from social_reply.connectors.feishu.security import (
    FEISHU_MAX_BODY_BYTES,
    FeishuSecurityError,
    decrypt_payload,
    parse_json_object,
    verify_signature,
    verify_token,
)
from social_reply.domain.messages.canonical import canonical_event_to_dict
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.shared.config import get_settings

router = APIRouter()


async def _verified_payload(account, request: Request) -> tuple[dict[str, Any], bytes]:
    secrets_bundle = account.webhook_secret_bundle
    verification_token = secrets_bundle["verification_token"]
    encrypt_key = secrets_bundle["encrypt_key"]
    app_id = account.external_account_id or account.credential_bundle["app_id"]
    body = await _read_limited_body(request)
    envelope = parse_json_object(body)
    encrypted = envelope.get("encrypt")
    timestamp = request.headers.get("X-Lark-Request-Timestamp")
    nonce = request.headers.get("X-Lark-Request-Nonce")
    signature = request.headers.get("X-Lark-Signature")
    signature_headers = (timestamp, nonce, signature)
    signature_verified = False
    if isinstance(encrypted, str):
        if any(signature_headers):
            if not all(signature_headers):
                raise FeishuSecurityError()
            verify_signature(
                timestamp=timestamp,
                nonce=nonce,
                signature=signature,
                encrypt_key=encrypt_key,
                body=body,
            )
            signature_verified = True
        payload = decrypt_payload(encrypted, encrypt_key=encrypt_key)
    else:
        payload = envelope

    if _is_url_verification(payload):
        verify_token(payload.get("token"), expected=verification_token)
        return payload, body
    if not isinstance(encrypted, str) or not signature_verified:
        raise FeishuSecurityError()
    header = payload.get("header")
    if not isinstance(header, dict):
        raise FeishuSecurityError()
    verify_token(header.get("token"), expected=verification_token)
    header_app_id = header.get("app_id")
    if not isinstance(header_app_id, str) or not secrets.compare_digest(header_app_id, app_id):
        raise FeishuSecurityError()
    if nonblank_string_or_none(header.get("event_id")) is None:
        raise FeishuSecurityError()
    return payload, body


async def _account_payload(public_id: str, request: Request):
    account = await find_platform_account_by_public_id(platform="feishu", public_id=public_id)
    if account is None:
        raise HTTPException(status_code=404, detail="feishu_account_not_found")
    try:
        payload, body = await _verified_payload(account, request)
    except FeishuSecurityError as exc:
        status_code = 413 if exc.code == "feishu_request_too_large" else 401
        detail = "feishu_request_too_large" if status_code == 413 else "invalid_feishu_request"
        raise HTTPException(status_code=status_code, detail=detail) from None
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=404, detail="feishu_account_not_found") from None
    return account, payload, body


async def _card_action_response(
    account,
    payload: dict[str, Any],
    body: bytes,
    *,
    feature_enabled: bool,
) -> JSONResponse:
    header = payload.get("header")
    if not isinstance(header, dict) or header.get("event_type") != "card.action.trigger":
        return JSONResponse(
            {"toast": {"type": "error", "content": "不支持的飞书卡片回调"}}
        )
    provider_event_id = nonblank_string_or_none(header.get("event_id"))
    if provider_event_id is None:
        raise HTTPException(status_code=401, detail="invalid_feishu_request")
    try:
        async with asyncio.timeout(2.5):
            response = await handle_feishu_card_action(
                account_id=account.id,
                tenant_id=account.tenant_id,
                provider_event_id=provider_event_id,
                request_digest=callback_request_digest(body),
                event=payload.get("event"),
                feature_enabled=feature_enabled,
            )
    except FeishuCardActionError:
        response = {"toast": {"type": "error", "content": "卡片操作参数无效"}}
    except TimeoutError:
        response = {"toast": {"type": "warning", "content": "系统繁忙，请稍后重试"}}
    return JSONResponse(response)


@router.post("/webhooks/feishu/{public_id}")
async def feishu_webhook(public_id: str, request: Request) -> Response:
    account, payload, body = await _account_payload(public_id, request)
    if _is_url_verification(payload):
        return JSONResponse({"challenge": payload["challenge"]})
    header = payload.get("header")
    if isinstance(header, dict) and header.get("event_type") == "card.action.trigger":
        settings = getattr(request.app.state, "settings", get_settings())
        return await _card_action_response(
            account,
            payload,
            body,
            feature_enabled=(
                settings.feishu_enabled and settings.feishu_handoff_notifications_enabled
            ),
        )

    sanitized_payload = _sanitize_payload(payload)
    adapter = FeishuWebhookAdapter(
        account_id=str(account.id),
        bot_open_id=str(account.config.get("feishu_bot_open_id") or ""),
        group_mode=str(account.config.get("feishu_group_mode") or ""),
    )
    events = adapter.normalize(sanitized_payload)
    serialized_events = [canonical_event_to_dict(event) for event in events]
    feature_enabled = getattr(request.app.state, "settings", get_settings()).feishu_enabled
    should_dispatch = feature_enabled and bool(serialized_events)
    raw_header = sanitized_payload.get("header")
    raw_event_id_value = nonblank_string_or_none(raw_header.get("event_id")) if raw_header else None
    event_namespace = nonblank_string_or_none(raw_header.get("event_type")) if raw_header else None
    event = sanitized_payload.get("event")
    message = event.get("message") if isinstance(event, dict) else None
    external_conversation_id = (
        nonblank_string_or_none(message.get("chat_id")) if isinstance(message, dict) else None
    )
    ignored_at = None if should_dispatch else datetime.now(UTC)
    async with get_session_factory()() as session:
        raw_event_id = (
            await session.execute(
                pg_insert(models.RawEvent)
                .values(
                    tenant_id=account.tenant_id,
                    platform_account_id=account.id,
                    source="feishu",
                    ingress_kind="webhook",
                    event_namespace=event_namespace,
                    external_event_id=raw_event_id_value,
                    external_conversation_id=external_conversation_id,
                    payload=sanitized_payload,
                    headers={
                        "signature_verified": True,
                        "token_verified": True,
                        "encrypted": True,
                        **({"ingress_gate": "FEISHU_DISABLED"} if not feature_enabled else {}),
                    },
                    context=(direct_dispatch_context(serialized_events) if should_dispatch else {}),
                    processing_status=("PENDING" if should_dispatch else "IGNORED_AT_INGRESS"),
                    processed_at=ignored_at,
                )
                .on_conflict_do_nothing(
                    index_elements=["platform_account_id", "external_event_id"],
                    index_where=text(
                        "source = 'feishu' AND ingress_kind = 'webhook' "
                        "AND external_event_id IS NOT NULL"
                    ),
                )
                .returning(models.RawEvent.id)
            )
        ).scalar_one_or_none()
        await session.commit()
    if should_dispatch and raw_event_id is not None:
        await dispatch_initial_raw_event(raw_event_id)
    return Response(status_code=200)


@router.post("/webhooks/feishu/{public_id}/card-actions")
async def feishu_card_actions(public_id: str, request: Request) -> Response:
    account, payload, body = await _account_payload(public_id, request)
    if _is_url_verification(payload):
        return JSONResponse({"challenge": payload["challenge"]})
    settings = getattr(request.app.state, "settings", get_settings())
    return await _card_action_response(
        account,
        payload,
        body,
        feature_enabled=(
            settings.feishu_enabled and settings.feishu_handoff_notifications_enabled
        ),
    )


async def _read_limited_body(request: Request) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > FEISHU_MAX_BODY_BYTES:
                raise FeishuSecurityError("feishu_request_too_large")
        except ValueError:
            pass
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > FEISHU_MAX_BODY_BYTES:
            raise FeishuSecurityError("feishu_request_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def _is_url_verification(payload: dict[str, Any]) -> bool:
    return payload.get("type") == "url_verification" and isinstance(payload.get("challenge"), str)


def _sanitize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(payload)
    sanitized.pop("token", None)
    header = sanitized.get("header")
    if isinstance(header, dict):
        sanitized["header"] = {key: value for key, value in header.items() if key != "token"}
    return sanitized
