# Phase 1 / Plan 1 — Reply Core 骨架与事件入站链路 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 搭建 Reply Core 的 FastAPI Modular Monolith 骨架，跑通 Chatwoot AgentBot Webhook → 验签 → raw_events → 去重/回声断路器 → normalized_events → 会话/状态机落库 的完整入站链路。

**Architecture:** 按 PLAN.md §十五 的模块化单体布局（apps/ + src/social_reply/），Webhook 入口只做验签+存 raw+快速 200，重活交给 Dramatiq actor；回声断路器与发送者甄别按 PLAN.md §四，人工接管检测按 §六，去重约束按 §十二。本计划不含决策管线与发送（Plan 2）、知识库与控制台（Plan 3）。

**Tech Stack:** Python 3.13 + uv、FastAPI、Pydantic v2 + pydantic-settings、SQLAlchemy 2 async + asyncpg、Alembic、Dramatiq[redis]、PostgreSQL 17（pgvector 镜像，向量能力 Plan 3 用）、Redis 8、pytest + pytest-asyncio + httpx。

**约定：**
- 所有命令在仓库根目录 `/Users/junqing/data/github/dm` 执行。
- 集成测试（标记 `integration`）需要 `docker compose -f deploy/docker-compose.yml up -d` 已启动。
- 代码注释使用中文，仅解释代码本身无法表达的约束。
- tenant_id 为常量 `"default"`（PLAN.md 裁决：单组织多品牌，保留列不做 RBAC/RLS）。
- 新会话默认自动化状态 `BOT_DRAFT_ONLY`（PLAN.md §十八 草稿先行）。

---

### Task 0: git 仓库与项目脚手架

**Files:**
- Create: `.gitignore`、`pyproject.toml`、`.env.example`、`README.md`
- Create: `src/social_reply/__init__.py` 及各子包 `__init__.py`
- Create: `src/social_reply/shared/config.py`
- Create: `apps/api/main.py`
- Test: `tests/unit/test_health.py`

- [ ] **Step 1: git init 与基础文件**

```bash
cd /Users/junqing/data/github/dm
git init -b main
```

创建 `.gitignore`：

```gitignore
__pycache__/
*.pyc
.venv/
.env
.pytest_cache/
.ruff_cache/
dist/
*.egg-info/
```

创建 `.env.example`：

```env
DATABASE_URL=postgresql+asyncpg://dev:dev@localhost:5432/social_reply
REDIS_URL=redis://localhost:6379/0
CHATWOOT_WEBHOOK_SECRET=change-me
CHATWOOT_SIGNATURE_TOLERANCE_SECONDS=300
```

- [ ] **Step 2: pyproject.toml**

```toml
[project]
name = "social-reply"
version = "0.1.0"
description = "社媒自动回复核心（Reply Core）"
requires-python = ">=3.13"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.30",
    "pydantic>=2.7",
    "pydantic-settings>=2.3",
    "sqlalchemy[asyncio]>=2.0.30",
    "asyncpg>=0.29",
    "alembic>=1.13",
    "dramatiq[redis]>=1.17",
    "httpx>=0.27",
]

[dependency-groups]
dev = [
    "pytest>=8.2",
    "pytest-asyncio>=0.23",
    "ruff>=0.5",
    "greenlet>=3.0",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/social_reply"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
markers = ["integration: 需要 docker compose 中的 PG/Redis"]

[tool.ruff]
line-length = 100
src = ["src", "apps", "tests"]

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]
```

- [ ] **Step 3: 包骨架与配置**

```bash
mkdir -p src/social_reply/{shared,domain/messages,domain/automation,connectors/chatwoot,application/event_ingestion,infrastructure/database,infrastructure/queue}
mkdir -p apps/api apps/worker tests/unit tests/integration deploy migrations
find src -type d -exec touch {}/__init__.py \;
touch apps/__init__.py apps/api/__init__.py apps/worker/__init__.py
```

创建 `src/social_reply/shared/config.py`：

```python
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://dev:dev@localhost:5432/social_reply"
    redis_url: str = "redis://localhost:6379/0"
    chatwoot_webhook_secret: str = "change-me"
    chatwoot_signature_tolerance_seconds: int = 300
    tenant_id: str = "default"
    default_automation_state: str = "BOT_DRAFT_ONLY"
    testing: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

- [ ] **Step 4: 写失败测试（健康检查）**

创建 `tests/unit/test_health.py`：

```python
import httpx

from apps.api.main import create_app


async def test_healthz_returns_ok():
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
```

- [ ] **Step 5: 运行确认失败**

Run: `uv sync && uv run pytest tests/unit/test_health.py -v`
Expected: FAIL（`ModuleNotFoundError: apps.api.main` 或 `create_app` 未定义）

- [ ] **Step 6: 实现 app 工厂**

创建 `apps/api/main.py`：

```python
from fastapi import FastAPI


def create_app() -> FastAPI:
    app = FastAPI(title="Reply Core")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
```

- [ ] **Step 7: 运行确认通过**

Run: `uv run pytest tests/unit/test_health.py -v`
Expected: PASS

- [ ] **Step 8: 提交**

```bash
git add -A
git commit -m "chore: 项目脚手架（uv + FastAPI + 配置）"
```

---

### Task 1: docker-compose 与数据库引擎

**Files:**
- Create: `deploy/docker-compose.yml`
- Create: `src/social_reply/infrastructure/database/engine.py`
- Test: `tests/integration/test_engine.py`

- [ ] **Step 1: docker-compose**

创建 `deploy/docker-compose.yml`：

```yaml
services:
  postgres:
    image: pgvector/pgvector:pg17
    environment:
      POSTGRES_USER: dev
      POSTGRES_PASSWORD: dev
      POSTGRES_DB: social_reply
    ports:
      - "5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U dev -d social_reply"]
      interval: 5s
      timeout: 3s
      retries: 10

  redis:
    image: redis:8-alpine
    ports:
      - "6379:6379"

volumes:
  pgdata:
```

Run: `docker compose -f deploy/docker-compose.yml up -d && docker compose -f deploy/docker-compose.yml ps`
Expected: postgres 与 redis 均为 running/healthy

- [ ] **Step 2: 写失败测试（引擎连通）**

创建 `tests/integration/test_engine.py`：

```python
import pytest
from sqlalchemy import text

from social_reply.infrastructure.database.engine import get_engine

pytestmark = pytest.mark.integration


async def test_engine_connects():
    engine = get_engine()
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT 1"))
        assert result.scalar() == 1
    await engine.dispose()
```

Run: `uv run pytest tests/integration/test_engine.py -v`
Expected: FAIL（get_engine 未定义）

- [ ] **Step 3: 实现引擎模块**

创建 `src/social_reply/infrastructure/database/engine.py`：

```python
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from social_reply.shared.config import get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/integration/test_engine.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add -A
git commit -m "feat: docker-compose（PG17+pgvector/Redis8）与异步数据库引擎"
```

---

### Task 2: 核心表模型与 Alembic 迁移

**Files:**
- Create: `src/social_reply/infrastructure/database/models.py`
- Create: `alembic.ini`、`migrations/env.py`、`migrations/script.py.mako`、`migrations/versions/0001_core_tables.py`
- Test: `tests/integration/test_schema.py`

- [ ] **Step 1: 写失败测试（表存在与约束生效）**

创建 `tests/integration/test_schema.py`：

```python
import uuid

import pytest
from sqlalchemy import insert, text
from sqlalchemy.exc import IntegrityError

from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_engine

pytestmark = pytest.mark.integration

EXPECTED_TABLES = {
    "platform_accounts", "contacts", "conversations", "conversation_mappings",
    "messages", "raw_events", "normalized_events", "automation_states",
    "outbox_messages", "audit_logs",
}


async def test_all_core_tables_exist(migrated_db):
    engine = get_engine()
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname='public'")
        )
        tables = {r[0] for r in rows}
    assert EXPECTED_TABLES <= tables


async def test_normalized_events_dedup_constraint(migrated_db, session):
    account_id = uuid.uuid4()
    await session.execute(insert(models.PlatformAccount).values(
        id=account_id, tenant_id="default", brand_id="b1", platform="telegram",
        name="acc", chatwoot_inbox_id=101,
    ))
    values = dict(
        id=uuid.uuid4(), tenant_id="default", platform="telegram",
        platform_account_id=account_id, external_event_id="cw_msg_1",
        event_type="dm.message.created",
    )
    await session.execute(insert(models.NormalizedEvent).values(**values))
    await session.commit()
    with pytest.raises(IntegrityError):
        await session.execute(insert(models.NormalizedEvent).values(
            **{**values, "id": uuid.uuid4()}
        ))
        await session.commit()
```

- [ ] **Step 2: conftest 夹具**

创建 `tests/integration/conftest.py`：

```python
import pytest

from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_engine, get_session_factory


@pytest.fixture
async def migrated_db():
    """每个测试用干净 schema：直接用 metadata 建表（迁移文件另行人工验证）"""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.drop_all)
        await conn.run_sync(models.Base.metadata.create_all)
    yield


@pytest.fixture
async def session(migrated_db):
    async with get_session_factory()() as s:
        yield s
        await s.rollback()
```

Run: `uv run pytest tests/integration/test_schema.py -v`
Expected: FAIL（models 未定义）

- [ ] **Step 3: 实现 models.py**

创建 `src/social_reply/infrastructure/database/models.py`：

```python
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, DateTime, ForeignKey, Integer, Text, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class PlatformAccount(Base):
    __tablename__ = "platform_accounts"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[str] = mapped_column(Text, default="default")
    brand_id: Mapped[str] = mapped_column(Text)
    platform: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    chatwoot_inbox_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    automation_default: Mapped[str] = mapped_column(Text, default="BOT_DRAFT_ONLY")
    status: Mapped[str] = mapped_column(Text, default="CONNECTED")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Contact(Base):
    __tablename__ = "contacts"
    __table_args__ = (UniqueConstraint("platform_account_id", "external_user_id"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[str] = mapped_column(Text, default="default")
    platform: Mapped[str] = mapped_column(Text)
    platform_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("platform_accounts.id"))
    external_user_id: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (UniqueConstraint("tenant_id", "conversation_key"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[str] = mapped_column(Text, default="default")
    brand_id: Mapped[str] = mapped_column(Text)
    platform: Mapped[str] = mapped_column(Text)
    platform_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("platform_accounts.id"))
    contact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contacts.id"))
    conversation_key: Mapped[str] = mapped_column(Text)
    channel_type: Mapped[str] = mapped_column(Text, default="dm")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ConversationMapping(Base):
    __tablename__ = "conversation_mappings"
    __table_args__ = (
        UniqueConstraint("chatwoot_account_id", "chatwoot_conversation_id"),
        UniqueConstraint("conversation_id"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    chatwoot_account_id: Mapped[int] = mapped_column(Integer)
    chatwoot_conversation_id: Mapped[int] = mapped_column(Integer)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id"))


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[uuid.UUID] = _uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id"))
    direction: Mapped[str] = mapped_column(Text)  # inbound / outbound
    sender_type: Mapped[str] = mapped_column(Text)  # contact / agent / bot
    text: Mapped[str | None] = mapped_column(Text)
    chatwoot_message_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    platform_message_id: Mapped[str | None] = mapped_column(Text, index=True)
    private: Mapped[bool] = mapped_column(Boolean, default=False)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RawEvent(Base):
    __tablename__ = "raw_events"
    id: Mapped[uuid.UUID] = _uuid_pk()
    source: Mapped[str] = mapped_column(Text)  # chatwoot / meta / telegram ...
    payload: Mapped[dict] = mapped_column(JSONB)
    headers: Mapped[dict] = mapped_column(JSONB, default=dict)
    processing_status: Mapped[str] = mapped_column(Text, default="PENDING")
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NormalizedEvent(Base):
    __tablename__ = "normalized_events"
    __table_args__ = (
        # PLAN.md §十二：多租户去重约束
        UniqueConstraint("tenant_id", "platform", "platform_account_id", "external_event_id"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[str] = mapped_column(Text, default="default")
    platform: Mapped[str] = mapped_column(Text)
    platform_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("platform_accounts.id"))
    external_event_id: Mapped[str] = mapped_column(Text)
    event_type: Mapped[str] = mapped_column(Text)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("conversations.id"))
    message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("messages.id"))
    raw_event_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("raw_events.id"))
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AutomationState(Base):
    __tablename__ = "automation_states"
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id"), primary_key=True
    )
    state: Mapped[str] = mapped_column(Text)  # PLAN.md §六 六状态
    state_version: Mapped[int] = mapped_column(Integer, default=1)
    human_agent_id: Mapped[str | None] = mapped_column(Text)
    last_human_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_bot_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resume_policy: Mapped[str] = mapped_column(Text, default="MANUAL")
    state_changed_reason: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class OutboxMessage(Base):
    __tablename__ = "outbox_messages"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[str] = mapped_column(Text, default="default")
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id"))
    platform_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("platform_accounts.id"))
    destination_type: Mapped[str] = mapped_column(Text)
    destination_id: Mapped[str] = mapped_column(Text)
    message_type: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB)
    idempotency_key: Mapped[str] = mapped_column(Text, unique=True)
    status: Mapped[str] = mapped_column(Text, default="PENDING")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(Text)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    platform_message_id: Mapped[str | None] = mapped_column(Text, index=True)
    chatwoot_message_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    last_error_code: Mapped[str | None] = mapped_column(Text)
    last_error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[str] = mapped_column(Text, default="default")
    category: Mapped[str] = mapped_column(Text)  # state_transition / ingestion / ...
    actor: Mapped[str] = mapped_column(Text)  # system / agent:<id> / bot
    action: Mapped[str] = mapped_column(Text)
    subject_type: Mapped[str] = mapped_column(Text)
    subject_id: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/integration/test_schema.py -v`
Expected: PASS（2 个测试）

- [ ] **Step 5: Alembic 配置与首个迁移**

创建 `alembic.ini`：

```ini
[alembic]
script_location = migrations
sqlalchemy.url =

[loggers]
keys = root

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
```

创建 `migrations/env.py`：

```python
import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from social_reply.infrastructure.database.models import Base
from social_reply.shared.config import get_settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = create_async_engine(get_settings().database_url)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


run_migrations_online()
```

创建 `migrations/script.py.mako`：

```mako
"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
"""
from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
```

生成迁移（autogenerate 对比空库与 models）：

```bash
mkdir -p migrations/versions
uv run alembic revision --autogenerate -m "core tables"
```

检查生成的 `migrations/versions/*_core_tables.py` 包含全部 10 张表后，重建验证：

```bash
docker compose -f deploy/docker-compose.yml exec postgres \
  psql -U dev -d social_reply -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
uv run alembic upgrade head
docker compose -f deploy/docker-compose.yml exec postgres \
  psql -U dev -d social_reply -c "\dt"
```

Expected: 列出 10 张表 + alembic_version

- [ ] **Step 6: 提交**

```bash
git add -A
git commit -m "feat: P1 核心表模型与 Alembic 迁移（含多租户去重约束）"
```

---

### Task 3: Chatwoot Webhook 验签（纯单元）

**Files:**
- Create: `src/social_reply/connectors/chatwoot/signature.py`
- Test: `tests/unit/test_chatwoot_signature.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/unit/test_chatwoot_signature.py`：

```python
import hashlib
import hmac

from social_reply.connectors.chatwoot.signature import verify_signature

SECRET = "s3cret"
BODY = b'{"event":"message_created"}'


def _sign(ts: str, body: bytes, secret: str = SECRET) -> str:
    digest = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def test_valid_signature_passes():
    assert verify_signature(
        secret=SECRET, timestamp="1000", body=BODY,
        signature=_sign("1000", BODY), now=1100, tolerance=300,
    )


def test_wrong_signature_rejected():
    assert not verify_signature(
        secret=SECRET, timestamp="1000", body=BODY,
        signature=_sign("1000", BODY, secret="other"), now=1100, tolerance=300,
    )


def test_stale_timestamp_rejected():
    # PLAN.md §十七：时间戳容忍窗口，超窗拒绝以防重放
    assert not verify_signature(
        secret=SECRET, timestamp="1000", body=BODY,
        signature=_sign("1000", BODY), now=2000, tolerance=300,
    )


def test_malformed_header_rejected():
    assert not verify_signature(
        secret=SECRET, timestamp="1000", body=BODY,
        signature="not-a-signature", now=1100, tolerance=300,
    )
    assert not verify_signature(
        secret=SECRET, timestamp="abc", body=BODY,
        signature=_sign("1000", BODY), now=1100, tolerance=300,
    )
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/unit/test_chatwoot_signature.py -v`
Expected: FAIL（verify_signature 未定义）

- [ ] **Step 3: 实现**

创建 `src/social_reply/connectors/chatwoot/signature.py`：

```python
import hashlib
import hmac


def verify_signature(
    *, secret: str, timestamp: str, body: bytes, signature: str, now: float, tolerance: int
) -> bool:
    """校验 Chatwoot v4.13+ Webhook 签名：sha256=HMAC-SHA256(secret, "{timestamp}.{body}")"""
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(now - ts) > tolerance:
        return False
    if not signature.startswith("sha256="):
        return False
    expected = hmac.new(
        secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(signature.removeprefix("sha256="), expected)
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/unit/test_chatwoot_signature.py -v`
Expected: PASS（4 个测试）

- [ ] **Step 5: 提交**

```bash
git add -A
git commit -m "feat: Chatwoot webhook HMAC 验签（时间戳窗口防重放）"
```

---

### Task 4: Chatwoot payload 解析与发送者甄别（纯单元）

**Files:**
- Create: `src/social_reply/connectors/chatwoot/normalizer.py`
- Test: `tests/unit/test_chatwoot_classify.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/unit/test_chatwoot_classify.py`：

```python
from social_reply.connectors.chatwoot.normalizer import (
    EventClass, ChatwootMessage, classify, parse_message_created,
)

BASE = {
    "event": "message_created",
    "id": 55,
    "content": "你好",
    "message_type": "incoming",
    "private": False,
    "created_at": "2026-07-14T10:00:00Z",
    "sender": {"id": 9, "type": "contact"},
    "conversation": {"id": 77, "inbox_id": 101, "status": "pending"},
    "account": {"id": 1},
}


def _payload(**overrides) -> dict:
    p = {**BASE, **overrides}
    return p


def test_parse_extracts_fields():
    msg = parse_message_created(_payload())
    assert msg == ChatwootMessage(
        chatwoot_message_id=55, content="你好", message_type="incoming",
        private=False, sender_id="9", sender_type="contact",
        chatwoot_conversation_id=77, chatwoot_inbox_id=101,
        chatwoot_account_id=1, occurred_at_iso="2026-07-14T10:00:00Z",
    )


def test_incoming_public_is_inbound_user():
    # PLAN.md §四 规则 2：仅 incoming 且非 private 进入决策管线
    assert classify(parse_message_created(_payload())) is EventClass.INBOUND_USER


def test_agent_outgoing_public_flips_human():
    # PLAN.md §六：outgoing 且 sender.type=user 且非 private → 人工接管
    p = _payload(message_type="outgoing", sender={"id": 3, "type": "user"})
    assert classify(parse_message_created(p)) is EventClass.AGENT_PUBLIC_REPLY


def test_bot_outgoing_is_reconcile_only():
    # PLAN.md §四 规则 4：agent_bot 的消息仅用于发送对账
    p = _payload(message_type="outgoing", sender={"id": 2, "type": "agent_bot"})
    assert classify(parse_message_created(p)) is EventClass.BOT_ECHO


def test_private_note_ignored():
    # 私有备注不触发任何状态变更
    p = _payload(message_type="outgoing", private=True, sender={"id": 3, "type": "user"})
    assert classify(parse_message_created(p)) is EventClass.IGNORE


def test_integer_message_type_from_api_payload():
    # Chatwoot 部分 payload 用整数 0=incoming/1=outgoing，需容错
    p = _payload(message_type=0)
    assert classify(parse_message_created(p)) is EventClass.INBOUND_USER
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/unit/test_chatwoot_classify.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**

创建 `src/social_reply/connectors/chatwoot/normalizer.py`：

```python
from dataclasses import dataclass
from enum import Enum, auto

_MESSAGE_TYPE_BY_INT = {0: "incoming", 1: "outgoing", 2: "activity", 3: "template"}


class EventClass(Enum):
    INBOUND_USER = auto()       # 进入决策管线
    AGENT_PUBLIC_REPLY = auto() # 人工坐席公开回复 → HUMAN_ACTIVE
    BOT_ECHO = auto()           # 机器人自身消息，仅对账
    IGNORE = auto()             # 私有备注 / activity 等


@dataclass(frozen=True)
class ChatwootMessage:
    chatwoot_message_id: int
    content: str | None
    message_type: str
    private: bool
    sender_id: str | None
    sender_type: str | None
    chatwoot_conversation_id: int
    chatwoot_inbox_id: int
    chatwoot_account_id: int
    occurred_at_iso: str | None


def parse_message_created(payload: dict) -> ChatwootMessage:
    mt = payload.get("message_type")
    if isinstance(mt, int):
        mt = _MESSAGE_TYPE_BY_INT.get(mt, "unknown")
    sender = payload.get("sender") or {}
    conversation = payload.get("conversation") or {}
    return ChatwootMessage(
        chatwoot_message_id=int(payload["id"]),
        content=payload.get("content"),
        message_type=mt or "unknown",
        private=bool(payload.get("private", False)),
        sender_id=str(sender["id"]) if "id" in sender else None,
        sender_type=sender.get("type"),
        chatwoot_conversation_id=int(conversation["id"]),
        chatwoot_inbox_id=int(conversation["inbox_id"]),
        chatwoot_account_id=int((payload.get("account") or {}).get("id", 0)),
        occurred_at_iso=payload.get("created_at"),
    )


def classify(msg: ChatwootMessage) -> EventClass:
    """PLAN.md §四 发送者甄别（self-echo 的 Outbox 比对在 processor 中另行执行）"""
    if msg.private:
        return EventClass.IGNORE
    if msg.message_type == "incoming":
        return EventClass.INBOUND_USER
    if msg.message_type == "outgoing":
        if msg.sender_type == "agent_bot":
            return EventClass.BOT_ECHO
        if msg.sender_type == "user":
            return EventClass.AGENT_PUBLIC_REPLY
    return EventClass.IGNORE
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/unit/test_chatwoot_classify.py -v`
Expected: PASS（6 个测试）

- [ ] **Step 5: 提交**

```bash
git add -A
git commit -m "feat: Chatwoot 消息解析与发送者甄别（回声断路器判定核心）"
```

---

### Task 5: 统一事件模型与会话键（纯单元）

**Files:**
- Create: `src/social_reply/domain/messages/events.py`
- Test: `tests/unit/test_events.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/unit/test_events.py`：

```python
from social_reply.domain.messages.events import build_dm_conversation_key


def test_dm_conversation_key_format():
    # PLAN.md §七：普通私信 = platform + platform_account + external_user
    key = build_dm_conversation_key(
        platform="telegram", platform_account_id="acc-uuid", external_user_id="tg_123"
    )
    assert key == "telegram:acc-uuid:tg_123"
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/unit/test_events.py -v`
Expected: FAIL

- [ ] **Step 3: 实现**

创建 `src/social_reply/domain/messages/events.py`：

```python
def build_dm_conversation_key(
    *, platform: str, platform_account_id: str, external_user_id: str
) -> str:
    return f"{platform}:{platform_account_id}:{external_user_id}"
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/unit/test_events.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add -A
git commit -m "feat: DM 会话键构造"
```

---

### Task 6: 状态机（初始化与人工接管翻转，纯单元 + 集成）

**Files:**
- Create: `src/social_reply/domain/automation/state_machine.py`
- Test: `tests/unit/test_state_machine.py`、`tests/integration/test_state_store.py`

- [ ] **Step 1: 写失败测试（纯逻辑）**

创建 `tests/unit/test_state_machine.py`：

```python
import pytest

from social_reply.domain.automation.state_machine import (
    AutomationStateEnum, can_transition,
)


def test_bot_active_to_human_active_on_agent_reply():
    assert can_transition(AutomationStateEnum.BOT_ACTIVE, AutomationStateEnum.HUMAN_ACTIVE)


def test_draft_only_to_human_active_on_agent_reply():
    assert can_transition(AutomationStateEnum.BOT_DRAFT_ONLY, AutomationStateEnum.HUMAN_ACTIVE)


def test_closed_is_terminal_except_reopen():
    assert not can_transition(AutomationStateEnum.CLOSED, AutomationStateEnum.HUMAN_ACTIVE)
    assert can_transition(AutomationStateEnum.CLOSED, AutomationStateEnum.BOT_ACTIVE)


@pytest.mark.parametrize("state", list(AutomationStateEnum))
def test_no_self_transition(state):
    assert not can_transition(state, state)
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/unit/test_state_machine.py -v`
Expected: FAIL

- [ ] **Step 3: 实现状态机**

创建 `src/social_reply/domain/automation/state_machine.py`：

```python
import uuid
from enum import StrEnum

from sqlalchemy import insert, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.infrastructure.database import models


class AutomationStateEnum(StrEnum):
    BOT_ACTIVE = "BOT_ACTIVE"
    BOT_DRAFT_ONLY = "BOT_DRAFT_ONLY"
    HANDOFF_PENDING = "HANDOFF_PENDING"
    HUMAN_ACTIVE = "HUMAN_ACTIVE"
    BOT_COOLDOWN = "BOT_COOLDOWN"
    CLOSED = "CLOSED"


# PLAN.md §六 状态图的允许转换（Plan 1 只使用其中初始化与 HUMAN_ACTIVE 翻转）
_ALLOWED: dict[AutomationStateEnum, set[AutomationStateEnum]] = {
    AutomationStateEnum.BOT_ACTIVE: {
        AutomationStateEnum.HANDOFF_PENDING,
        AutomationStateEnum.BOT_DRAFT_ONLY,
        AutomationStateEnum.HUMAN_ACTIVE,
        AutomationStateEnum.CLOSED,
    },
    AutomationStateEnum.BOT_DRAFT_ONLY: {
        AutomationStateEnum.HUMAN_ACTIVE,
        AutomationStateEnum.BOT_ACTIVE,
        AutomationStateEnum.CLOSED,
    },
    AutomationStateEnum.HANDOFF_PENDING: {
        AutomationStateEnum.HUMAN_ACTIVE,
        AutomationStateEnum.BOT_ACTIVE,
        AutomationStateEnum.CLOSED,
    },
    AutomationStateEnum.HUMAN_ACTIVE: {
        AutomationStateEnum.BOT_COOLDOWN,
        AutomationStateEnum.CLOSED,
    },
    AutomationStateEnum.BOT_COOLDOWN: {
        AutomationStateEnum.BOT_ACTIVE,
        AutomationStateEnum.HUMAN_ACTIVE,
        AutomationStateEnum.CLOSED,
    },
    AutomationStateEnum.CLOSED: {
        AutomationStateEnum.BOT_ACTIVE,
    },
}


def can_transition(src: AutomationStateEnum, dst: AutomationStateEnum) -> bool:
    return dst in _ALLOWED.get(src, set()) and src is not dst


async def ensure_state(
    session: AsyncSession, conversation_id: uuid.UUID, default_state: str
) -> None:
    """会话首次出现时初始化状态行；已存在则不动（幂等）"""
    stmt = (
        pg_insert(models.AutomationState)
        .values(conversation_id=conversation_id, state=default_state)
        .on_conflict_do_nothing(index_elements=["conversation_id"])
    )
    await session.execute(stmt)


async def flip_to_human_active(
    session: AsyncSession, conversation_id: uuid.UUID, agent_id: str | None, reason: str
) -> bool:
    """人工接管翻转：非 HUMAN_ACTIVE 才更新（幂等），state_version 自增（CAS 基础）。
    返回是否发生了翻转。"""
    stmt = (
        update(models.AutomationState)
        .where(
            models.AutomationState.conversation_id == conversation_id,
            models.AutomationState.state != AutomationStateEnum.HUMAN_ACTIVE,
        )
        .values(
            state=AutomationStateEnum.HUMAN_ACTIVE,
            state_version=models.AutomationState.state_version + 1,
            human_agent_id=agent_id,
            state_changed_reason=reason,
        )
    )
    result = await session.execute(stmt)
    flipped = result.rowcount > 0
    if flipped:
        await session.execute(
            insert(models.AuditLog).values(
                category="state_transition",
                actor=f"agent:{agent_id}" if agent_id else "system",
                action="HUMAN_ACTIVE",
                subject_type="conversation",
                subject_id=str(conversation_id),
                detail={"reason": reason},
            )
        )
    return flipped
```

- [ ] **Step 4: 运行单元确认通过**

Run: `uv run pytest tests/unit/test_state_machine.py -v`
Expected: PASS

- [ ] **Step 5: 写集成测试（落库行为）**

创建 `tests/integration/test_state_store.py`：

```python
import uuid

import pytest
from sqlalchemy import insert, select

from social_reply.domain.automation.state_machine import (
    ensure_state, flip_to_human_active,
)
from social_reply.infrastructure.database import models

pytestmark = pytest.mark.integration


async def _make_conversation(session) -> uuid.UUID:
    account_id, contact_id, conv_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await session.execute(insert(models.PlatformAccount).values(
        id=account_id, brand_id="b1", platform="telegram", name="acc", chatwoot_inbox_id=101,
    ))
    await session.execute(insert(models.Contact).values(
        id=contact_id, platform="telegram", platform_account_id=account_id,
        external_user_id="u1",
    ))
    await session.execute(insert(models.Conversation).values(
        id=conv_id, brand_id="b1", platform="telegram", platform_account_id=account_id,
        contact_id=contact_id, conversation_key=f"telegram:{account_id}:u1",
    ))
    return conv_id


async def test_ensure_state_is_idempotent(session):
    conv_id = await _make_conversation(session)
    await ensure_state(session, conv_id, "BOT_DRAFT_ONLY")
    await ensure_state(session, conv_id, "BOT_ACTIVE")  # 第二次不覆盖
    row = (await session.execute(
        select(models.AutomationState).where(
            models.AutomationState.conversation_id == conv_id)
    )).scalar_one()
    assert row.state == "BOT_DRAFT_ONLY"
    assert row.state_version == 1


async def test_flip_to_human_active_increments_version_and_audits(session):
    conv_id = await _make_conversation(session)
    await ensure_state(session, conv_id, "BOT_ACTIVE")
    assert await flip_to_human_active(session, conv_id, "3", "agent_public_reply") is True
    assert await flip_to_human_active(session, conv_id, "3", "agent_public_reply") is False
    row = (await session.execute(
        select(models.AutomationState).where(
            models.AutomationState.conversation_id == conv_id)
    )).scalar_one()
    assert row.state == "HUMAN_ACTIVE"
    assert row.state_version == 2
    audit_count = len((await session.execute(
        select(models.AuditLog).where(models.AuditLog.subject_id == str(conv_id))
    )).all())
    assert audit_count == 1
```

- [ ] **Step 6: 运行集成确认通过**

Run: `uv run pytest tests/integration/test_state_store.py -v`
Expected: PASS（2 个测试）

- [ ] **Step 7: 提交**

```bash
git add -A
git commit -m "feat: 自动化状态机（幂等初始化、HUMAN_ACTIVE 翻转、state_version 与审计）"
```

---

### Task 7: Webhook 端点（验签 → 存 raw → 入队 → 快速 200）

**Files:**
- Create: `src/social_reply/infrastructure/queue/broker.py`
- Create: `src/social_reply/application/event_ingestion/router.py`
- Create: `src/social_reply/application/event_ingestion/actors.py`
- Modify: `apps/api/main.py`
- Test: `tests/integration/test_webhook_endpoint.py`

- [ ] **Step 1: broker 模块（测试用 StubBroker）**

创建 `src/social_reply/infrastructure/queue/broker.py`：

```python
import dramatiq
from dramatiq.brokers.redis import RedisBroker
from dramatiq.brokers.stub import StubBroker

from social_reply.shared.config import get_settings


def setup_broker() -> dramatiq.Broker:
    settings = get_settings()
    if settings.testing:
        broker = StubBroker()
    else:
        broker = RedisBroker(url=settings.redis_url)
    dramatiq.set_broker(broker)
    return broker


broker = setup_broker()
```

- [ ] **Step 2: actor 占位（仅入队验证，处理逻辑在 Task 8）**

创建 `src/social_reply/application/event_ingestion/actors.py`：

```python
import asyncio
import threading

import dramatiq

import social_reply.infrastructure.queue.broker  # noqa: F401  确保 broker 先初始化

# 常驻事件循环：单例引擎的连接池绑定事件循环，
# 每条消息 asyncio.run() 会跨循环复用连接导致 "Event loop is closed"（Task 1 质量评审实测）
_loop = asyncio.new_event_loop()
threading.Thread(target=_loop.run_forever, daemon=True, name="actor-loop").start()


@dramatiq.actor(max_retries=3)
def process_chatwoot_event(raw_event_id: str) -> None:
    from social_reply.application.event_ingestion.processor import process_raw_event

    asyncio.run_coroutine_threadsafe(process_raw_event(raw_event_id), _loop).result()
```

同时创建空的 `src/social_reply/application/event_ingestion/processor.py`：

```python
async def process_raw_event(raw_event_id: str) -> None:  # Task 8 实现
    raise NotImplementedError
```

- [ ] **Step 3: 写失败测试**

创建 `tests/integration/test_webhook_endpoint.py`：

```python
import hashlib
import hmac
import json
import time
import uuid

import httpx
import pytest
from sqlalchemy import insert, select

from social_reply.infrastructure.database import models

pytestmark = pytest.mark.integration

SECRET = "change-me"


def _signed_headers(body: bytes, ts: int | None = None) -> dict[str, str]:
    ts = ts or int(time.time())
    digest = hmac.new(SECRET.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return {
        "X-Chatwoot-Signature": f"sha256={digest}",
        "X-Chatwoot-Timestamp": str(ts),
        "Content-Type": "application/json",
    }


@pytest.fixture
async def client(migrated_db, monkeypatch):
    monkeypatch.setenv("TESTING", "true")
    from social_reply.shared.config import get_settings
    get_settings.cache_clear()

    from apps.api.main import create_app
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    get_settings.cache_clear()


PAYLOAD = {
    "event": "message_created", "id": 55, "content": "你好",
    "message_type": "incoming", "private": False,
    "created_at": "2026-07-14T10:00:00Z",
    "sender": {"id": 9, "type": "contact"},
    "conversation": {"id": 77, "inbox_id": 101, "status": "pending"},
    "account": {"id": 1},
}


async def test_bad_signature_rejected_and_nothing_stored(client, session):
    body = json.dumps(PAYLOAD).encode()
    resp = await client.post(
        "/webhooks/chatwoot", content=body,
        headers={**_signed_headers(body), "X-Chatwoot-Signature": "sha256=bad"},
    )
    assert resp.status_code == 401
    assert (await session.execute(select(models.RawEvent))).first() is None


async def test_valid_webhook_stores_raw_and_enqueues(client, session):
    import dramatiq
    broker = dramatiq.get_broker()
    body = json.dumps(PAYLOAD).encode()
    resp = await client.post("/webhooks/chatwoot", content=body, headers=_signed_headers(body))
    assert resp.status_code == 200
    raw = (await session.execute(select(models.RawEvent))).scalar_one()
    assert raw.source == "chatwoot"
    assert raw.payload["id"] == 55
    queue = broker.queues["default"]
    assert queue.qsize() == 1


async def test_non_message_event_acknowledged_without_enqueue(client, session):
    import dramatiq
    broker = dramatiq.get_broker()
    payload = {**PAYLOAD, "event": "conversation_updated"}
    body = json.dumps(payload).encode()
    resp = await client.post("/webhooks/chatwoot", content=body, headers=_signed_headers(body))
    assert resp.status_code == 200
    # conversation_* 事件 Plan 2 处理，这里只存 raw 不入队
    assert (await session.execute(select(models.RawEvent))).scalar_one() is not None
    assert broker.queues["default"].qsize() == 1  # 仅上一个测试遗留？不——StubBroker 每测试重建
```

注意：最后一行断言依赖 broker 状态隔离。在 `tests/integration/conftest.py` 中追加夹具：

```python
@pytest.fixture(autouse=True)
def _flush_stub_broker():
    import dramatiq
    from dramatiq.brokers.stub import StubBroker

    broker = dramatiq.get_broker()
    if isinstance(broker, StubBroker):
        broker.flush_all()
    yield
```

并把上面最后一个断言改为 `assert broker.queues["default"].qsize() == 0`。

- [ ] **Step 4: 运行确认失败**

Run: `uv run pytest tests/integration/test_webhook_endpoint.py -v`
Expected: FAIL（路由不存在 → 404）

- [ ] **Step 5: 实现路由**

创建 `src/social_reply/application/event_ingestion/router.py`：

```python
import time

from fastapi import APIRouter, Request, Response
from sqlalchemy import insert

from social_reply.application.event_ingestion.actors import process_chatwoot_event
from social_reply.connectors.chatwoot.signature import verify_signature
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.shared.config import get_settings

router = APIRouter()


@router.post("/webhooks/chatwoot")
async def chatwoot_webhook(request: Request) -> Response:
    settings = get_settings()
    body = await request.body()
    ok = verify_signature(
        secret=settings.chatwoot_webhook_secret,
        timestamp=request.headers.get("X-Chatwoot-Timestamp", ""),
        body=body,
        signature=request.headers.get("X-Chatwoot-Signature", ""),
        now=time.time(),
        tolerance=settings.chatwoot_signature_tolerance_seconds,
    )
    if not ok:
        return Response(status_code=401)

    payload = await request.json()
    async with get_session_factory()() as session:
        result = await session.execute(
            insert(models.RawEvent)
            .values(source="chatwoot", payload=payload,
                    headers={"X-Chatwoot-Timestamp": request.headers.get("X-Chatwoot-Timestamp")})
            .returning(models.RawEvent.id)
        )
        raw_event_id = result.scalar_one()
        await session.commit()

    # PLAN.md §四：入口只做验签+存 raw+入队，重活交给 worker
    if payload.get("event") == "message_created":
        process_chatwoot_event.send(str(raw_event_id))
    return Response(status_code=200)
```

修改 `apps/api/main.py`：

```python
from fastapi import FastAPI

from social_reply.application.event_ingestion.router import router as ingestion_router


def create_app() -> FastAPI:
    app = FastAPI(title="Reply Core")
    app.include_router(ingestion_router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
```

同时在 `src/social_reply/shared/config.py` 的 `Settings` 中确认 `testing: bool = False` 可由环境变量 `TESTING` 注入（pydantic-settings 默认大小写不敏感，已满足）。

- [ ] **Step 6: 运行确认通过**

Run: `uv run pytest tests/integration/test_webhook_endpoint.py -v`
Expected: PASS（3 个测试）

- [ ] **Step 7: 提交**

```bash
git add -A
git commit -m "feat: Chatwoot webhook 端点（验签→raw_events→Dramatiq 入队→快速 200）"
```

---

### Task 8: 入站处理器（去重 → 回声断路 → 会话/消息落库 → 接管翻转）

**Files:**
- Modify: `src/social_reply/application/event_ingestion/processor.py`（替换占位实现）
- Test: `tests/integration/test_processor.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/integration/test_processor.py`：

```python
import uuid

import pytest
from sqlalchemy import func, insert, select

from social_reply.application.event_ingestion.processor import process_raw_event
from social_reply.infrastructure.database import models

pytestmark = pytest.mark.integration


def _payload(**overrides) -> dict:
    p = {
        "event": "message_created", "id": 55, "content": "你好",
        "message_type": "incoming", "private": False,
        "created_at": "2026-07-14T10:00:00Z",
        "sender": {"id": 9, "type": "contact", "name": "张三"},
        "conversation": {"id": 77, "inbox_id": 101, "status": "pending"},
        "account": {"id": 1},
    }
    p.update(overrides)
    return p


async def _seed_account(session) -> uuid.UUID:
    account_id = uuid.uuid4()
    await session.execute(insert(models.PlatformAccount).values(
        id=account_id, brand_id="b1", platform="telegram", name="tg-main",
        chatwoot_inbox_id=101, automation_default="BOT_DRAFT_ONLY",
    ))
    await session.commit()
    return account_id


async def _seed_raw(session, payload: dict) -> str:
    result = await session.execute(
        insert(models.RawEvent).values(source="chatwoot", payload=payload)
        .returning(models.RawEvent.id)
    )
    raw_id = result.scalar_one()
    await session.commit()
    return str(raw_id)


async def _count(session, model) -> int:
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


async def test_inbound_user_message_full_chain(session):
    account_id = await _seed_account(session)
    raw_id = await _seed_raw(session, _payload())

    await process_raw_event(raw_id)

    conv = (await session.execute(select(models.Conversation))).scalar_one()
    assert conv.platform_account_id == account_id
    assert conv.conversation_key == f"telegram:{account_id}:9"
    mapping = (await session.execute(select(models.ConversationMapping))).scalar_one()
    assert mapping.chatwoot_conversation_id == 77
    msg = (await session.execute(select(models.Message))).scalar_one()
    assert msg.direction == "inbound" and msg.chatwoot_message_id == 55
    state = (await session.execute(select(models.AutomationState))).scalar_one()
    assert state.state == "BOT_DRAFT_ONLY"  # 账号默认草稿先行
    norm = (await session.execute(select(models.NormalizedEvent))).scalar_one()
    assert norm.external_event_id == "55"
    assert norm.event_type == "dm.message.created"
    raw = (await session.execute(select(models.RawEvent))).scalar_one()
    assert raw.processing_status == "PROCESSED"


async def test_duplicate_delivery_is_idempotent(session):
    await _seed_account(session)
    raw1 = await _seed_raw(session, _payload())
    raw2 = await _seed_raw(session, _payload())  # Chatwoot 重投同一条消息

    await process_raw_event(raw1)
    await process_raw_event(raw2)

    assert await _count(session, models.NormalizedEvent) == 1
    assert await _count(session, models.Message) == 1


async def test_agent_public_reply_flips_human_active(session):
    await _seed_account(session)
    await process_raw_event(await _seed_raw(session, _payload()))  # 先建会话
    agent_payload = _payload(
        id=56, message_type="outgoing", sender={"id": 3, "type": "user", "name": "客服A"},
    )
    await process_raw_event(await _seed_raw(session, agent_payload))

    state = (await session.execute(select(models.AutomationState))).scalar_one()
    assert state.state == "HUMAN_ACTIVE"
    assert state.state_version == 2
    msg_count = await _count(session, models.Message)
    assert msg_count == 2  # 坐席消息也落库（direction=outbound, sender_type=agent）


async def test_self_echo_via_outbox_is_skipped(session):
    await _seed_account(session)
    await process_raw_event(await _seed_raw(session, _payload()))
    conv = (await session.execute(select(models.Conversation))).scalar_one()
    account = (await session.execute(select(models.PlatformAccount))).scalar_one()
    # 模拟 Plan 2 的 Outbox 已发送记录：chatwoot_message_id=99
    await session.execute(insert(models.OutboxMessage).values(
        conversation_id=conv.id, platform_account_id=account.id,
        destination_type="chatwoot_conversation", destination_id="77",
        message_type="text", payload={}, idempotency_key="k1",
        status="SENT", chatwoot_message_id=99,
    ))
    await session.commit()

    echo_payload = _payload(
        id=99, message_type="outgoing", sender={"id": 2, "type": "agent_bot"},
    )
    await process_raw_event(await _seed_raw(session, echo_payload))

    state = (await session.execute(select(models.AutomationState))).scalar_one()
    assert state.state == "BOT_DRAFT_ONLY"  # 未被误翻转
    assert await _count(session, models.Message) == 1  # 回声不入消息表
    raw_rows = (await session.execute(select(models.RawEvent))).scalars().all()
    assert raw_rows[-1].processing_status == "SKIPPED_ECHO"


async def test_unknown_inbox_is_skipped(session):
    await _seed_account(session)
    raw_id = await _seed_raw(session, _payload(conversation={
        "id": 88, "inbox_id": 999, "status": "pending",
    }))
    await process_raw_event(raw_id)
    assert await _count(session, models.Conversation) == 0
    raw = (await session.execute(
        select(models.RawEvent).order_by(models.RawEvent.received_at.desc())
    )).scalars().first()
    assert raw.processing_status == "SKIPPED_UNKNOWN_INBOX"
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_processor.py -v`
Expected: FAIL（NotImplementedError）

- [ ] **Step 3: 实现 processor**

替换 `src/social_reply/application/event_ingestion/processor.py`：

```python
import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.connectors.chatwoot.normalizer import (
    ChatwootMessage, EventClass, classify, parse_message_created,
)
from social_reply.domain.automation.state_machine import ensure_state, flip_to_human_active
from social_reply.domain.messages.events import build_dm_conversation_key
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.shared.config import get_settings


async def process_raw_event(raw_event_id: str) -> None:
    async with get_session_factory()() as session:
        raw = (await session.execute(
            select(models.RawEvent).where(models.RawEvent.id == uuid.UUID(raw_event_id))
        )).scalar_one()
        status = await _process(session, raw)
        await session.execute(
            update(models.RawEvent)
            .where(models.RawEvent.id == raw.id)
            .values(processing_status=status, processed_at=datetime.now().astimezone())
        )
        await session.commit()


async def _process(session: AsyncSession, raw: models.RawEvent) -> str:
    msg = parse_message_created(raw.payload)

    account = (await session.execute(
        select(models.PlatformAccount)
        .where(models.PlatformAccount.chatwoot_inbox_id == msg.chatwoot_inbox_id)
    )).scalar_one_or_none()
    if account is None:
        return "SKIPPED_UNKNOWN_INBOX"

    # PLAN.md §四 回声断路器规则 1：与 Outbox 已记录的 chatwoot_message_id 比对
    if await _is_self_echo(session, msg):
        return "SKIPPED_ECHO"

    event_class = classify(msg)
    if event_class is EventClass.IGNORE:
        return "SKIPPED_IGNORED"
    if event_class is EventClass.BOT_ECHO:
        # sender=agent_bot 但 Outbox 无记录（如手动经 bot token 发送）：仅对账，不入管线
        return "SKIPPED_ECHO"

    # PLAN.md §十二 去重：normalized_events 唯一约束，冲突即重复投递
    settings = get_settings()
    dedup = await session.execute(
        pg_insert(models.NormalizedEvent)
        .values(
            tenant_id=settings.tenant_id,
            platform=account.platform,
            platform_account_id=account.id,
            external_event_id=str(msg.chatwoot_message_id),
            event_type=_event_type(event_class),
            raw_event_id=raw.id,
            occurred_at=_parse_ts(msg.occurred_at_iso),
        )
        .on_conflict_do_nothing(
            index_elements=["tenant_id", "platform", "platform_account_id", "external_event_id"]
        )
        .returning(models.NormalizedEvent.id)
    )
    normalized_id = dedup.scalar_one_or_none()
    if normalized_id is None:
        return "SKIPPED_DUPLICATE"

    conversation = await _ensure_conversation(session, account, msg)
    await ensure_state(session, conversation.id, account.automation_default)

    message_id = await _store_message(session, conversation.id, msg, event_class)
    await session.execute(
        update(models.NormalizedEvent)
        .where(models.NormalizedEvent.id == normalized_id)
        .values(conversation_id=conversation.id, message_id=message_id)
    )

    if event_class is EventClass.AGENT_PUBLIC_REPLY:
        # PLAN.md §六：仅人工坐席 outgoing 非 private 触发接管
        await flip_to_human_active(
            session, conversation.id, msg.sender_id, "agent_public_reply"
        )
    return "PROCESSED"


async def _is_self_echo(session: AsyncSession, msg: ChatwootMessage) -> bool:
    row = await session.execute(
        select(models.OutboxMessage.id)
        .where(models.OutboxMessage.chatwoot_message_id == msg.chatwoot_message_id)
        .limit(1)
    )
    return row.first() is not None


def _event_type(event_class: EventClass) -> str:
    return {
        EventClass.INBOUND_USER: "dm.message.created",
        EventClass.AGENT_PUBLIC_REPLY: "agent.message.created",
    }[event_class]


def _parse_ts(iso: str | None) -> datetime | None:
    if not iso:
        return None
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


async def _ensure_conversation(
    session: AsyncSession, account: models.PlatformAccount, msg: ChatwootMessage
) -> models.Conversation:
    # 先按 Chatwoot 会话映射找（坐席消息的 sender 是坐席而非联系人，不能用 sender 建键）
    mapped = (await session.execute(
        select(models.Conversation)
        .join(models.ConversationMapping,
              models.ConversationMapping.conversation_id == models.Conversation.id)
        .where(
            models.ConversationMapping.chatwoot_account_id == msg.chatwoot_account_id,
            models.ConversationMapping.chatwoot_conversation_id == msg.chatwoot_conversation_id,
        )
    )).scalar_one_or_none()
    if mapped is not None:
        return mapped

    contact = await _ensure_contact(session, account, msg)
    key = build_dm_conversation_key(
        platform=account.platform,
        platform_account_id=str(account.id),
        external_user_id=contact.external_user_id,
    )
    # PLAN.md §十：查不到即按 conversation_key upsert，消除竞态
    await session.execute(
        pg_insert(models.Conversation)
        .values(
            id=uuid.uuid4(), tenant_id=get_settings().tenant_id, brand_id=account.brand_id,
            platform=account.platform, platform_account_id=account.id,
            contact_id=contact.id, conversation_key=key,
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "conversation_key"])
    )
    conversation = (await session.execute(
        select(models.Conversation).where(
            models.Conversation.tenant_id == get_settings().tenant_id,
            models.Conversation.conversation_key == key,
        )
    )).scalar_one()
    await session.execute(
        pg_insert(models.ConversationMapping)
        .values(
            chatwoot_account_id=msg.chatwoot_account_id,
            chatwoot_conversation_id=msg.chatwoot_conversation_id,
            conversation_id=conversation.id,
        )
        .on_conflict_do_nothing(
            index_elements=["chatwoot_account_id", "chatwoot_conversation_id"]
        )
    )
    return conversation


async def _ensure_contact(
    session: AsyncSession, account: models.PlatformAccount, msg: ChatwootMessage
) -> models.Contact:
    external_user_id = msg.sender_id or "unknown"
    await session.execute(
        pg_insert(models.Contact)
        .values(
            id=uuid.uuid4(), platform=account.platform, platform_account_id=account.id,
            external_user_id=external_user_id,
        )
        .on_conflict_do_nothing(index_elements=["platform_account_id", "external_user_id"])
    )
    return (await session.execute(
        select(models.Contact).where(
            models.Contact.platform_account_id == account.id,
            models.Contact.external_user_id == external_user_id,
        )
    )).scalar_one()


async def _store_message(
    session: AsyncSession, conversation_id: uuid.UUID, msg: ChatwootMessage,
    event_class: EventClass,
) -> uuid.UUID:
    message_id = uuid.uuid4()
    inbound = event_class is EventClass.INBOUND_USER
    await session.execute(
        pg_insert(models.Message).values(
            id=message_id,
            conversation_id=conversation_id,
            direction="inbound" if inbound else "outbound",
            sender_type="contact" if inbound else "agent",
            text=msg.content,
            chatwoot_message_id=msg.chatwoot_message_id,
            private=msg.private,
            occurred_at=_parse_ts(msg.occurred_at_iso),
        )
    )
    return message_id
```

注意一个已知的坐席消息会话归属问题：坐席回复时 payload 的 `sender` 是坐席，`_ensure_conversation` 优先走 ConversationMapping 命中已有会话，因此不会用坐席 id 误建联系人；只有映射不存在（极端乱序）时才会退化——测试 `test_agent_public_reply_flips_human_active` 先注入用户消息建立映射，覆盖正常路径。

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/integration/test_processor.py -v`
Expected: PASS（5 个测试）

- [ ] **Step 5: 全量回归**

Run: `uv run pytest -v`
Expected: 全部 PASS

- [ ] **Step 6: 提交**

```bash
git add -A
git commit -m "feat: 入站处理器（去重/回声断路/会话映射 upsert/接管翻转）"
```

---

### Task 9: worker 入口、冒烟脚本与 README

**Files:**
- Create: `apps/worker/main.py`
- Create: `scripts/send_test_webhook.py`
- Modify: `README.md`

- [ ] **Step 1: worker 入口**

创建 `apps/worker/main.py`：

```python
"""Dramatiq worker 入口：uv run dramatiq apps.worker.main"""
import social_reply.application.event_ingestion.actors  # noqa: F401  注册 actor
```

- [ ] **Step 2: 冒烟脚本**

创建 `scripts/send_test_webhook.py`：

```python
"""本地冒烟：以 Chatwoot 签名格式向本地 API 发送一条模拟 message_created"""
import hashlib
import hmac
import json
import sys
import time

import httpx

SECRET = sys.argv[1] if len(sys.argv) > 1 else "change-me"
payload = {
    "event": "message_created", "id": int(time.time()), "content": "冒烟测试：可以提现吗？",
    "message_type": "incoming", "private": False,
    "created_at": "2026-07-14T10:00:00Z",
    "sender": {"id": 9, "type": "contact", "name": "冒烟用户"},
    "conversation": {"id": 77, "inbox_id": 101, "status": "pending"},
    "account": {"id": 1},
}
body = json.dumps(payload).encode()
ts = str(int(time.time()))
digest = hmac.new(SECRET.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
resp = httpx.post(
    "http://localhost:8000/webhooks/chatwoot",
    content=body,
    headers={
        "X-Chatwoot-Signature": f"sha256={digest}",
        "X-Chatwoot-Timestamp": ts,
        "Content-Type": "application/json",
    },
)
print(resp.status_code, resp.text)
```

- [ ] **Step 3: README 运行手册**

替换 `README.md`：

```markdown
# Reply Core（社媒自动回复核心）

架构见 `PLAN.md`。当前进度：Phase 1 / Plan 1 —— Chatwoot 事件入站链路。

## 本地运行

```bash
docker compose -f deploy/docker-compose.yml up -d
uv sync
cp .env.example .env
uv run alembic upgrade head

# 准备一个测试账号（chatwoot_inbox_id=101）
docker compose -f deploy/docker-compose.yml exec postgres psql -U dev -d social_reply -c \
  "INSERT INTO platform_accounts (id, tenant_id, brand_id, platform, name, chatwoot_inbox_id, automation_default, status, created_at)
   VALUES (gen_random_uuid(), 'default', 'b1', 'telegram', 'tg-main', 101, 'BOT_DRAFT_ONLY', 'CONNECTED', now());"

# 终端 1：API
uv run uvicorn apps.api.main:app --port 8000
# 终端 2：worker
uv run dramatiq apps.worker.main
# 终端 3：冒烟
uv run python scripts/send_test_webhook.py
```

验收：`raw_events.processing_status='PROCESSED'`，`conversations`/`messages`/`automation_states`（BOT_DRAFT_ONLY）各一行。

## 测试

```bash
uv run pytest -m "not integration"   # 纯单元
docker compose -f deploy/docker-compose.yml up -d
uv run pytest                        # 全量
```
```

- [ ] **Step 4: 端到端冒烟验证**

按 README 顺序启动 API 与 worker，运行 `uv run python scripts/send_test_webhook.py`，然后：

```bash
docker compose -f deploy/docker-compose.yml exec postgres psql -U dev -d social_reply -c \
  "SELECT processing_status FROM raw_events ORDER BY received_at DESC LIMIT 1;"
```

Expected: `PROCESSED`；`SELECT state FROM automation_states;` 返回 `BOT_DRAFT_ONLY`

- [ ] **Step 5: 提交**

```bash
git add -A
git commit -m "feat: worker 入口、冒烟脚本与运行手册"
```

---

## Self-Review 记录

1. **Spec 覆盖**（对照 PLAN.md 相关章节）：§四 入口三步（验签/存 raw/快速 200）→ Task 7；§四 回声断路器规则 1-4 → Task 4/8（规则 5 属评论 Adapter，Plan 3）；§十二 去重约束含 tenant_id → Task 2/8；§六 HUMAN_ACTIVE 甄别与 state_version → Task 6/8；§十 conversation_mappings upsert 协议 → Task 8；§十八 草稿先行默认态 → Task 8（automation_default）。缺口（有意延后）：conversation_status_changed 双向同步、聚合/拉模型、Outbox worker、Final Guard → Plan 2。
2. **占位符扫描**：无 TBD/TODO；所有测试与实现均给出完整代码。
3. **类型一致性**：`ChatwootMessage` 字段在 Task 4/8 一致；`EventClass.BOT_ECHO` 命名统一（无 BOT_ECHO_CANDIDATE 残留）；`process_raw_event(raw_event_id: str)` 与 actor 调用签名一致。
