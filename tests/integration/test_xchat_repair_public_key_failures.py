"""Keep service authorization and activation real; replace only the provider transport.

HTTP authentication/form extraction is injected with an authenticated database
Principal. These cases do not claim CSRF or browser-login coverage.
"""

import uuid
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from starlette.responses import HTMLResponse
from tests.integration.test_channel_management_access import _create_user

from social_reply.application.account_management import admin_console, saas_console, service
from social_reply.connectors.xchat.client import XChatClient
from social_reply.infrastructure.database import models
from social_reply.infrastructure.secret_crypto import encrypt_secret_bundle
from social_reply.shared.config import get_settings

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("route", ["saas", "legacy"])
@pytest.mark.parametrize(
    ("failure", "expected_status", "expected_code"),
    [
        (429, 429, "XCHAT_RATE_LIMITED"),
        (503, 503, "XCHAT_API_UNAVAILABLE"),
        ("timeout", 503, "XCHAT_API_UNAVAILABLE"),
        (401, 422, "XCHAT_REAUTHORIZATION_REQUIRED"),
    ],
)
async def test_first_public_key_failure_is_normalized_without_writes(
    session, migrated_db, monkeypatch, route, failure, expected_status, expected_code
):
    monkeypatch.setattr(get_settings(), "xchat_enabled", True)
    user, principal = await _create_user(
        session, f"xchat-http-{uuid.uuid4().hex[:12]}", "WORKSPACE_ADMIN"
    )
    account = models.PlatformAccount(
        tenant_id="default",
        brand_id="default",
        platform="x",
        owner_user_id=user.id,
        name="XChat transport contract",
        external_account_id="123456789",
        public_id=f"xchat-{uuid.uuid4()}",
        status="active",
        config_version=1,
        automation_default="BOT_DRAFT_ONLY",
        config={"xchat_enabled": True},
        capability={"dm": True},
        credential_bundle=encrypt_secret_bundle(
            {
                "consumer_key": "test-consumer",
                "consumer_secret": "test-secret",
                "access_token": "test-token",
                "access_token_secret": "test-token-secret",
            }
        ),
    )
    session.add(account)
    await session.commit()
    before = (dict(account.credential_bundle), dict(account.config), dict(account.capability))
    requests = []

    def respond(request):
        requests.append(request)
        assert request.method == "GET"
        assert request.url.path == "/2/users/123456789/public_keys"
        if failure == "timeout":
            raise httpx.ReadTimeout("provider unavailable", request=request)
        return httpx.Response(failure, json={"title": "Provider failure"}, request=request)

    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(
        service, "XChatClient", lambda **kwargs: XChatClient(**kwargs, transport=transport)
    )
    dispatch = AsyncMock()
    monkeypatch.setattr(service, "dispatch_actor", dispatch)
    form = {"xchat_pin": "1234", "expected_config_version": "1"}
    if route == "saas":
        monkeypatch.setattr(
            saas_console,
            "_channel_mutation_principal_and_form",
            AsyncMock(return_value=(principal, form)),
        )
        with pytest.raises(HTTPException) as caught:
            await saas_console.repair_channel_xchat_route(object(), "default", account.id)
        assert caught.value.status_code == expected_status
        assert caught.value.detail["code"] == expected_code
        assert caught.value.detail["message"]
        assert caught.value.detail["retryable"] is (expected_status in {429, 503})
    else:
        monkeypatch.setattr(admin_console, "_web_principal", AsyncMock(return_value=principal))
        monkeypatch.setattr(admin_console, "_form", AsyncMock(return_value=form))
        monkeypatch.setattr(admin_console, "_require_csrf", lambda *args: None)
        monkeypatch.setattr(
            admin_console,
            "notice",
            lambda title, text, *, status_code: HTMLResponse(text, status_code=status_code),
        )
        response = await admin_console.enable_account_xchat(object(), account.id)
        assert response.status_code == expected_status
        assert expected_code in response.body.decode()
        assert any("\u4e00" <= char <= "\u9fff" for char in response.body.decode())
    assert len(requests) == 1
    dispatch.assert_not_awaited()
    await session.refresh(account)
    assert account.config_version == 1
    assert (account.credential_bundle, account.config, account.capability) == before
    assert not list(
        await session.scalars(
            select(models.AuditLog.id).where(
                models.AuditLog.action == "REPAIR_XCHAT_ACCOUNT",
                models.AuditLog.subject_id == str(account.id),
            )
        )
    )
