import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any


class SendContractError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class TextSendCommand:
    destination_type: str
    platform: str
    text: str
    target: dict[str, Any]


@dataclass(frozen=True)
class DirectReplyDestination:
    destination_type: str
    target: dict[str, Any]
    valid_until: datetime | None


_DESTINATION_PLATFORMS = {
    "telegram_dm": frozenset({"telegram"}),
    "meta_messenger_dm": frozenset({"facebook"}),
    "meta_instagram_dm": frozenset({"instagram"}),
    "meta_public_comment": frozenset({"facebook", "instagram"}),
    "meta_private_reply": frozenset({"facebook", "instagram"}),
    "whatsapp_session_message": frozenset({"whatsapp"}),
    "x_dm": frozenset({"x"}),
    "x_chat_message": frozenset({"x"}),
    "x_post_reply": frozenset({"x"}),
    "feishu_p2p_reply": frozenset({"feishu"}),
    "feishu_group_reply": frozenset({"feishu"}),
}


def _required_string(target: Mapping[str, Any], key: str) -> str:
    value = target.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SendContractError("DELIVERY_TARGET_INVALID", f"{key}_missing")
    return value


def _bound_string(
    target: Mapping[str, Any],
    source_target: Mapping[str, Any],
    key: str,
    *,
    fallback: str | None = None,
) -> str:
    value = _required_string(target, key)
    expected = source_target.get(key, fallback)
    if not isinstance(expected, str) or not expected.strip() or value != expected:
        raise SendContractError("DELIVERY_TARGET_INVALID", f"{key}_scope_mismatch")
    return value


def _target_kind(target: Mapping[str, Any], expected: str) -> None:
    if target.get("kind") != expected:
        raise SendContractError(
            "DELIVERY_TARGET_INVALID",
            f"target_kind_must_be_{expected}",
        )


def _optional_bound_string(
    target: Mapping[str, Any],
    source_target: Mapping[str, Any],
    key: str,
) -> str | None:
    value = target.get(key)
    expected = source_target.get(key)
    if value is None and expected is None:
        return None
    if (
        not isinstance(value, str)
        or not value.strip()
        or not isinstance(expected, str)
        or not expected.strip()
        or value != expected
    ):
        raise SendContractError("DELIVERY_TARGET_INVALID", f"{key}_scope_mismatch")
    return value


def _feishu_reply_target(
    target: Mapping[str, Any],
    source_target: Mapping[str, Any],
    *,
    kind: str,
    chat_type: str,
) -> dict[str, Any]:
    allowed_keys = {
        "kind",
        "message_id",
        "chat_id",
        "chat_type",
        "sender_open_id",
        "thread_id",
        "root_id",
    }
    if unknown_keys := set(target) - allowed_keys:
        raise SendContractError(
            "DELIVERY_TARGET_INVALID",
            f"feishu_target_unknown_keys:{','.join(sorted(unknown_keys))}",
        )
    _target_kind(target, kind)
    bound_chat_type = _bound_string(target, source_target, "chat_type")
    if bound_chat_type != chat_type:
        raise SendContractError(
            "DELIVERY_TARGET_INVALID",
            f"feishu_chat_type_must_be_{chat_type}",
        )
    normalized = {
        "kind": kind,
        "message_id": _bound_string(target, source_target, "message_id"),
        "chat_id": _bound_string(target, source_target, "chat_id"),
        "chat_type": bound_chat_type,
        "sender_open_id": _bound_string(target, source_target, "sender_open_id"),
    }
    for key in ("thread_id", "root_id"):
        if value := _optional_bound_string(target, source_target, key):
            normalized[key] = value
    return normalized


def _telegram_target(
    target: Mapping[str, Any],
    *,
    destination_id: str,
) -> dict[str, Any]:
    value = target.get("chat_id")
    if value is None and not target:
        try:
            value = int(destination_id.rsplit(":", 1)[-1])
        except (TypeError, ValueError) as exc:
            raise SendContractError(
                "DELIVERY_TARGET_INVALID",
                "telegram_chat_id_missing",
            ) from exc
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise SendContractError("DELIVERY_TARGET_INVALID", "telegram_chat_id_invalid")
    if isinstance(value, str) and not value.strip():
        raise SendContractError("DELIVERY_TARGET_INVALID", "telegram_chat_id_invalid")
    return {"chat_id": value}


def parse_direct_text_command(
    *,
    destination_type: str,
    message_type: str,
    payload: object,
    destination_id: str,
    account_platform: str,
    account_external_id: str | None,
    source_target: Mapping[str, Any],
    conversation_external_user_id: str,
    outbox_id: uuid.UUID,
) -> TextSendCommand:
    if message_type != "text":
        raise SendContractError("DELIVERY_PAYLOAD_INVALID", "direct_message_type_must_be_text")
    if not isinstance(payload, Mapping):
        raise SendContractError("DELIVERY_PAYLOAD_INVALID", "payload_must_be_object")
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise SendContractError("DELIVERY_TEXT_INVALID", "text_must_be_nonempty_string")
    allowed_platforms = _DESTINATION_PLATFORMS.get(destination_type)
    if allowed_platforms is None or account_platform not in allowed_platforms:
        raise SendContractError("DELIVERY_ROUTE_INVALID", "destination_platform_mismatch")
    raw_target = payload.get("target")
    if raw_target is None:
        raw_target = {}
    if not isinstance(raw_target, Mapping):
        raise SendContractError("DELIVERY_TARGET_INVALID", "target_must_be_object")
    target = dict(raw_target)

    if destination_type == "telegram_dm":
        target = _telegram_target(target, destination_id=destination_id)
        source_chat_id = source_target.get("chat_id")
        if source_chat_id is not None and str(target["chat_id"]) != str(source_chat_id):
            raise SendContractError(
                "DELIVERY_TARGET_INVALID",
                "telegram_chat_id_scope_mismatch",
            )
    elif destination_type in {"meta_messenger_dm", "meta_instagram_dm"}:
        _target_kind(target, "dm")
        target = {
            "kind": "dm",
            "recipient_id": _bound_string(
                target,
                source_target,
                "recipient_id",
                fallback=conversation_external_user_id,
            ),
        }
    elif destination_type == "meta_public_comment":
        _target_kind(target, "comment")
        target = {
            "kind": "comment",
            "comment_id": _bound_string(target, source_target, "comment_id"),
        }
    elif destination_type == "meta_private_reply":
        _target_kind(target, "private_reply")
        target = {
            "kind": "private_reply",
            "comment_id": _bound_string(target, source_target, "comment_id"),
        }
    elif destination_type == "whatsapp_session_message":
        _target_kind(target, "session_message")
        phone_number_id = _required_string(target, "phone_number_id")
        if not account_external_id or phone_number_id != account_external_id:
            raise SendContractError(
                "DELIVERY_TARGET_INVALID",
                "whatsapp_phone_number_mismatch",
            )
        target = {
            "kind": "session_message",
            "phone_number_id": phone_number_id,
            "to": _bound_string(
                target,
                source_target,
                "to",
                fallback=conversation_external_user_id,
            ),
        }
    elif destination_type == "x_dm":
        _target_kind(target, "dm")
        target = {
            "kind": "dm",
            "participant_id": _bound_string(
                target,
                source_target,
                "participant_id",
                fallback=conversation_external_user_id,
            ),
        }
    elif destination_type == "x_post_reply":
        _target_kind(target, "reply")
        target = {
            "kind": "reply",
            "in_reply_to_post_id": _bound_string(
                target,
                source_target,
                "in_reply_to_post_id",
            ),
        }
    elif destination_type == "x_chat_message":
        _target_kind(target, "x_chat")
        conversation_token = target.get("conversation_token")
        if conversation_token is not None and not isinstance(conversation_token, str):
            raise SendContractError(
                "DELIVERY_TARGET_INVALID",
                "xchat_conversation_token_invalid",
            )
        target = {
            "kind": "x_chat",
            "conversation_id": _bound_string(
                target,
                source_target,
                "conversation_id",
            ),
            "message_id": str(outbox_id),
        }
        if conversation_token:
            target["conversation_token"] = conversation_token
    elif destination_type == "feishu_p2p_reply":
        target = _feishu_reply_target(target, source_target, kind="dm", chat_type="p2p")
        target["uuid"] = str(outbox_id)
    elif destination_type == "feishu_group_reply":
        target = _feishu_reply_target(target, source_target, kind="mention", chat_type="group")
        target["uuid"] = str(outbox_id)

    return TextSendCommand(
        destination_type=destination_type,
        platform=account_platform,
        text=text,
        target=target,
    )


def build_direct_reply_destination(
    *,
    platform: str,
    reply_target: Mapping[str, Any] | None,
    visibility: str,
    occurred_at: datetime | None,
    now: datetime,
) -> DirectReplyDestination:
    target = dict(reply_target or {})
    kind = target.get("kind", "dm")
    if platform == "telegram":
        destination_type = "telegram_dm"
    elif platform == "facebook":
        destination_type = (
            "meta_private_reply"
            if visibility == "private" and kind == "comment"
            else "meta_public_comment"
            if kind == "comment"
            else "meta_messenger_dm"
        )
    elif platform == "instagram":
        destination_type = (
            "meta_private_reply"
            if visibility == "private" and kind == "comment"
            else "meta_public_comment"
            if kind == "comment"
            else "meta_instagram_dm"
        )
    elif platform == "whatsapp":
        destination_type = "whatsapp_session_message"
    elif platform == "x":
        if kind == "reply" and visibility != "public":
            raise ValueError("x_post_reply_requires_public_visibility")
        destination_type = (
            "x_post_reply" if kind == "reply" else "x_chat_message" if kind == "x_chat" else "x_dm"
        )
    elif platform == "feishu":
        if kind == "dm":
            destination_type = "feishu_p2p_reply"
            target = _feishu_reply_target(target, target, kind="dm", chat_type="p2p")
        elif kind == "mention":
            destination_type = "feishu_group_reply"
            target = _feishu_reply_target(target, target, kind="mention", chat_type="group")
        else:
            raise ValueError(f"unsupported_feishu_reply_target:{kind}")
    else:
        raise ValueError(f"unsupported_direct_platform:{platform}")

    if destination_type == "meta_private_reply":
        target = {**target, "kind": "private_reply"}
    valid_until = None
    if destination_type in {
        "meta_messenger_dm",
        "meta_instagram_dm",
        "whatsapp_session_message",
    }:
        valid_until = (occurred_at or now) + timedelta(hours=24)
    elif destination_type == "meta_private_reply":
        valid_until = now + timedelta(days=7)
    return DirectReplyDestination(destination_type, target, valid_until)
