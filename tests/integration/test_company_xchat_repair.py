from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest
from sqlalchemy import select, update
from tests.integration.company_permission_support import (
    create_staff,
    login_client,
    seed_conversation,
)

from apps.api.main import create_app
from social_reply.application.account_management import service
from social_reply.application.account_management.auth import revoke_session
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.secret_crypto import decrypt_secret_bundle, encrypt_secret_bundle

pytestmark = pytest.mark.integration


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://test",
        follow_redirects=False,
    )


def _oauth_bundle(access_token: str) -> dict[str, str]:
    return {
        "consumer_key": "ck",
        "consumer_secret": "cs",
        "access_token": access_token,
        "access_token_secret": f"{access_token}-secret",
    }


async def _seed_xchat_account(
    session,
    *,
    owner_user_id: uuid.UUID,
    grant_user_id: uuid.UUID | None = None,
    grant_account_access: bool = False,
):
    seed = await seed_conversation(
        session,
        tenant_id="default",
        platform="x",
        brand_id=f"xchat-{uuid.uuid4().hex}",
        owner_user_id=owner_user_id,
        shared_with_support=False,
        account_name="Company XChat repair account",
        automation_default="BOT_ACTIVE",
        state="BOT_ACTIVE",
    )
    async with get_session_factory()() as fresh:
        account = await fresh.get(models.PlatformAccount, seed.account_id)
        assert account is not None
        account.credential_bundle = encrypt_secret_bundle(_oauth_bundle("old-token"))
        account.config = {
            "delivery_mode": "direct",
            "operator_choice": "preserve",
            "xchat_enabled": True,
            "repair_marker": "preserve",
        }
        account.capability = {"dm": True, "x_chat": False, "repair_marker": "preserve"}
        account.config_version = 1
        if grant_user_id is not None:
            if grant_account_access:
                fresh.add(
                    models.AccountAccessGrant(
                        tenant_id="default",
                        platform_account_id=seed.account_id,
                        user_id=grant_user_id,
                        active=True,
                    )
                )
            fresh.add(
                models.AccountReauthorizationGrant(
                    tenant_id="default",
                    platform_account_id=seed.account_id,
                    user_id=grant_user_id,
                    active=True,
                )
            )
        await fresh.commit()
    return seed


async def _account_snapshot(account_id: uuid.UUID) -> dict:
    # Always read through a new session after HTTP work; the fixture session may retain
    # an identity-mapped PlatformAccount from setup.
    async with get_session_factory()() as fresh:
        account = await fresh.get(models.PlatformAccount, account_id)
        assert account is not None
        return {
            "owner_user_id": account.owner_user_id,
            "shared_with_support": account.shared_with_support,
            "automation_default": account.automation_default,
            "config_version": account.config_version,
            "config": dict(account.config or {}),
            "capability": dict(account.capability or {}),
            "credentials": decrypt_secret_bundle(account.credential_bundle),
        }


async def _repair_audits(account_id: uuid.UUID) -> list[dict]:
    async with get_session_factory()() as fresh:
        rows = (
            await fresh.execute(
                select(models.AuditLog).where(
                    models.AuditLog.tenant_id == "default",
                    models.AuditLog.action == "REPAIR_XCHAT_ACCOUNT",
                    models.AuditLog.subject_id == str(account_id),
                )
            )
        ).scalars()
        return [{"action": row.action, "detail": dict(row.detail or {})} for row in rows]


def _repair_path(route: str, account_id: uuid.UUID) -> str:
    if route == "saas":
        return f"/app/t/default/channels/accounts/{account_id}/xchat/repair"
    if route == "legacy":
        return f"/admin/accounts/{account_id}/xchat"
    raise AssertionError(f"unknown repair route: {route}")


async def _post_repair(
    client: httpx.AsyncClient,
    *,
    route: str,
    account_id: uuid.UUID,
    csrf: str,
    expected_config_version: str | None,
) -> httpx.Response:
    form = {"csrf_token": csrf, "xchat_pin": "1234"}
    if expected_config_version is not None:
        form["expected_config_version"] = expected_config_version
    return await client.post(
        _repair_path(route, account_id),
        data=form,
    )


def _stub_xchat_network(
    monkeypatch,
    *,
    unlock_started: asyncio.Event | None = None,
    release_unlock: asyncio.Event | None = None,
) -> tuple[list[dict], list[dict]]:
    client_calls: list[dict] = []
    unlock_calls: list[dict] = []

    class FakeXChatClient:
        def __init__(self, **kwargs):
            client_calls.append(kwargs)

        async def aclose(self):
            pass

    async def fake_unlock(**kwargs):
        unlock_calls.append(kwargs)
        if unlock_started is not None:
            unlock_started.set()
        if release_unlock is not None:
            await release_unlock.wait()
        return "private", "7"

    # Only the external XChat boundary is replaced; DB target lookup and persistence stay real.
    monkeypatch.setattr(service, "XChatClient", FakeXChatClient)
    monkeypatch.setattr(service, "unlock_account_xchat_keys", fake_unlock)
    return client_calls, unlock_calls


def _stub_dispatch(monkeypatch) -> list[tuple[str, tuple, dict]]:
    dispatched: list[tuple[str, tuple, dict]] = []

    async def fake_dispatch(actor, *args, **kwargs):
        dispatched.append((actor.actor_name, args, kwargs))

    monkeypatch.setattr(service, "dispatch_actor", fake_dispatch)
    return dispatched


# Both POST adapters are intended to reach the same grant-aware repair service.
@pytest.mark.parametrize("route", ["saas", "legacy"])
async def test_user_with_both_grants_repair_preserves_scope_and_allows_inbox(
    session, monkeypatch, route
):
    owner = await create_staff(session, username=f"xchat-owner-{uuid.uuid4().hex}")
    operator = await create_staff(
        session, username=f"xchat-grantee-{uuid.uuid4().hex}", role="OPERATOR"
    )
    seed = await _seed_xchat_account(
        session,
        owner_user_id=owner.user_id,
        grant_user_id=operator.user_id,
        grant_account_access=True,
    )
    client_calls, unlock_calls = _stub_xchat_network(monkeypatch)
    dispatched = _stub_dispatch(monkeypatch)

    async with _client() as client:
        csrf = await login_client(client, username=operator.username, password=operator.password)
        response = await _post_repair(
            client,
            route=route,
            account_id=seed.account_id,
            csrf=csrf,
            expected_config_version="1",
        )
        assert response.status_code == 303, response.text
        inbox = await client.get(f"/app/t/default/conversations/{seed.conversation_id}")
        assert inbox.status_code == 200

    state = await _account_snapshot(seed.account_id)
    assert state["owner_user_id"] == owner.user_id
    assert state["shared_with_support"] is False
    assert state["automation_default"] == "BOT_ACTIVE"
    assert state["config_version"] == 2
    assert state["config"]["delivery_mode"] == "direct"
    assert state["config"]["operator_choice"] == "preserve"
    assert state["config"]["repair_marker"] == "preserve"
    assert state["config"]["xchat_enabled"] is True
    assert state["capability"] == {"dm": True, "x_chat": True, "repair_marker": "preserve"}
    assert state["credentials"] == {
        **_oauth_bundle("old-token"),
        "xchat_private_keys_b64": "private",
        "xchat_signing_key_version": "7",
    }
    assert "1234" not in str(state["credentials"])
    assert len(client_calls) == 1
    assert len(unlock_calls) == 1
    assert unlock_calls[0]["pin"] == "1234"
    assert isinstance(unlock_calls[0]["pin"], str)
    assert dispatched == [("recover_xchat_account", (str(seed.account_id),), {})]

    audits = await _repair_audits(seed.account_id)
    assert len(audits) == 1
    assert audits[0]["action"] == "REPAIR_XCHAT_ACCOUNT"
    detail = audits[0]["detail"]
    assert set(detail) == {
        "input_config_version",
        "config_version",
        "actor_user_id",
        "actor_session_id",
        "outcome",
    }
    assert detail["input_config_version"] == 1
    assert detail["config_version"] == 2
    assert detail["actor_user_id"] == str(operator.user_id)
    assert isinstance(detail["actor_session_id"], str)
    assert detail["actor_session_id"]
    assert detail["outcome"] == "completed"
    assert "1234" not in str(detail)



@pytest.mark.parametrize("route", ["saas", "legacy"])
@pytest.mark.parametrize("has_reauthorization_grant", [False, True])
async def test_user_without_account_access_is_rejected_without_network(
    session, monkeypatch, route, has_reauthorization_grant
):
    owner = await create_staff(session, username=f"xchat-no-grant-owner-{uuid.uuid4().hex}")
    operator = await create_staff(
        session, username=f"xchat-no-grant-user-{uuid.uuid4().hex}", role="OPERATOR"
    )
    seed = await _seed_xchat_account(
        session,
        owner_user_id=owner.user_id,
        grant_user_id=operator.user_id if has_reauthorization_grant else None,
    )
    before = await _account_snapshot(seed.account_id)
    client_calls, unlock_calls = _stub_xchat_network(monkeypatch)
    dispatched = _stub_dispatch(monkeypatch)

    async with _client() as client:
        csrf = await login_client(client, username=operator.username, password=operator.password)
        response = await _post_repair(
            client,
            route=route,
            account_id=seed.account_id,
            csrf=csrf,
            expected_config_version="1",
        )
        assert response.status_code == 403
        assert response.json() == {"detail": "account_reauthorization_denied"}
        inbox = await client.get(f"/app/t/default/conversations/{seed.conversation_id}")
        assert inbox.status_code == 404
        assert inbox.json() == {"detail": "conversation_not_found"}

    assert client_calls == []
    assert unlock_calls == []
    assert dispatched == []
    assert await _account_snapshot(seed.account_id) == before
    assert await _repair_audits(seed.account_id) == []


@pytest.mark.parametrize("route", ["saas", "legacy"])
async def test_stale_repair_version_is_rejected_before_network(session, monkeypatch, route):
    admin = await create_staff(
        session,
        username=f"xchat-stale-admin-{route}-{uuid.uuid4().hex}",
        role="WORKSPACE_ADMIN",
    )
    owner = await create_staff(session, username=f"xchat-stale-owner-{route}-{uuid.uuid4().hex}")
    seed = await _seed_xchat_account(session, owner_user_id=owner.user_id)
    async with get_session_factory()() as fresh:
        account = await fresh.get(models.PlatformAccount, seed.account_id)
        assert account is not None
        account.config = {**account.config, "competitor_marker": "version-2"}
        account.config_version = 2
        await fresh.commit()
    before = await _account_snapshot(seed.account_id)
    client_calls, unlock_calls = _stub_xchat_network(monkeypatch)
    dispatched = _stub_dispatch(monkeypatch)

    async with _client() as client:
        csrf = await login_client(client, username=admin.username, password=admin.password)
        response = await _post_repair(
            client,
            route=route,
            account_id=seed.account_id,
            csrf=csrf,
            expected_config_version="1",
        )
        assert response.status_code == 409, response.text
        assert response.json() == {"detail": "account_reauthorization_version_conflict"}

    assert client_calls == []
    assert unlock_calls == []
    assert dispatched == []
    assert await _account_snapshot(seed.account_id) == before
    assert await _repair_audits(seed.account_id) == []


@pytest.mark.parametrize("route", ["saas", "legacy"])
@pytest.mark.parametrize("submitted_version", [None, "not-an-integer", "0", "-1"])
async def test_missing_invalid_repair_version_is_rejected_without_network(
    session, monkeypatch, route, submitted_version
):
    admin = await create_staff(
        session,
        username=f"xchat-version-admin-{route}-{uuid.uuid4().hex}",
        role="WORKSPACE_ADMIN",
    )
    owner = await create_staff(session, username=f"xchat-version-owner-{route}-{uuid.uuid4().hex}")
    seed = await _seed_xchat_account(session, owner_user_id=owner.user_id)
    before = await _account_snapshot(seed.account_id)
    client_calls, unlock_calls = _stub_xchat_network(monkeypatch)

    async with _client() as client:
        csrf = await login_client(client, username=admin.username, password=admin.password)
        response = await _post_repair(
            client,
            route=route,
            account_id=seed.account_id,
            csrf=csrf,
            expected_config_version=submitted_version,
        )
        # 0/-1 are intentional boundary cases: they must be rejected as invalid input,
        # rather than reaching the service as a version conflict.
        assert response.status_code == 422, response.text
        if submitted_version in {None, "not-an-integer"}:
            assert response.json() == {"detail": "invalid_integer:expected_config_version"}

    assert client_calls == []
    assert unlock_calls == []
    assert await _account_snapshot(seed.account_id) == before
    assert await _repair_audits(seed.account_id) == []


async def test_repair_rechecks_session_after_real_revoke_during_unlock(session, monkeypatch):
    owner = await create_staff(session, username=f"xchat-revoke-owner-{uuid.uuid4().hex}")
    operator = await create_staff(
        session, username=f"xchat-revoke-user-{uuid.uuid4().hex}", role="OPERATOR"
    )
    seed = await _seed_xchat_account(
        session,
        owner_user_id=owner.user_id,
        grant_user_id=operator.user_id,
        grant_account_access=True,
    )
    before = await _account_snapshot(seed.account_id)
    unlock_started = asyncio.Event()
    release_unlock = asyncio.Event()
    client_calls, unlock_calls = _stub_xchat_network(
        monkeypatch,
        unlock_started=unlock_started,
        release_unlock=release_unlock,
    )
    dispatched = _stub_dispatch(monkeypatch)

    async with _client() as client:
        csrf = await login_client(client, username=operator.username, password=operator.password)
        request_task = asyncio.create_task(
            _post_repair(
                client,
                route="saas",
                account_id=seed.account_id,
                csrf=csrf,
                expected_config_version="1",
            )
        )
        try:
            await asyncio.wait_for(unlock_started.wait(), timeout=5)
            raw_token = client.cookies.get("reply_admin_session")
            assert isinstance(raw_token, str)
            await revoke_session(raw_token)
        finally:
            release_unlock.set()
        response = await asyncio.wait_for(request_task, timeout=5)
        assert response.status_code == 403
        assert response.json() == {"detail": "xchat_repair_authority_revoked"}

    assert len(client_calls) == 1
    assert len(unlock_calls) == 1
    assert dispatched == []
    assert await _account_snapshot(seed.account_id) == before
    assert await _repair_audits(seed.account_id) == []


async def test_repair_cas_keeps_new_oauth_write_after_unlock_race(session, monkeypatch):
    owner = await create_staff(session, username=f"xchat-cas-owner-{uuid.uuid4().hex}")
    operator = await create_staff(
        session, username=f"xchat-cas-user-{uuid.uuid4().hex}", role="OPERATOR"
    )
    seed = await _seed_xchat_account(
        session,
        owner_user_id=owner.user_id,
        grant_user_id=operator.user_id,
        grant_account_access=True,
    )
    unlock_started = asyncio.Event()
    release_unlock = asyncio.Event()
    client_calls, unlock_calls = _stub_xchat_network(
        monkeypatch,
        unlock_started=unlock_started,
        release_unlock=release_unlock,
    )
    dispatched = _stub_dispatch(monkeypatch)
    replacement = _oauth_bundle("replacement-token")

    async with _client() as client:
        csrf = await login_client(client, username=operator.username, password=operator.password)
        request_task = asyncio.create_task(
            _post_repair(
                client,
                route="saas",
                account_id=seed.account_id,
                csrf=csrf,
                expected_config_version="1",
            )
        )
        await asyncio.wait_for(unlock_started.wait(), timeout=5)

        # This is an independent conditional PlatformAccount write, not a full OAuth
        # submit/process flow. It specifically proves the persistence CAS keeps a
        # reauthorization update that wins while the older repair is in unlock.
        async with get_session_factory()() as competitor:
            result = await competitor.execute(
                update(models.PlatformAccount)
                .where(
                    models.PlatformAccount.id == seed.account_id,
                    models.PlatformAccount.config_version == 1,
                )
                .values(
                    credential_bundle=encrypt_secret_bundle(replacement),
                    config={
                        "delivery_mode": "direct",
                        "operator_choice": "new-oauth",
                        "xchat_enabled": True,
                        "competitor_marker": "oauth-reconnect",
                    },
                    config_version=2,
                )
            )
            assert result.rowcount == 1
            await competitor.commit()
        release_unlock.set()
        response = await asyncio.wait_for(request_task, timeout=5)
        assert response.status_code == 409
        assert response.json() == {"detail": "account_reauthorization_version_conflict"}

    state = await _account_snapshot(seed.account_id)
    assert state["owner_user_id"] == owner.user_id
    assert state["automation_default"] == "BOT_ACTIVE"
    assert state["config_version"] == 2
    assert state["config"]["operator_choice"] == "new-oauth"
    assert state["config"]["competitor_marker"] == "oauth-reconnect"
    assert state["credentials"] == replacement
    assert "xchat_private_keys_b64" not in state["credentials"]
    assert len(client_calls) == 1
    assert len(unlock_calls) == 1
    assert dispatched == []
    assert await _repair_audits(seed.account_id) == []
