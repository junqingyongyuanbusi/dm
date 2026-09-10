import hashlib
import json
import time
import uuid

import pytest

from social_reply.application.handoff_notifications.callbacks import (
    FeishuCardActionError,
    callback_request_digest,
    handle_feishu_card_action,
)
from social_reply.connectors.feishu.security import FeishuSecurityError


def _request():
    event = {"operator": {"open_id": "ou_employee"}, "action": {"value": {"action": "claim"}}}
    body = json.dumps(
        {
            "header": {
                "event_type": "card.action.trigger",
                "event_id": "event-1",
                "app_id": "cli-company",
                "token": "fixture-verification-token",
            },
            "event": event,
        }
    ).encode()
    timestamp, nonce, key = str(int(time.time())), "fixture-nonce", "fixture-encrypt-key"
    signature = hashlib.sha256((timestamp + nonce + key).encode() + body).hexdigest()
    return (
        event,
        body,
        {
            "account_id": uuid.uuid4(),
            "tenant_id": "default",
            "app_id": "cli-company",
            "verification_token": "fixture-verification-token",
            "encrypt_key": key,
            "timestamp": timestamp,
            "nonce": nonce,
            "signature": signature,
        },
    )


@pytest.mark.parametrize("change", ["bad_signature", "token", "app", "body"])
def test_unverified_body_cannot_produce_callback_proof(change):
    _event, body, values = _request()
    if change == "bad_signature":
        values["signature"] = "0" * 64
    elif change == "token":
        values["verification_token"] = "different-token"
    elif change == "app":
        values["app_id"] = "different-app"
    else:
        body += b" "
    with pytest.raises(FeishuSecurityError):
        callback_request_digest(body, **values)


def test_unsigned_card_callback_is_authenticated_by_verification_token_only():
    # 飞书交互卡片回调不投递 X-Lark 签名头。缺失签名必须被接受——强制要求会把每条
    # 真实回调拒成 401，飞书侧表现为错误码 200671。签名存在时仍走严格校验（见上）。
    _event, body, values = _request()
    values.update({"timestamp": None, "nonce": None, "signature": None})
    proof = callback_request_digest(body, **values)
    assert proof.is_verified
    assert proof.provider_event_id == "event-1"
    assert proof.digest == hashlib.sha256(body).hexdigest()


def test_valid_callback_proof_is_bound_to_verified_body():
    _event, body, values = _request()
    proof = callback_request_digest(body, **values)
    assert proof.is_verified
    assert proof.account_id == values["account_id"]
    assert proof.tenant_id == "default"
    assert proof.provider_event_id == "event-1"
    assert proof.digest == hashlib.sha256(body).hexdigest()


async def test_verified_callback_cannot_be_rebound_to_different_event_or_account():
    event, body, values = _request()
    proof = callback_request_digest(body, **values)
    for account_id, supplied_event in (
        (uuid.uuid4(), event),
        (values["account_id"], {**event, "operator": {"open_id": "ou_other"}}),
    ):
        with pytest.raises(FeishuCardActionError, match="feishu_callback_scope_mismatch"):
            await handle_feishu_card_action(
                account_id=account_id,
                tenant_id="default",
                provider_event_id="event-1",
                request_digest=proof,
                event=supplied_event,
                feature_enabled=True,
            )
