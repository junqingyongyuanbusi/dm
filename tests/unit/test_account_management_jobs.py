from pathlib import Path

import httpx
import pytest

from social_reply.application.account_management import jobs
from social_reply.application.account_management.service import AccountConnectionResult
from social_reply.application.account_management.xchat_activation import XChatActivationError


def test_safe_request_never_contains_credentials():
    safe = jobs._safe_request(
        "instagram",
        {
            "external_account_id": "ig-1",
            "access_token": "secret",
            "app_secret": "secret",
            "name": "IG",
            "idempotency_key": "browser-value",
        },
    )
    assert safe == {"external_account_id": "ig-1", "name": "IG"}


def test_error_sanitizes_platform_http_errors():
    request = httpx.Request("POST", "https://api.example/messages?token=secret")
    response = httpx.Response(503, request=request)
    code, message, retryable = jobs._error(
        httpx.HTTPStatusError("secret response", request=request, response=response)
    )
    assert code == "PLATFORM_HTTP_503"
    assert message == "Platform API returned HTTP 503"
    assert retryable is True
    assert "secret" not in message


def test_error_requires_manual_pin_resubmission_for_xchat_failures():
    code, message, retryable = jobs._error(
        XChatActivationError(
            "XCHAT_RATE_LIMITED",
            "X API 当前限流",
            status_code=429,
            retryable=True,
        )
    )
    assert (code, message, retryable) == (
        "XCHAT_RATE_LIMITED",
        "X API 当前限流",
        False,
    )


def test_error_explains_missing_x_direct_message_permission():
    code, message, retryable = jobs._error(
        ValueError(
            "x_direct_message_permission_missing: set X App permissions to "
            "Read and write and Direct message"
        )
    )
    assert code == "X_DM_PERMISSION_REQUIRED"
    assert "Read and write and Direct message" in message
    assert retryable is False


@pytest.mark.parametrize(
    ("platform", "settings_update"),
    [
        ("facebook", {"facebook_messenger_enabled": False}),
        ("instagram", {"instagram_messaging_enabled": False}),
        ("whatsapp", {"whatsapp_enabled": False}),
    ],
)
async def test_provisioning_execution_rechecks_platform_flag_before_decrypting_secrets(
    monkeypatch, platform, settings_update
):
    settings = jobs.get_settings().model_copy(update=settings_update)
    monkeypatch.setattr(jobs, "get_settings", lambda: settings)

    def unexpected_decrypt(_value):
        raise AssertionError("disabled platform must not decrypt staging credentials")

    monkeypatch.setattr(jobs, "decrypt_secret_bundle", unexpected_decrypt)
    job = type("Job", (), {"platform": platform})()
    with pytest.raises(ValueError, match=f"{platform}_integration_disabled"):
        await jobs._connect(job)


def test_result_payload_does_not_expose_verify_token():
    result = AccountConnectionResult(
        account_id=__import__("uuid").uuid4(),
        platform="instagram",
        external_account_id="ig-1",
        public_id="ig_public",
        webhook_url="https://reply.example/webhooks/meta/meta_public",
        name="IG",
        automation_default="BOT_DRAFT_ONLY",
        verify_token="do-not-store",
    )
    payload = jobs._result_payload(result)
    assert "verify_token" not in payload
    assert payload["public_id"] == "ig_public"


def test_public_job_defensively_redacts_nested_credentials():
    job = type(
        "Job",
        (),
        {
            "id": __import__("uuid").uuid4(),
            "tenant_id": "default",
            "brand_id": "default",
            "platform": "instagram",
            "operation": "CONNECT",
            "status": "COMPLETED",
            "current_step": "COMPLETED",
            "attempt_count": 1,
            "account_id": None,
            "platform_app_id": None,
            "result": {"verify_token": "secret", "nested": {"access_token": "secret"}},
            "last_error_code": None,
            "last_error_message": None,
            "created_at": None,
            "updated_at": None,
            "completed_at": None,
        },
    )()
    assert jobs.public_job(job)["result"] == {"nested": {}}


def test_secret_store_staging_path_is_outside_database(tmp_path):
    path = Path(tmp_path) / "staging" / "job.json"
    from social_reply.infrastructure.secrets import SecretStore

    store = SecretStore()
    ref = store.write_mapping(path, {"token": "secret"})
    assert ref.startswith("file://")
    assert store.read_mapping(ref, fallback_key="token") == {"token": "secret"}
    store.delete(ref)
    assert not path.exists()
