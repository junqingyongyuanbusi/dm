"""Error transport only; live authorization is exercised by database contracts."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.responses import HTMLResponse

from social_reply.application.account_management import (
    admin_console,
    channel_management,
    saas_console,
)
from social_reply.application.account_management.auth import Principal
from social_reply.application.account_management.xchat_activation import XChatActivationError


@pytest.mark.parametrize("principal_role", ["WORKSPACE_ADMIN", "USER"])
@pytest.mark.parametrize(
    ("code", "message", "status_code", "retryable"),
    [
        ("XCHAT_PIN_INVALID", "PIN 不正确。", 422, False),
        ("XCHAT_KEYSTORE_RATE_LIMITED", "请稍后重新提交 PIN。", 429, True),
        ("XCHAT_KEYSTORE_UNAVAILABLE", "密钥服务暂时不可用。", 503, True),
    ],
)
async def test_activation_errors_survive_adapter_and_both_routes(
    monkeypatch, principal_role, code, message, status_code, retryable
):
    account_id = uuid.uuid4()
    # This unit test isolates exception transport; the mocked session/principal is not
    # authorization evidence. Database contracts cover live session and grant checks.
    principal = Principal(
        session_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        username="operator",
        actor="user:operator",
        tenant_id="default",
        allowed_tenants=frozenset({"default"}),
        role=principal_role,
    )
    actor = channel_management.ChannelActor(
        actor=principal.actor,
        role="ADMIN" if principal.is_workspace_admin else "USER",
        user_id=principal.user_id,
        session_id=principal.session_id,
    )
    account = SimpleNamespace(id=account_id, tenant_id="default", platform="x", config_version=1)

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def scalar(self, statement):
            return account

    error = XChatActivationError(code, message, status_code=status_code, retryable=retryable)
    repair = AsyncMock(side_effect=error)
    monkeypatch.setattr(channel_management, "enable_xchat_for_account", repair)
    monkeypatch.setattr(
        channel_management, "principal_from_session_row", AsyncMock(return_value=principal)
    )
    for module in (channel_management, admin_console):
        monkeypatch.setattr(module, "get_session_factory", lambda: Session)
        monkeypatch.setattr(module, "get_settings", lambda: SimpleNamespace(xchat_enabled=True))
    with pytest.raises(XChatActivationError) as caught:
        await channel_management.repair_channel_xchat(
            tenant_id="default",
            account_id=account_id,
            actor=actor,
            pin="1234",
            expected_config_version=1,
        )
    assert caught.value is error
    direct_service_call = repair.await_args_list[-1]
    assert repair.await_count == 1

    form = {"xchat_pin": "1234", "expected_config_version": "1"}
    monkeypatch.setattr(
        saas_console,
        "_channel_mutation_principal_and_form",
        AsyncMock(return_value=(principal, form)),
    )
    saas_actors = []
    original_saas_repair = saas_console.repair_channel_xchat

    async def capture_saas_repair(**kwargs):
        saas_actors.append(kwargs["actor"])
        return await original_saas_repair(**kwargs)

    monkeypatch.setattr(saas_console, "repair_channel_xchat", capture_saas_repair)
    with pytest.raises(HTTPException) as response:
        await saas_console.repair_channel_xchat_route(object(), "default", account_id)
    assert response.value.status_code == status_code
    assert response.value.detail == {"code": code, "message": message, "retryable": retryable}
    saas_service_call = repair.await_args_list[-1]
    assert repair.await_count == 2

    legacy_actors = []
    original_legacy_repair = admin_console.repair_channel_xchat

    async def capture_legacy_repair(**kwargs):
        legacy_actors.append(kwargs["actor"])
        return await original_legacy_repair(**kwargs)

    monkeypatch.setattr(admin_console, "repair_channel_xchat", capture_legacy_repair)
    monkeypatch.setattr(admin_console, "_web_principal", AsyncMock(return_value=principal))
    monkeypatch.setattr(admin_console, "_form", AsyncMock(return_value=form))
    monkeypatch.setattr(admin_console, "_require_csrf", lambda *args: None)
    monkeypatch.setattr(admin_console, "_account_read_scope", lambda *args: True)
    monkeypatch.setattr(
        admin_console,
        "notice",
        lambda title, text, *, status_code: HTMLResponse(text, status_code=status_code),
    )
    response = await admin_console.enable_account_xchat(object(), account_id)
    assert response.status_code == status_code
    assert message in response.body.decode()
    assert code in response.body.decode()
    legacy_service_call = repair.await_args_list[-1]
    assert repair.await_count == 3
    for service_call in (direct_service_call, saas_service_call, legacy_service_call):
        assert service_call.kwargs["account_id"] == account_id
        assert service_call.kwargs["tenant_id"] == "default"
        assert service_call.kwargs["principal"] is principal
        assert service_call.kwargs["expected_config_version"] == 1
    assert account.config_version == 1
    assert saas_actors[0].role == ("ADMIN" if principal.is_workspace_admin else "USER")
    assert legacy_actors[0].role == ("ADMIN" if principal.is_workspace_admin else "USER")
