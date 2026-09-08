import uuid
from dataclasses import dataclass, field

import pytest
from sqlalchemy import select

from social_reply.application.account_management import channel_management
from social_reply.application.account_management.auth import (
    authenticate,
    hash_password,
    issue_session,
)
from social_reply.application.account_management.kill_switch_recovery import (
    build_pending_account_kill_switch_detail,
    reconcile_account_kill_switch_command,
    sweep_account_kill_switch_commands,
)
from social_reply.infrastructure.database import models


@dataclass
class FakeRedis:
    values: set[str] = field(default_factory=set)
    fail_delete_after_apply_once: bool = False
    fail_set: bool = False
    set_calls: list[str] = field(default_factory=list)
    delete_calls: list[str] = field(default_factory=list)

    async def exists(self, key: str) -> int:
        return int(key in self.values)

    async def set(self, key: str, _value: str) -> bool:
        self.set_calls.append(key)
        if self.fail_set:
            raise RuntimeError("redis set unavailable")
        self.values.add(key)
        return True

    async def delete(self, key: str) -> int:
        self.delete_calls.append(key)
        existed = key in self.values
        self.values.discard(key)
        if self.fail_delete_after_apply_once:
            self.fail_delete_after_apply_once = False
            raise RuntimeError("redis delete result unknown")
        return int(existed)

    async def aclose(self) -> None:
        return None


def _redis_key(account_id: uuid.UUID) -> str:
    return f"killswitch:account:default:{account_id}"


def _command_detail(
    *,
    operation_id: uuid.UUID,
    account_id: uuid.UUID,
    target_enabled: bool | None,
    account_sequence: int,
    actor_role: str = "ADMIN",
    owner_user_id: uuid.UUID | None = None,
) -> dict[str, object]:
    detail: dict[str, object] = {
        "operation_id": str(operation_id),
        "tenant_id": "default",
        "account_id": str(account_id),
        "account_sequence": account_sequence,
        "actor_role": actor_role,
        "owner_user_id": str(owner_user_id) if owner_user_id else None,
        "status": "PENDING",
        "outcome": "PENDING",
    }
    if target_enabled is not None:
        detail["target_enabled"] = target_enabled
        detail["enabled"] = target_enabled
    return detail


async def _seed_account(
    session,
    *,
    suffix: str,
    owner_user_id: uuid.UUID | None = None,
) -> models.PlatformAccount:
    account = models.PlatformAccount(
        tenant_id="default",
        brand_id="default",
        platform="telegram",
        owner_user_id=owner_user_id,
        name=f"Kill switch recovery {suffix}",
        external_account_id=f"telegram-kill-switch-recovery-{suffix}",
        public_id=f"tg_kill_switch_recovery_{suffix}",
        config={},
        capability={},
        automation_default="BOT_DRAFT_ONLY",
        status="active",
    )
    session.add(account)
    await session.flush()
    return account


async def _seed_command(
    session,
    *,
    account: models.PlatformAccount,
    operation_id: uuid.UUID,
    target_enabled: bool | None,
    account_sequence: int,
    actor_role: str = "ADMIN",
    owner_user_id: uuid.UUID | None = None,
) -> models.AuditLog:
    audit = models.AuditLog(
        id=operation_id,
        tenant_id=account.tenant_id,
        category="account_management",
        actor="system:test",
        action="SET_PLATFORM_ACCOUNT_KILL_SWITCH",
        subject_type="platform_account",
        subject_id=str(account.id),
        detail=_command_detail(
            operation_id=operation_id,
            account_id=account.id,
            target_enabled=target_enabled,
            account_sequence=account_sequence,
            actor_role=actor_role,
            owner_user_id=owner_user_id,
        ),
    )
    if actor_role == "ADMIN":
        _token, session_id = await issue_session()
        audit.detail = {
            **audit.detail,
            "authority_version": 2,
            "actor_kind": "BOOTSTRAP",
            "actor_user_id": None,
            "actor_session_id": str(session_id),
        }
    session.add(audit)
    return audit


async def test_request_persists_complete_operation_contract(
    session,
    migrated_db,
    monkeypatch,
) -> None:
    owner = models.AdminUser(
        username="kill-switch-owner",
        password_hash="not-used",
        tenant_id="default",
        role="USER",
        must_change_password=False,
        status="active",
    )
    session.add(owner)
    await session.flush()
    account = await _seed_account(session, suffix="request-contract", owner_user_id=owner.id)
    await session.commit()
    fake_redis = FakeRedis()

    from social_reply.application.account_management import kill_switch_recovery

    monkeypatch.setattr(
        kill_switch_recovery.aioredis,
        "from_url",
        lambda _url: fake_redis,
    )

    _token, admin_session_id = await issue_session()
    await channel_management.set_channel_account_kill_switch(
        tenant_id="default",
        account_id=account.id,
        actor=channel_management.ChannelActor(
            actor="user:admin",
            role="ADMIN",
            user_id=None,
            session_id=admin_session_id,
        ),
        enabled=False,
    )

    audit = await session.scalar(
        select(models.AuditLog).where(
            models.AuditLog.action == "SET_PLATFORM_ACCOUNT_KILL_SWITCH",
            models.AuditLog.subject_id == str(account.id),
        )
    )
    assert audit is not None
    assert audit.detail["operation_id"] == str(audit.id)
    assert audit.detail["tenant_id"] == "default"
    assert audit.detail["account_id"] == str(account.id)
    assert audit.detail["account_sequence"] == 1
    assert audit.detail["target_enabled"] is False
    assert audit.detail["enabled"] is False
    assert audit.detail["actor_role"] == "ADMIN"
    assert audit.detail["authority_version"] == 2
    assert audit.detail["actor_kind"] == "BOOTSTRAP"
    assert audit.detail["actor_session_id"] == str(admin_session_id)
    assert audit.detail["owner_user_id"] == str(owner.id)
    assert audit.detail["status"] == "UNCHANGED"
    assert audit.detail["outcome"] == "UNCHANGED"


async def test_sweep_recovers_pending_command_after_crash_window(
    session,
    migrated_db,
    monkeypatch,
) -> None:
    account = await _seed_account(session, suffix="crash-window")
    operation_id = uuid.uuid4()
    audit = await _seed_command(
        session,
        account=account,
        operation_id=operation_id,
        target_enabled=True,
        account_sequence=1,
    )
    await session.commit()
    fake_redis = FakeRedis()

    from social_reply.application.account_management import kill_switch_recovery

    monkeypatch.setattr(
        kill_switch_recovery.aioredis,
        "from_url",
        lambda _url: fake_redis,
    )

    recovered = await sweep_account_kill_switch_commands(batch_size=10)

    await session.refresh(audit)
    assert recovered == [operation_id]
    assert _redis_key(account.id) in fake_redis.values
    assert audit.detail["status"] == "APPLIED"
    assert audit.detail["outcome"] == "APPLIED"
    assert audit.detail["attempt_count"] == 1


async def test_newer_command_supersedes_stale_command_without_old_overwrite(
    session,
    migrated_db,
    monkeypatch,
) -> None:
    account = await _seed_account(session, suffix="superseded")
    stale_operation_id = uuid.uuid4()
    latest_operation_id = uuid.uuid4()
    stale_audit = await _seed_command(
        session,
        account=account,
        operation_id=stale_operation_id,
        target_enabled=False,
        account_sequence=1,
    )
    latest_audit = await _seed_command(
        session,
        account=account,
        operation_id=latest_operation_id,
        target_enabled=True,
        account_sequence=2,
    )
    await session.commit()
    fake_redis = FakeRedis()

    from social_reply.application.account_management import kill_switch_recovery

    monkeypatch.setattr(
        kill_switch_recovery.aioredis,
        "from_url",
        lambda _url: fake_redis,
    )

    recovered = await sweep_account_kill_switch_commands(batch_size=10)

    await session.refresh(stale_audit)
    await session.refresh(latest_audit)
    assert set(recovered) == {stale_operation_id, latest_operation_id}
    assert stale_audit.detail["status"] == "SUPERSEDED"
    assert stale_audit.detail["superseded_by_operation_id"] == str(latest_operation_id)
    assert latest_audit.detail["status"] == "APPLIED"
    assert _redis_key(account.id) in fake_redis.values
    assert fake_redis.delete_calls == []


async def test_ambiguous_redis_delete_is_fail_closed_then_retried_idempotently(
    session,
    migrated_db,
    monkeypatch,
) -> None:
    account = await _seed_account(session, suffix="redis-retry")
    operation_id = uuid.uuid4()
    audit = await _seed_command(
        session,
        account=account,
        operation_id=operation_id,
        target_enabled=False,
        account_sequence=1,
    )
    await session.commit()
    redis_key = _redis_key(account.id)
    fake_redis = FakeRedis(
        values={redis_key},
        fail_delete_after_apply_once=True,
    )

    from social_reply.application.account_management import kill_switch_recovery

    monkeypatch.setattr(
        kill_switch_recovery.aioredis,
        "from_url",
        lambda _url: fake_redis,
    )

    first_recovered = await sweep_account_kill_switch_commands(batch_size=10)
    await session.refresh(audit)
    assert first_recovered == []
    assert audit.detail["status"] == "UNKNOWN"
    assert audit.detail["error_code"] == "REDIS_APPLY_UNCERTAIN"
    assert redis_key in fake_redis.values

    second_recovered = await sweep_account_kill_switch_commands(batch_size=10)
    await session.refresh(audit)
    assert second_recovered == [operation_id]
    assert audit.detail["status"] == "APPLIED"
    assert redis_key not in fake_redis.values

    third_recovered = await sweep_account_kill_switch_commands(batch_size=10)
    assert third_recovered == []
    assert fake_redis.delete_calls == [redis_key, redis_key]


async def test_uncertain_target_and_stale_ownership_remain_fail_closed(
    session,
    migrated_db,
    monkeypatch,
) -> None:
    former_owner = models.AdminUser(
        username="former-kill-switch-owner",
        password_hash="not-used",
        tenant_id="default",
        role="USER",
        must_change_password=False,
        status="active",
    )
    current_owner = models.AdminUser(
        username="current-kill-switch-owner",
        password_hash="not-used",
        tenant_id="default",
        role="USER",
        must_change_password=False,
        status="active",
    )
    session.add_all([former_owner, current_owner])
    await session.flush()
    uncertain_account = await _seed_account(session, suffix="uncertain-target")
    reassigned_account = await _seed_account(
        session,
        suffix="stale-owner",
        owner_user_id=current_owner.id,
    )
    uncertain_operation_id = uuid.uuid4()
    stale_owner_operation_id = uuid.uuid4()
    uncertain_audit = await _seed_command(
        session,
        account=uncertain_account,
        operation_id=uncertain_operation_id,
        target_enabled=None,
        account_sequence=1,
    )
    stale_owner_audit = await _seed_command(
        session,
        account=reassigned_account,
        operation_id=stale_owner_operation_id,
        target_enabled=False,
        account_sequence=1,
        actor_role="USER",
        owner_user_id=former_owner.id,
    )
    await session.commit()
    fake_redis = FakeRedis()

    from social_reply.application.account_management import kill_switch_recovery

    monkeypatch.setattr(
        kill_switch_recovery.aioredis,
        "from_url",
        lambda _url: fake_redis,
    )

    recovered = await sweep_account_kill_switch_commands(batch_size=10)

    await session.refresh(uncertain_audit)
    await session.refresh(stale_owner_audit)
    assert set(recovered) == {uncertain_operation_id, stale_owner_operation_id}
    assert uncertain_audit.detail["status"] == "REJECTED"
    assert uncertain_audit.detail["error_code"] == "KILL_SWITCH_TARGET_UNCERTAIN"
    assert stale_owner_audit.detail["status"] == "RECONFIRM_REQUIRED"
    assert stale_owner_audit.detail["error_code"] == "LEGACY_KILL_SWITCH_RECONFIRM_REQUIRED"
    assert _redis_key(uncertain_account.id) in fake_redis.values
    assert _redis_key(reassigned_account.id) in fake_redis.values
    assert fake_redis.delete_calls == []


async def test_sweep_limits_each_pass_by_account(
    session,
    migrated_db,
    monkeypatch,
) -> None:
    first_account = await _seed_account(session, suffix="batch-first")
    second_account = await _seed_account(session, suffix="batch-second")
    first_operation_id = uuid.uuid4()
    second_operation_id = uuid.uuid4()
    first_audit = await _seed_command(
        session,
        account=first_account,
        operation_id=first_operation_id,
        target_enabled=True,
        account_sequence=1,
    )
    second_audit = await _seed_command(
        session,
        account=second_account,
        operation_id=second_operation_id,
        target_enabled=True,
        account_sequence=1,
    )
    await session.commit()
    fake_redis = FakeRedis()

    from social_reply.application.account_management import kill_switch_recovery

    monkeypatch.setattr(
        kill_switch_recovery.aioredis,
        "from_url",
        lambda _url: fake_redis,
    )

    first_pass = await sweep_account_kill_switch_commands(batch_size=1)
    await session.refresh(first_audit)
    await session.refresh(second_audit)
    assert len(first_pass) == 1
    assert {first_audit.detail["status"], second_audit.detail["status"]} == {
        "APPLIED",
        "PENDING",
    }

    second_pass = await sweep_account_kill_switch_commands(batch_size=1)
    assert set(first_pass + second_pass) == {first_operation_id, second_operation_id}


async def _business_admin(session, suffix):
    password = "staff-admin-test-password"
    user = models.AdminUser(
        username=f"recovery-admin-{suffix}",
        password_hash=await hash_password(password),
        tenant_id="default",
        role="WORKSPACE_ADMIN",
        must_change_password=False,
        status="active",
    )
    session.add(user)
    await session.commit()
    result = await authenticate(user.username, password)
    assert result is not None
    principal, _token = result
    actor = channel_management.ChannelActor(
        actor=principal.actor, role="ADMIN", user_id=user.id, session_id=principal.session_id
    )
    return user, actor


@pytest.mark.parametrize("revocation", ["disabled", "demoted", "expired", "none"])
async def test_staff_redis_retry_rechecks_current_authority(
    session, migrated_db, monkeypatch, revocation
):
    from datetime import UTC, datetime, timedelta

    from social_reply.application.account_management import kill_switch_recovery

    user, actor = await _business_admin(session, revocation)
    account = await _seed_account(session, suffix=f"staff-{revocation}")
    await session.commit()
    key = _redis_key(account.id)
    redis = FakeRedis(values={key}, fail_delete_after_apply_once=True)
    monkeypatch.setattr(kill_switch_recovery.aioredis, "from_url", lambda _url: redis)
    with pytest.raises(RuntimeError, match="redis delete result unknown"):
        await channel_management.set_channel_account_kill_switch(
            tenant_id="default", account_id=account.id, actor=actor, enabled=False
        )
    audit = await session.scalar(
        select(models.AuditLog).where(
            models.AuditLog.action == "SET_PLATFORM_ACCOUNT_KILL_SWITCH",
            models.AuditLog.subject_id == str(account.id),
        )
    )
    assert audit is not None
    assert audit.detail["status"] == "UNKNOWN"
    if revocation == "disabled":
        user.status = "disabled"
    elif revocation == "demoted":
        user.role = "USER"
    elif revocation == "expired":
        stored = await session.get(models.AdminSession, actor.session_id)
        stored.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()
    await sweep_account_kill_switch_commands()
    await session.refresh(audit)
    if revocation == "none":
        assert audit.detail["status"] == "APPLIED"
        assert key not in redis.values
        return
    assert audit.detail["status"] == "REJECTED"
    assert key in redis.values
    calls = len(redis.delete_calls)
    user.status = "active"
    user.role = "WORKSPACE_ADMIN"
    await session.commit()
    assert await sweep_account_kill_switch_commands() == []
    assert len(redis.delete_calls) == calls
    assert key in redis.values


async def test_rejected_command_only_retries_protective_write(session, migrated_db, monkeypatch):
    from social_reply.application.account_management import kill_switch_recovery

    user, actor = await _business_admin(session, "safe-only")
    account = await _seed_account(session, suffix="safe-only")
    operation_id = uuid.uuid4()
    audit = models.AuditLog(
        id=operation_id,
        tenant_id="default",
        category="account_management",
        actor=actor.actor,
        action="SET_PLATFORM_ACCOUNT_KILL_SWITCH",
        subject_type="platform_account",
        subject_id=str(account.id),
        detail=build_pending_account_kill_switch_detail(
            operation_id=operation_id,
            tenant_id="default",
            account_id=account.id,
            target_enabled=False,
            account_sequence=1,
            actor_role="ADMIN",
            owner_user_id=None,
            actor_user_id=user.id,
            actor_session_id=actor.session_id,
        ),
    )
    session.add(audit)
    user.role = "USER"
    await session.commit()
    redis = FakeRedis(fail_set=True)
    monkeypatch.setattr(kill_switch_recovery.aioredis, "from_url", lambda _url: redis)
    assert await reconcile_account_kill_switch_command(operation_id) == "FAIL_CLOSED_PENDING"
    user.role = "WORKSPACE_ADMIN"
    await session.commit()
    redis.fail_set = False
    assert await reconcile_account_kill_switch_command(operation_id) == "REJECTED"
    assert _redis_key(account.id) in redis.values
    assert redis.delete_calls == []


async def test_rejected_batch_does_not_starve_later_valid_command(
    session, migrated_db, monkeypatch
):
    from datetime import UTC, datetime, timedelta

    from social_reply.application.account_management import kill_switch_recovery

    rows = []
    base = datetime.now(UTC) - timedelta(minutes=10)
    for index in range(4):
        account = await _seed_account(session, suffix=f"fair-{index}")
        audit = await _seed_command(
            session,
            account=account,
            operation_id=uuid.uuid4(),
            target_enabled=False,
            account_sequence=1,
        )
        audit.created_at = base + timedelta(seconds=index)
        if index < 3:
            # No persisted identity exists for a pre-upgrade bootstrap command.
            audit.detail = _command_detail(
                operation_id=audit.id,
                account_id=account.id,
                target_enabled=False,
                account_sequence=1,
            )
        rows.append((account, audit))
    await session.commit()
    redis = FakeRedis(values={_redis_key(account.id) for account, _ in rows})
    monkeypatch.setattr(kill_switch_recovery.aioredis, "from_url", lambda _url: redis)
    await sweep_account_kill_switch_commands(batch_size=2)
    await sweep_account_kill_switch_commands(batch_size=2)
    for _account, audit in rows:
        await session.refresh(audit)
    assert all(audit.detail["status"] == "RECONFIRM_REQUIRED" for _, audit in rows[:3])
    assert rows[-1][1].detail["status"] == "APPLIED"
    assert redis.delete_calls == [_redis_key(rows[-1][0].id)]


async def test_invalid_account_scope_is_quarantined_without_starving_valid_work(
    session, migrated_db, monkeypatch
):
    from datetime import UTC, datetime, timedelta

    from social_reply.application.account_management import kill_switch_recovery

    invalid = models.AuditLog(
        tenant_id="default",
        category="account_management",
        actor="system:legacy",
        action="SET_PLATFORM_ACCOUNT_KILL_SWITCH",
        subject_type="platform_account",
        subject_id="invalid-account-id",
        created_at=datetime.now(UTC) - timedelta(days=1),
        detail={"status": "UNKNOWN", "actor_role": "ADMIN", "enabled": False},
    )
    session.add(invalid)
    account = await _seed_account(session, suffix="after-invalid-scope")
    valid = await _seed_command(
        session,
        account=account,
        operation_id=uuid.uuid4(),
        target_enabled=True,
        account_sequence=1,
    )
    await session.commit()
    redis = FakeRedis()
    monkeypatch.setattr(kill_switch_recovery.aioredis, "from_url", lambda _url: redis)
    assert await sweep_account_kill_switch_commands(batch_size=1) == [invalid.id]
    await session.refresh(invalid)
    assert invalid.detail["status"] == "QUARANTINED"
    assert invalid.detail["fail_closed"] is False
    assert invalid.detail["manual_review_required"] is True
    assert await sweep_account_kill_switch_commands(batch_size=1) == [valid.id]
    await session.refresh(valid)
    assert valid.detail["status"] == "APPLIED"
    assert _redis_key(account.id) in redis.values
