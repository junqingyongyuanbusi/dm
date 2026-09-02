import httpx
import pytest
from sqlalchemy import select

from apps.api.main import create_app
from social_reply.application.account_management.auth import hash_password
from social_reply.infrastructure.database import models

pytestmark = pytest.mark.integration


class _MemoryRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def exists(self, key: str) -> int:
        return int(key in self.values)

    async def get(self, key: str):
        return self.values.get(key)

    async def set(
        self,
        key: str,
        value: str,
        *,
        nx: bool = False,
        ex: int | None = None,
    ):
        del ex
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)

    async def mget(self, keys: list[str]):
        return [self.values.get(key) for key in keys]

    async def eval(self, _script: str, _key_count: int, key: str, token: str) -> int:
        if self.values.get(key) != token:
            return 0
        self.values.pop(key, None)
        return 1

    async def aclose(self) -> None:
        return None


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://test",
        follow_redirects=False,
    )


async def _login(client: httpx.AsyncClient, username: str, password: str) -> str:
    await client.get("/admin/login")
    csrf = client.cookies["reply_admin_csrf"]
    response = await client.post(
        "/admin/login",
        data={"csrf_token": csrf, "username": username, "password": password},
    )
    assert response.status_code == 303
    return csrf


async def test_system_overview_and_audit_only_render_security_metadata(
    session,
    migrated_db,
    monkeypatch,
):
    from social_reply.application.account_management import saas_console

    memory_redis = _MemoryRedis()
    monkeypatch.setattr(saas_console.aioredis, "from_url", lambda _url: memory_redis)
    session.add_all(
        [
            models.AdminUser(
                username="overview-user-two",
                password_hash=await hash_password("overview-user-two-password-123"),
                tenant_id="default",
                role="USER",
                must_change_password=False,
                status="active",
            ),
            models.AdminUser(
                username="overview-user",
                password_hash=await hash_password("overview-user-password-123"),
                tenant_id="default",
                role="USER",
                must_change_password=False,
                status="active",
            ),
            models.AdminUser(
                username="overview-disabled",
                password_hash=await hash_password("overview-disabled-password-123"),
                tenant_id="default",
                role="USER",
                must_change_password=False,
                status="disabled",
            ),
            models.PlatformAccount(
                tenant_id="default",
                brand_id="customer-secret-brand",
                platform="telegram",
                name="customer-secret-account-name",
                public_id="customer-secret-account-public-id",
                status="active",
            ),
            models.AuditLog(
                tenant_id="default",
                category="user_management",
                actor="user:admin",
                action="SECURITY_VISIBLE_ACTION",
                subject_type="admin_user",
                subject_id="visible-user",
                detail={"username": "visible-user", "password": "never-render-this-password"},
            ),
            models.AuditLog(
                tenant_id="default",
                category="ingestion",
                actor="system",
                action="TENANT_BUSINESS_ACTION_MUST_NOT_RENDER",
                subject_type="raw_event",
                subject_id="customer-secret-event",
                detail={"payload": "customer-secret-payload"},
            ),
            models.AuditLog(
                tenant_id="default",
                category="release_safety",
                actor="system",
                action="RETIRE_REPLY_BUSINESS_PROMPT_WORK",
                subject_type="conversation",
                subject_id="customer-secret-conversation-id",
                detail={"outcome": "retired"},
            ),
        ]
    )
    await session.commit()

    async with _client() as client:
        await _login(client, "admin", "test-admin-password")
        overview = await client.get("/admin/system/overview")
        audit = await client.get("/admin/system/audit")

    assert overview.status_code == 200
    assert "活跃 USER" in overview.text
    assert "活跃 ADMIN" not in overview.text
    assert "活跃会话" in overview.text
    assert "全局急停" in overview.text
    assert "安全配置" in overview.text
    assert "SECURITY_VISIBLE_ACTION" in overview.text
    for forbidden_business_value in (
        "customer-secret-account-name",
        "customer-secret-brand",
        "customer-secret-account-public-id",
        "TENANT_BUSINESS_ACTION_MUST_NOT_RENDER",
        "customer-secret-payload",
        "customer-secret-conversation-id",
    ):
        assert forbidden_business_value not in overview.text

    assert audit.status_code == 200
    assert "SECURITY_VISIBLE_ACTION" in audit.text
    assert "visible-user" in audit.text
    assert "never-render-this-password" not in audit.text
    assert "[REDACTED]" in audit.text
    assert "TENANT_BUSINESS_ACTION_MUST_NOT_RENDER" not in audit.text
    assert "customer-secret-payload" not in audit.text
    assert "customer-secret-conversation-id" not in audit.text


async def test_database_user_cannot_open_any_system_console_page(session, migrated_db):
    session.add(
        models.AdminUser(
            username="system-forbidden-user",
            password_hash=await hash_password("system-forbidden-user-password-123"),
            tenant_id="default",
            role="USER",
            must_change_password=False,
            status="active",
        )
    )
    await session.commit()

    async with _client() as client:
        await _login(client, "system-forbidden-user", "system-forbidden-user-password-123")
        responses = [
            await client.get(path)
            for path in (
                "/admin/system/overview",
                "/admin/system/users",
                "/admin/system/safety",
                "/admin/system/audit",
            )
        ]

    assert all(response.status_code == 403 for response in responses)


async def test_login_and_logout_write_sanitized_authentication_audit(session, migrated_db):
    session.add(
        models.AdminUser(
            username="audited-login-user",
            password_hash=await hash_password("audited-login-user-password-123"),
            tenant_id="default",
            role="USER",
            must_change_password=False,
            status="active",
        )
    )
    await session.commit()

    async with _client() as client:
        csrf = await _login(
            client,
            "audited-login-user",
            "audited-login-user-password-123",
        )
        logout = await client.post("/auth/logout", data={"csrf_token": csrf})

    assert logout.status_code == 303
    audits = list(
        (
            await session.execute(
                select(models.AuditLog).where(models.AuditLog.category == "authentication")
            )
        ).scalars()
    )
    assert {audit.action for audit in audits} >= {"LOGIN_SUCCESS", "LOGOUT"}
    serialized_details = " ".join(str(audit.detail) for audit in audits)
    assert "audited-login-user" in serialized_details
    assert "audited-login-user-password-123" not in serialized_details
    assert "password_hash" not in serialized_details


async def test_global_kill_switch_change_is_audited(session, migrated_db, monkeypatch):
    from social_reply.application.account_management import admin_console

    memory_redis = _MemoryRedis()
    monkeypatch.setattr(admin_console.aioredis, "from_url", lambda _url: memory_redis)

    async with _client() as client:
        csrf = await _login(client, "admin", "test-admin-password")
        wrong_password = await client.post(
            "/admin/killswitch/toggle",
            data={
                "csrf_token": csrf,
                "scope": "global",
                "tenant_id": "default",
                "enabled": "true",
                "bootstrap_password": "wrong-bootstrap-password",
            },
        )
        assert wrong_password.status_code == 401
        assert "killswitch:global:default" not in memory_redis.values
        response = await client.post(
            "/admin/killswitch/toggle",
            data={
                "csrf_token": csrf,
                "scope": "global",
                "tenant_id": "default",
                "enabled": "true",
                "bootstrap_password": "test-admin-password",
            },
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/system/safety"
    assert memory_redis.values["killswitch:global:default"] == "1"
    audit = await session.scalar(
        select(models.AuditLog).where(
            models.AuditLog.category == "global_safety",
            models.AuditLog.action == "SET_GLOBAL_KILL_SWITCH",
        )
    )
    assert audit is not None
    assert audit.detail["enabled"] is True
    assert audit.detail["previous_enabled"] is False


async def test_superadmin_can_change_account_kill_switch(session, migrated_db):
    account = models.PlatformAccount(
        tenant_id="default",
        brand_id="default",
        platform="telegram",
        name="tenant-business-account",
        public_id="tenant-business-account",
        status="active",
    )
    session.add(account)
    await session.commit()
    account_id = account.id

    async with _client() as client:
        csrf = await _login(client, "admin", "test-admin-password")
        response = await client.post(
            "/admin/killswitch/toggle",
            data={
                "csrf_token": csrf,
                "scope": "account",
                "tenant_id": "default",
                "account_id": str(account_id),
                "enabled": "true",
            },
        )

    assert response.status_code == 303
    assert response.headers["location"] == f"/app/t/default/channels/accounts/{account_id}"


async def test_global_kill_switch_rolls_back_when_audit_commit_fails(
    migrated_db,
    monkeypatch,
):
    from social_reply.application.account_management import admin_console

    class _FailingSession:
        async def __aenter__(self):
            raise RuntimeError("audit database unavailable")

        async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
            return None

    memory_redis = _MemoryRedis()
    monkeypatch.setattr(admin_console.aioredis, "from_url", lambda _url: memory_redis)
    monkeypatch.setattr(
        admin_console,
        "get_session_factory",
        lambda: lambda: _FailingSession(),
    )

    async with _client() as client:
        csrf = await _login(client, "admin", "test-admin-password")
        response = await client.post(
            "/admin/killswitch/toggle",
            data={
                "csrf_token": csrf,
                "scope": "global",
                "tenant_id": "default",
                "enabled": "true",
                "bootstrap_password": "test-admin-password",
            },
        )

    assert response.status_code == 503
    assert response.json() == {"detail": "global_killswitch_audit_failed_rolled_back"}
    assert "killswitch:global:default" not in memory_redis.values
