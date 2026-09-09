import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from social_reply.application.account_management import email


@pytest.mark.parametrize("operation", ["CONNECT_ACCOUNT", "REAUTHORIZE"])
async def test_email_provisioning_preserves_operation_and_authority(
    monkeypatch, tmp_path, operation
):
    settings = email.get_settings().model_copy(
        update={
            "email_enabled": True,
            "email_allowed_hosts": frozenset({"imap.larksuite.com", "smtp.larksuite.com"}),
        }
    )
    monkeypatch.setattr(email, "get_settings", lambda: settings)
    account_id = uuid.uuid4()
    initiator_user_id = uuid.uuid4()
    initiator_session_id = uuid.uuid4()
    provision = AsyncMock(return_value=(account_id, "email-operation-contract"))
    runtime = AsyncMock(return_value=SimpleNamespace(automation_default="BOT_ACTIVE"))
    monkeypatch.setattr(email, "provision_direct_account", provision)
    monkeypatch.setattr(email, "get_platform_account_runtime", runtime)
    imap_client = SimpleNamespace(connect=AsyncMock(return_value=42), aclose=AsyncMock())
    smtp_client = SimpleNamespace(probe=AsyncMock(), aclose=AsyncMock())
    operation_arguments = (
        {"operation": operation, "target_account_id": account_id, "expected_config_version": 7}
        if operation == "REAUTHORIZE"
        else {}
    )

    result = await email.connect_email_account(
        email_address="support@example.com",
        username="mail-user",
        password="mail-password",
        imap_host="imap.larksuite.com",
        smtp_host="smtp.larksuite.com",
        public_base_url="https://reply.example.com",
        secrets_root=tmp_path,
        imap_client_factory=lambda **_kwargs: imap_client,
        smtp_client_factory=lambda **_kwargs: smtp_client,
        initiator_user_id=initiator_user_id,
        initiator_session_id=initiator_session_id,
        authority_kind="STAFF_SESSION",
        authority_version=3,
        **operation_arguments,
    )

    provision.assert_awaited_once()
    arguments = provision.await_args.kwargs
    assert arguments["operation"] == operation
    assert arguments["initiator_user_id"] == initiator_user_id
    assert arguments["initiator_session_id"] == initiator_session_id
    assert arguments["authority_kind"] == "STAFF_SESSION"
    assert arguments["authority_version"] == 3
    imap_client.connect.assert_awaited_once()
    smtp_client.probe.assert_awaited_once()
    imap_client.aclose.assert_awaited_once()
    smtp_client.aclose.assert_awaited_once()
    if operation == "REAUTHORIZE":
        assert arguments["target_account_id"] == account_id
        assert arguments["expected_config_version"] == 7
        assert arguments["derived_config_patch"]["email_health_status"] == "READY"
        runtime.assert_awaited_once_with(account_id)
        assert result.automation_default == "BOT_ACTIVE"
        assert result.credential_updated is True
        assert result.connection_status == "NEEDS_ACTION"
    else:
        assert arguments["derived_config_patch"] is None
        runtime.assert_not_awaited()
        assert result.automation_default == "BOT_DRAFT_ONLY"
        assert result.credential_updated is False
        assert result.connection_status == "READY"
