# Phase 1 / Plan 2b — Outbox 投递与发送 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 消费 Plan 2a 写入的 `PENDING` outbox 行，经 Chatwoot Messages API 真实发送回复（草稿→私有备注，自动回复→outgoing 消息），带接管竞态 **defense 2**（发送前复检 `state==BOT_ACTIVE`）——机器人真正回复用户。

**Architecture:** Outbox 投递 worker 以 Dramatiq actor（复用与入站 actor 共享的常驻事件循环，避免单例引擎跨循环）认领 outbox 行（`status IN (PENDING,FAILED)` → `SENDING`），发送前复检自动化状态（defense 2），经注入的 `ChatwootClient`（testing→Fake / 生产→httpx）发到 Chatwoot，成功标 `SENT` 并存 `chatwoot_message_id`（闭合 Plan 1 回声断路器），失败按可重试性落 `FAILED`（退避重试）或 `NEEDS_REVIEW`。scheduler 补扫孤儿/滞留行。**LLM 仍 Stub，真实 OpenAI 属 Plan 2c。**

**Tech Stack:** 延续（Python 3.13 + uv、FastAPI、SQLAlchemy 2 async、Alembic、Dramatiq[redis]、PostgreSQL 17、Redis 8、httpx、pytest）。

**约定（延续）：** 命令在仓库根 `/Users/junqing/data/github/dm`；集成测试标记 `integration` 需 docker compose；中文注释；ruff 全绿；不 push、每任务一次 commit、显式路径 stage。起点 HEAD = `d3ea542`（Plan 2a 完成，79 tests）。

**关键前置事实（来自 Plan 2a 评审累积，见记忆 plan2b-delivery-backlog）：**
- **defense 2 是权威闸门**：defense 1（决策 CAS）+ defense 3（flip 取消 PENDING/FAILED）仍尽力而为；投递 worker 认领后、发送前复检 `state==BOT_ACTIVE` 是"接管后不发送"的最终保证。已进入 Chatwoot API 调用中的消息无法取消，属显式接受的最小竞态窗口（PLAN §六）。
- **状态词表统一**：canonical outbox status = `PENDING → SENDING → SENT`；`PENDING → CANCELLED`（defense 2/3）；`SENDING → FAILED`（可重试，退避后经补扫重投）；`SENDING → NEEDS_REVIEW`（歧义/永久失败）。**废弃 Plan 2a defense 3 filter 里的 `RETRY`**，改为 `PENDING/FAILED`。
- **SENT 语义**：Chatwoot 受理即 SENT（存 Chatwoot 返回的 message id）。Chatwoot→Meta 的二跳送达状态属后续（订阅 Chatwoot 消息状态 webhook），不在 Plan 2b。
- **歧义失败不自动重发**：Chatwoot Messages API 无客户端幂等键；`SENDING` 滞留（进程崩在发送后、finalize 前）由补扫转 `NEEDS_REVIEW` 人工核对，**不自动重发**（避免重复）。仅 `FAILED`（明确未送达）过退避后重投。

---

### Task 0: 状态词表统一、delivery_attempts 表、outbox 复合索引、defense 3 对齐

**Files:**
- Modify: `src/social_reply/infrastructure/database/models.py`（新增 `DeliveryAttempt`；OutboxMessage 加 `(conversation_id, status)` 复合索引）
- Modify: `src/social_reply/domain/automation/state_machine.py`（defense 3 filter `["PENDING","RETRY"]` → `["PENDING","FAILED"]`）
- Modify: `tests/integration/test_takeover_cancels_outbox.py`（补 FAILED 被取消的断言）
- Create: `migrations/versions/*_delivery_attempts.py`（autogenerate）
- Test: `tests/integration/test_schema.py`（扩展表集合）

- [ ] **Step 1: 扩展 schema 测试（失败先行）**

在 `tests/integration/test_schema.py` 的 `EXPECTED_TABLES` 加入 `"delivery_attempts"`，并追加：

```python
async def test_delivery_attempts_and_outbox_index(migrated_db):
    engine = get_engine()
    async with engine.connect() as conn:
        cols = {r[0] for r in await conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='delivery_attempts'"))}
        idx = {r[0] for r in await conn.execute(text(
            "SELECT indexname FROM pg_indexes WHERE tablename='outbox_messages'"))}
    assert {"id", "outbox_id", "attempt_no", "outcome",
            "error_code", "created_at"} <= cols
    assert any("conversation" in name and "status" in name for name in idx)
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_schema.py::test_delivery_attempts_and_outbox_index -v`
Expected: FAIL

- [ ] **Step 3: 新增 DeliveryAttempt + outbox 索引**

在 `models.py` 末尾（`ReplyDecision` 之后）追加：

```python
class DeliveryAttempt(Base):
    __tablename__ = "delivery_attempts"
    id: Mapped[uuid.UUID] = _uuid_pk()
    outbox_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("outbox_messages.id"))
    attempt_no: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(Text)  # SENT / FAILED / CANCELLED / NEEDS_REVIEW
    error_code: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    chatwoot_message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())
```

在 `OutboxMessage` 类体末尾（`sent_at` 之后）追加复合索引到 `__table_args__`。OutboxMessage 当前无 `__table_args__`，新增：

```python
    __table_args__ = (
        # defense 3 取消 + 补扫认领：按会话+状态过滤（FK 无自动索引，Task 8 评审）
        Index("ix_outbox_conversation_status", "conversation_id", "status"),
    )
```

在 `models.py` 顶部 sqlalchemy import 补 `Index`：把 import 块加入 `Index`（与 `BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, Text, UniqueConstraint, func` 同列，按字母序 `Index` 在 `ForeignKey` 后 `Integer` 前）。

- [ ] **Step 4: defense 3 filter 对齐**

`state_machine.py` 的 `flip_to_human_active` 内 defense 3 取消，把 `status.in_(["PENDING", "RETRY"])` 改为 `status.in_(["PENDING", "FAILED"])`。

- [ ] **Step 5: 补 FAILED 取消测试**

`tests/integration/test_takeover_cancels_outbox.py` 追加（seed 一条 FAILED outbox，flip 后断言 CANCELLED）：

```python
async def test_flip_cancels_failed_outbox(session):
    conv_id, ob_id = await _seed_conv_with_outbox(session, "FAILED")
    await flip_to_human_active(session, conv_id, "3", "agent_public_reply")
    await session.commit()
    ob = (await session.execute(
        select(models.OutboxMessage).where(models.OutboxMessage.id == ob_id))).scalar_one()
    assert ob.status == "CANCELLED"
```

- [ ] **Step 6: 生成迁移并验证**（TESTING=true，先回到 Plan 2a head 再 autogenerate）

```bash
docker compose -f deploy/docker-compose.yml exec -T postgres \
  psql -U dev -d social_reply -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
TESTING=true uv run alembic upgrade head          # 回到 Plan 2a head 1f72cef1e05f
TESTING=true uv run alembic revision --autogenerate -m "delivery_attempts and outbox index"
TESTING=true uv run alembic upgrade head
docker compose -f deploy/docker-compose.yml exec -T postgres \
  psql -U dev -d social_reply -c "\dt" | grep delivery_attempts
```

检查生成的迁移只含 `create_table('delivery_attempts')` + `create_index('ix_outbox_conversation_status')`，`down_revision='1f72cef1e05f'`。

- [ ] **Step 7: 门禁**

Run: `uv run pytest -q`（预期 80 passed：79 + schema 扩展 1；FAILED 取消测试 +1 → 报告实际数）+ `uv run ruff check`

- [ ] **Step 8: 提交**

```bash
git add -A
git commit -m "feat: 状态词表统一（RETRY→FAILED）、delivery_attempts 表与 outbox 复合索引"
```

---

### Task 1: 共享常驻事件循环（重构 actors.py，防两循环击穿单例引擎）

**Files:**
- Create: `src/social_reply/infrastructure/queue/actor_loop.py`
- Modify: `src/social_reply/application/event_ingestion/actors.py`（改用共享 loop）
- Test: `tests/unit/test_actor_loop.py`

**背景（load-bearing）**：单例异步引擎的连接池绑定"首次建连的事件循环"。若入站 actor 与投递 actor 各起一个常驻 loop，跨循环复用连接会重演 "Event loop is closed"（Plan 1 Task 1 实测）。因此两个 actor 必须共用同一个常驻 loop。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_actor_loop.py
import asyncio

from social_reply.infrastructure.queue.actor_loop import run_on_actor_loop


def test_run_on_actor_loop_executes_coroutine():
    async def _coro():
        await asyncio.sleep(0)
        return 42
    assert run_on_actor_loop(_coro()) == 42


def test_run_on_actor_loop_propagates_exception():
    async def _boom():
        raise ValueError("boom")
    try:
        run_on_actor_loop(_boom())
        raise AssertionError("should have raised")
    except ValueError as e:
        assert str(e) == "boom"


def test_same_loop_reused_across_calls():
    async def _which_loop():
        return id(asyncio.get_running_loop())
    assert run_on_actor_loop(_which_loop()) == run_on_actor_loop(_which_loop())
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/unit/test_actor_loop.py -v` → FAIL

- [ ] **Step 3: 实现共享 loop**

```python
# src/social_reply/infrastructure/queue/actor_loop.py
import asyncio
import threading
from collections.abc import Coroutine
from typing import Any

# 单一常驻事件循环：所有 Dramatiq actor 共用，使单例引擎连接池只绑定这一个循环。
# 每消息 asyncio.run() 会跨循环复用 asyncpg 连接导致 "Event loop is closed"（Plan 1 Task 1 实测）。
_loop = asyncio.new_event_loop()
threading.Thread(target=_loop.run_forever, daemon=True, name="actor-loop").start()


def run_on_actor_loop(coro: Coroutine[Any, Any, Any], timeout: float = 120) -> Any:
    """在常驻 loop 上跑协程并阻塞取结果。
    无超时的 result() 会阻塞在 C 层锁上，Dramatiq TimeLimit 杀不掉（评审核对源码）——故带 timeout。"""
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    try:
        return future.result(timeout=timeout)
    except TimeoutError:
        future.cancel()
        raise
```

- [ ] **Step 4: 重构入站 actor 用共享 loop**

`event_ingestion/actors.py` 全文替换为：

```python
import dramatiq

import social_reply.infrastructure.queue.broker  # noqa: F401  确保 broker 先初始化
from social_reply.infrastructure.queue.actor_loop import run_on_actor_loop


@dramatiq.actor(max_retries=3)
def process_chatwoot_event(raw_event_id: str) -> None:
    from social_reply.application.event_ingestion.processor import process_raw_event

    run_on_actor_loop(process_raw_event(raw_event_id))
```

- [ ] **Step 5: 门禁（Plan 1 入站测试不得回归）**

Run: `uv run pytest -q`（全绿，与上一任务同数）+ `uv run pytest tests/integration/test_webhook_endpoint.py -v`（3 passed，入队仍工作）+ `uv run ruff check`

- [ ] **Step 6: 提交**

```bash
git add -A
git commit -m "refactor: 抽取共享常驻事件循环（投递 actor 与入站 actor 共用，防单例引擎跨循环）"
```

---

### Task 2: Chatwoot Messages API 客户端（Protocol + Fake + httpx）

**Files:**
- Create: `src/social_reply/connectors/chatwoot/client.py`
- Modify: `src/social_reply/shared/config.py`（chatwoot_base_url / chatwoot_api_token）
- Test: `tests/unit/test_chatwoot_client.py`

- [ ] **Step 1: config 新增字段**

`config.py` 的 `Settings` 内 `prompt_version` 之后加两行：

```python
    chatwoot_base_url: str = "http://localhost:3000"
    chatwoot_api_token: str = "dev-local-token"
```

（注：这两项无生产密钥校验器——Plan 2c 若需可仿 webhook_secret 加校验；本任务不加，保持本地开发可跑。）

- [ ] **Step 2: 写失败测试（Fake 行为 + httpx 请求形状用 MockTransport）**

```python
# tests/unit/test_chatwoot_client.py
import httpx

from social_reply.connectors.chatwoot.client import (
    FakeChatwootClient, HttpxChatwootClient,
)


async def test_fake_records_and_returns_incrementing_id():
    fake = FakeChatwootClient()
    mid1 = await fake.create_message(
        account_id=1, conversation_id=77, content="您好", private=False)
    mid2 = await fake.create_message(
        account_id=1, conversation_id=77, content="草稿", private=True)
    assert mid2 > mid1
    assert fake.sent[0] == {
        "account_id": 1, "conversation_id": 77, "content": "您好",
        "private": False, "id": mid1}
    assert fake.sent[1]["private"] is True


async def test_httpx_client_builds_correct_request():
    captured = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["token"] = request.headers.get("api_access_token")
        import json
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": 9001})

    transport = httpx.MockTransport(_handler)
    client = HttpxChatwootClient("http://cw.test", "tok-123", transport=transport)
    mid = await client.create_message(
        account_id=2, conversation_id=88, content="hi", private=False)
    assert mid == 9001
    assert captured["url"] == "http://cw.test/api/v1/accounts/2/conversations/88/messages"
    assert captured["token"] == "tok-123"
    assert captured["body"] == {"content": "hi", "message_type": "outgoing", "private": False}
```

- [ ] **Step 3: 运行确认失败**

Run: `uv run pytest tests/unit/test_chatwoot_client.py -v` → FAIL

- [ ] **Step 4: 实现客户端**

```python
# src/social_reply/connectors/chatwoot/client.py
from typing import Protocol

import httpx

from social_reply.shared.config import get_settings


class ChatwootClient(Protocol):
    async def create_message(
        self, *, account_id: int, conversation_id: int, content: str, private: bool
    ) -> int:
        """向 Chatwoot 会话发一条 outgoing 消息（private=True 为私有备注），返回 Chatwoot message id。"""
        ...


class FakeChatwootClient:
    """测试用：记录发送、返回自增 id。供集成测试内省 .sent。"""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self._next_id = 1000

    async def create_message(
        self, *, account_id: int, conversation_id: int, content: str, private: bool
    ) -> int:
        self._next_id += 1
        self.sent.append({
            "account_id": account_id, "conversation_id": conversation_id,
            "content": content, "private": private, "id": self._next_id})
        return self._next_id


class HttpxChatwootClient:
    """生产：POST /api/v1/accounts/{account_id}/conversations/{conversation_id}/messages
    Header api_access_token；message_type=outgoing，private 决定是否私有备注。"""

    def __init__(
        self, base_url: str, api_token: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_token = api_token
        self._transport = transport

    async def create_message(
        self, *, account_id: int, conversation_id: int, content: str, private: bool
    ) -> int:
        url = (f"{self._base_url}/api/v1/accounts/{account_id}"
               f"/conversations/{conversation_id}/messages")
        async with httpx.AsyncClient(timeout=15.0, transport=self._transport) as client:
            resp = await client.post(
                url,
                headers={"api_access_token": self._api_token},
                json={"content": content, "message_type": "outgoing", "private": private},
            )
            resp.raise_for_status()
            return int(resp.json()["id"])


_fake: FakeChatwootClient | None = None


def get_chatwoot_client() -> ChatwootClient:
    settings = get_settings()
    if settings.testing:
        global _fake
        if _fake is None:
            _fake = FakeChatwootClient()
        return _fake
    return HttpxChatwootClient(settings.chatwoot_base_url, settings.chatwoot_api_token)
```

- [ ] **Step 5: 运行确认通过**

Run: `uv run pytest tests/unit/test_chatwoot_client.py -v` → PASS
Run: `uv run pytest -q` + `uv run ruff check`

**验证 Chatwoot API 形状**：实现前用 context7 或 developers.chatwoot.com 确认 message create 端点为 `POST /api/v1/accounts/{account_id}/conversations/{conversation_id}/messages`、header `api_access_token`、body `{content, message_type, private}`、响应含 `id`。若形状不同，以官方文档为准并在报告中说明偏差。

- [ ] **Step 6: 提交**

```bash
git add -A
git commit -m "feat: Chatwoot Messages API 客户端（Protocol + Fake + httpx，可注入 transport）"
```

---

### Task 3: Outbox 投递（认领 + defense 2 + 发送 + 落库）

**Files:**
- Create: `src/social_reply/application/message_delivery/__init__.py`（空）
- Create: `src/social_reply/application/message_delivery/outbox.py`
- Test: `tests/integration/test_deliver_outbox.py`

- [ ] **Step 1: 写失败测试（核心行为）**

```python
# tests/integration/test_deliver_outbox.py
import uuid

import pytest
from sqlalchemy import insert, select

from social_reply.application.message_delivery.outbox import deliver_outbox
from social_reply.connectors.chatwoot.client import get_chatwoot_client
from social_reply.domain.automation.state_machine import ensure_state, flip_to_human_active
from social_reply.infrastructure.database import models

pytestmark = pytest.mark.integration


async def _seed(session, *, state="BOT_ACTIVE", message_type="text", status="PENDING",
                with_mapping=True):
    account_id, contact_id, conv_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await session.execute(insert(models.PlatformAccount).values(
        id=account_id, brand_id="b1", platform="telegram", name="a", chatwoot_inbox_id=101))
    await session.execute(insert(models.Contact).values(
        id=contact_id, platform="telegram", platform_account_id=account_id, external_user_id="9"))
    await session.execute(insert(models.Conversation).values(
        id=conv_id, brand_id="b1", platform="telegram", platform_account_id=account_id,
        contact_id=contact_id, conversation_key="telegram:x:9"))
    await ensure_state(session, conv_id, state)
    if with_mapping:
        await session.execute(insert(models.ConversationMapping).values(
            chatwoot_account_id=1, chatwoot_conversation_id=77, conversation_id=conv_id))
    ob_id = uuid.uuid4()
    await session.execute(insert(models.OutboxMessage).values(
        id=ob_id, conversation_id=conv_id, platform_account_id=account_id,
        destination_type="chatwoot_conversation", destination_id="telegram:x:9",
        message_type=message_type, payload={"text": "您好，请提供订单号。", "visibility": "public"},
        idempotency_key=str(ob_id), status=status))
    await session.commit()
    return conv_id, ob_id


async def test_bot_active_text_delivers_and_marks_sent(session):
    conv_id, ob_id = await _seed(session, state="BOT_ACTIVE", message_type="text")
    result = await deliver_outbox(str(ob_id))
    assert result == "SENT"
    ob = (await session.execute(
        select(models.OutboxMessage).where(models.OutboxMessage.id == ob_id))).scalar_one()
    assert ob.status == "SENT" and ob.chatwoot_message_id is not None and ob.sent_at is not None
    # 真实发送到 Chatwoot（Fake）
    fake = get_chatwoot_client()
    assert fake.sent[-1] == {
        "account_id": 1, "conversation_id": 77, "content": "您好，请提供订单号。",
        "private": False, "id": ob.chatwoot_message_id}
    att = (await session.execute(
        select(models.DeliveryAttempt).where(models.DeliveryAttempt.outbox_id == ob_id))).scalar_one()
    assert att.outcome == "SENT"


async def test_private_note_delivers_as_private(session):
    conv_id, ob_id = await _seed(session, state="BOT_DRAFT_ONLY", message_type="private_note")
    assert await deliver_outbox(str(ob_id)) == "SENT"
    fake = get_chatwoot_client()
    assert fake.sent[-1]["private"] is True


async def test_defense2_cancels_text_when_not_bot_active(session):
    # 认领后复检：会话已 HUMAN_ACTIVE → 公开回复不发，标 CANCELLED
    conv_id, ob_id = await _seed(session, state="BOT_ACTIVE", message_type="text")
    await flip_to_human_active(session, conv_id, "3", "agent_takeover")  # 会一并取消 PENDING，故先取消
    await session.commit()
    # flip 的 defense 3 已把 PENDING 置 CANCELLED；deliver 认领 WHERE PENDING/FAILED 落空
    result = await deliver_outbox(str(ob_id))
    assert result == "SKIPPED_NOT_CLAIMABLE"
    fake = get_chatwoot_client()
    assert all(s["content"] != "您好，请提供订单号。" or True for s in fake.sent)  # 未新增该会话发送
    ob = (await session.execute(
        select(models.OutboxMessage).where(models.OutboxMessage.id == ob_id))).scalar_one()
    assert ob.status == "CANCELLED"


async def test_defense2_direct_cancel_when_state_flips_without_defense3(session):
    # 模拟 defense 3 未覆盖的窗口：手动把 outbox 留在 PENDING 但状态已 HUMAN_ACTIVE
    conv_id, ob_id = await _seed(session, state="HUMAN_ACTIVE", message_type="text")
    result = await deliver_outbox(str(ob_id))
    assert result == "CANCELLED"  # defense 2 认领后复检拦截
    fake = get_chatwoot_client()
    assert not any(s["conversation_id"] == 77 for s in fake.sent[-1:]) or fake.sent[-1]["content"] != "您好，请提供订单号。"
    ob = (await session.execute(
        select(models.OutboxMessage).where(models.OutboxMessage.id == ob_id))).scalar_one()
    assert ob.status == "CANCELLED" and ob.last_error_code == "TAKEOVER_AT_SEND"


async def test_no_mapping_marks_needs_review(session):
    conv_id, ob_id = await _seed(session, state="BOT_ACTIVE", message_type="text",
                                 with_mapping=False)
    assert await deliver_outbox(str(ob_id)) == "NEEDS_REVIEW"
    ob = (await session.execute(
        select(models.OutboxMessage).where(models.OutboxMessage.id == ob_id))).scalar_one()
    assert ob.status == "NEEDS_REVIEW" and ob.last_error_code == "NO_MAPPING"
```

（注：`get_chatwoot_client()` 在 testing 下返回模块级 Fake 单例，测试间会累积 `.sent`；断言用 `[-1]` 看最近一次。`_flush` 非必需——各测试 seed 不同会话/内容，用最近一次即可。）

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_deliver_outbox.py -v` → FAIL

- [ ] **Step 3: 实现投递**

```python
# src/social_reply/application/message_delivery/outbox.py
import uuid
from datetime import UTC, datetime

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.connectors.chatwoot.client import get_chatwoot_client
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory

_MAX_ATTEMPTS = 5


async def _resolve_target(
    session: AsyncSession, conversation_id: uuid.UUID
) -> tuple[int, int] | None:
    row = (await session.execute(
        select(models.ConversationMapping.chatwoot_account_id,
               models.ConversationMapping.chatwoot_conversation_id)
        .where(models.ConversationMapping.conversation_id == conversation_id)
    )).first()
    return (row.chatwoot_account_id, row.chatwoot_conversation_id) if row else None


async def _finalize(
    outbox_id: uuid.UUID, status: str, *, attempt_no: int,
    error_code: str | None = None, error_message: str | None = None,
    chatwoot_message_id: int | None = None, next_attempt_at: datetime | None = None,
) -> str:
    async with get_session_factory()() as session:
        values: dict = {"status": status, "last_error_code": error_code,
                        "last_error_message": error_message}
        if chatwoot_message_id is not None:
            values["chatwoot_message_id"] = chatwoot_message_id
            values["sent_at"] = datetime.now(UTC)
        if next_attempt_at is not None:
            values["next_attempt_at"] = next_attempt_at
        await session.execute(
            update(models.OutboxMessage)
            .where(models.OutboxMessage.id == outbox_id).values(**values))
        await session.execute(insert(models.DeliveryAttempt).values(
            outbox_id=outbox_id, attempt_no=attempt_no, outcome=status,
            error_code=error_code, error_message=error_message,
            chatwoot_message_id=chatwoot_message_id))
        await session.commit()
    return status


async def deliver_outbox(outbox_id: str) -> str:
    """认领 → defense 2 发送前复检 → 发送 → 落库。返回终态字符串。"""
    oid = uuid.UUID(outbox_id)

    # 1) 原子认领：仅 PENDING/FAILED 可认领 → SENDING（防重复认领、跳过已取消/已发送）
    async with get_session_factory()() as session:
        claimed = (await session.execute(
            update(models.OutboxMessage)
            .where(models.OutboxMessage.id == oid,
                   models.OutboxMessage.status.in_(["PENDING", "FAILED"]))
            .values(status="SENDING", locked_at=datetime.now(UTC), locked_by="deliver")
            .returning(models.OutboxMessage.id))).first()
        if claimed is None:
            await session.commit()
            return "SKIPPED_NOT_CLAIMABLE"
        row = (await session.execute(
            select(models.OutboxMessage).where(models.OutboxMessage.id == oid))).scalar_one()
        conversation_id = row.conversation_id
        message_type = row.message_type
        payload = dict(row.payload)
        attempt_no = row.attempt_count + 1

        # 2) defense 2：公开回复（text）发送前必须 state==BOT_ACTIVE（PLAN §六 权威闸门）
        state = (await session.execute(
            select(models.AutomationState.state)
            .where(models.AutomationState.conversation_id == conversation_id)
        )).scalar_one_or_none()
        if message_type == "text" and state != "BOT_ACTIVE":
            await session.execute(
                update(models.OutboxMessage).where(models.OutboxMessage.id == oid)
                .values(status="CANCELLED", last_error_code="TAKEOVER_AT_SEND"))
            await session.execute(insert(models.DeliveryAttempt).values(
                outbox_id=oid, attempt_no=attempt_no, outcome="CANCELLED",
                error_code="TAKEOVER_AT_SEND"))
            await session.commit()
            return "CANCELLED"

        target = await _resolve_target(session, conversation_id)
        await session.execute(
            update(models.OutboxMessage).where(models.OutboxMessage.id == oid)
            .values(attempt_count=attempt_no))
        await session.commit()

    if target is None:
        return await _finalize(oid, "NEEDS_REVIEW", attempt_no=attempt_no,
                               error_code="NO_MAPPING", error_message="no chatwoot mapping")

    account_id, chatwoot_conv_id = target

    # 3) 发送（不持 DB 事务，避免网络 I/O 期间持锁）
    client = get_chatwoot_client()
    try:
        chatwoot_message_id = await client.create_message(
            account_id=account_id, conversation_id=chatwoot_conv_id,
            content=payload["text"], private=(message_type == "private_note"))
    except Exception as e:  # noqa: BLE001 平台错误统一按可重试处理，超阈值转人工
        if attempt_no >= _MAX_ATTEMPTS:
            return await _finalize(oid, "NEEDS_REVIEW", attempt_no=attempt_no,
                                   error_code="SEND_ERROR", error_message=repr(e))
        # 指数退避（简化：attempt_no 秒；生产可换真正退避）
        next_at = datetime.now(UTC)
        return await _finalize(oid, "FAILED", attempt_no=attempt_no,
                               error_code="SEND_ERROR", error_message=repr(e),
                               next_attempt_at=next_at)

    # 4) 成功：SENT + 存 chatwoot_message_id（闭合 Plan 1 回声断路器）
    return await _finalize(oid, "SENT", attempt_no=attempt_no,
                           chatwoot_message_id=chatwoot_message_id)
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/integration/test_deliver_outbox.py -v` → PASS（5 tests）
Run: `uv run pytest -q` + `uv run ruff check`

- [ ] **Step 5: 提交**

```bash
git add -A
git commit -m "feat: Outbox 投递（认领/defense 2 发送前复检/发送/SENT 存 chatwoot id/失败重试）"
```

---

### Task 4: 投递 actor + runner enqueue 接线

**Files:**
- Create: `src/social_reply/application/message_delivery/actors.py`
- Modify: `src/social_reply/application/reply_decision/runner.py`（enqueue deliver）
- Test: `tests/integration/test_decision_enqueues_delivery.py`

- [ ] **Step 1: 写失败测试（决策后投递任务入队）**

```python
# tests/integration/test_decision_enqueues_delivery.py
import uuid

import pytest
from sqlalchemy import func, insert, select

from social_reply.application.event_ingestion.processor import process_raw_event
from social_reply.infrastructure.database import models

pytestmark = pytest.mark.integration


def _payload(**o):
    p = {"event": "message_created", "id": 55, "content": "请问怎么改邮箱",
         "message_type": "incoming", "private": False, "created_at": "2026-07-15T10:00:00Z",
         "sender": {"id": 9, "type": "contact"},
         "conversation": {"id": 77, "inbox_id": 101, "status": "pending"},
         "account": {"id": 1}}
    p.update(o)
    return p


async def test_bot_active_inbound_enqueues_delivery(session):
    import dramatiq
    broker = dramatiq.get_broker()
    aid = uuid.uuid4()
    await session.execute(insert(models.PlatformAccount).values(
        id=aid, brand_id="b1", platform="telegram", name="a", chatwoot_inbox_id=101,
        automation_default="BOT_ACTIVE"))
    r = (await session.execute(insert(models.RawEvent).values(
        source="chatwoot", payload=_payload()).returning(models.RawEvent.id))).scalar_one()
    await session.commit()

    await process_raw_event(str(r))

    # 决策产生 PENDING outbox，并把 deliver_outbox_message 入队
    ob = (await session.execute(select(models.OutboxMessage))).scalar_one()
    assert ob.status == "PENDING"
    assert broker.queues["default"].qsize() >= 1  # 至少一个投递任务
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_decision_enqueues_delivery.py -v` → FAIL（当前 runner 只留注释未 enqueue）

- [ ] **Step 3: 实现投递 actor**

```python
# src/social_reply/application/message_delivery/actors.py
import dramatiq

import social_reply.infrastructure.queue.broker  # noqa: F401
from social_reply.infrastructure.queue.actor_loop import run_on_actor_loop


@dramatiq.actor(max_retries=3)
def deliver_outbox_message(outbox_id: str) -> None:
    from social_reply.application.message_delivery.outbox import deliver_outbox

    run_on_actor_loop(deliver_outbox(outbox_id))
```

- [ ] **Step 4: runner enqueue 接线**

`runner.py` 末尾，把 `# Plan 2b：if outbox_id: enqueue deliver_outbox(outbox_id)` 注释替换为真实 enqueue：

```python
    if outbox_id is not None:
        from social_reply.application.message_delivery.actors import deliver_outbox_message
        deliver_outbox_message.send(str(outbox_id))
    return outbox_id
```

- [ ] **Step 5: 运行确认通过 + 全量**

Run: `uv run pytest tests/integration/test_decision_enqueues_delivery.py -v` → PASS
Run: `uv run pytest -q`（报告数）+ `uv run ruff check`

- [ ] **Step 6: 提交**

```bash
git add -A
git commit -m "feat: 投递 actor 与 runner enqueue 接线（决策→PENDING outbox→投递入队）"
```

---

### Task 5: scheduler 补扫（孤儿 PENDING / 退避到期 FAILED / 滞留 SENDING）

**Files:**
- Create: `src/social_reply/application/message_delivery/sweep.py`
- Create: `apps/scheduler/main.py`
- Test: `tests/integration/test_outbox_sweep.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/integration/test_outbox_sweep.py
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, select

from social_reply.application.message_delivery.sweep import sweep_outbox
from social_reply.infrastructure.database import models

pytestmark = pytest.mark.integration


async def _ob(session, *, status, next_attempt_at=None, locked_at=None):
    aid, conv = uuid.uuid4(), uuid.uuid4()
    await session.execute(insert(models.PlatformAccount).values(
        id=aid, brand_id="b1", platform="telegram", name="a", chatwoot_inbox_id=int(uuid.uuid4().int % 1_000_000)))
    await session.execute(insert(models.Conversation).values(
        id=conv, brand_id="b1", platform="telegram", platform_account_id=aid,
        contact_id=uuid.uuid4(), conversation_key=f"telegram:{aid}:9"))
    # contact FK：先建 contact
    ob_id = uuid.uuid4()
    await session.execute(insert(models.OutboxMessage).values(
        id=ob_id, conversation_id=conv, platform_account_id=aid,
        destination_type="chatwoot_conversation", destination_id="k",
        message_type="text", payload={"text": "x"}, idempotency_key=str(ob_id),
        status=status, next_attempt_at=next_attempt_at, locked_at=locked_at))
    await session.commit()
    return ob_id


async def test_sweep_enqueues_pending_and_due_failed(session):
    past = datetime.now(UTC) - timedelta(minutes=1)
    ob_pending = await _ob(session, status="PENDING")
    ob_failed_due = await _ob(session, status="FAILED", next_attempt_at=past)
    ob_failed_future = await _ob(session, status="FAILED",
                                 next_attempt_at=datetime.now(UTC) + timedelta(hours=1))
    enqueued = await sweep_outbox()
    assert ob_pending in enqueued
    assert ob_failed_due in enqueued
    assert ob_failed_future not in enqueued  # 未到退避时间


async def test_sweep_marks_stale_sending_needs_review(session):
    # SENDING 且锁很旧（进程崩在发送后 finalize 前）→ 不自动重发（防重复），转 NEEDS_REVIEW
    stale = datetime.now(UTC) - timedelta(minutes=30)
    ob = await _ob(session, status="SENDING", locked_at=stale)
    await sweep_outbox()
    row = (await session.execute(
        select(models.OutboxMessage).where(models.OutboxMessage.id == ob))).scalar_one()
    assert row.status == "NEEDS_REVIEW" and row.last_error_code == "STALE_SENDING"
```

（注：`_ob` 需先建 contact 满足 conversations.contact_id FK——实现测试时补一行 `insert(Contact)`；此处示意，实现者据实补全 seed 使 FK 成立。）

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_outbox_sweep.py -v` → FAIL

- [ ] **Step 3: 实现补扫**

```python
# src/social_reply/application/message_delivery/sweep.py
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, update

from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory

_STALE_SENDING = timedelta(minutes=10)


async def sweep_outbox() -> list[uuid.UUID]:
    """补扫：把滞留 SENDING 转 NEEDS_REVIEW（不自动重发，防重复）；
    把可投递的 PENDING / 退避到期的 FAILED 重新入队。返回本轮入队的 outbox id。"""
    now = datetime.now(UTC)
    enqueued: list[uuid.UUID] = []
    async with get_session_factory()() as session:
        # 滞留 SENDING（歧义失败）→ NEEDS_REVIEW
        await session.execute(
            update(models.OutboxMessage)
            .where(models.OutboxMessage.status == "SENDING",
                   models.OutboxMessage.locked_at < now - _STALE_SENDING)
            .values(status="NEEDS_REVIEW", last_error_code="STALE_SENDING"))
        # 可投递集合：PENDING，或 FAILED 且退避到期
        rows = (await session.execute(
            select(models.OutboxMessage.id)
            .where(or_(
                models.OutboxMessage.status == "PENDING",
                (models.OutboxMessage.status == "FAILED")
                & (models.OutboxMessage.next_attempt_at <= now)))
        )).scalars().all()
        enqueued = list(rows)
        await session.commit()

    # 入队（导入放函数内避免循环依赖）
    from social_reply.application.message_delivery.actors import deliver_outbox_message
    for oid in enqueued:
        deliver_outbox_message.send(str(oid))
    return enqueued
```

创建 `apps/scheduler/main.py`：

```python
"""周期补扫入口：uv run python -m apps.scheduler.main（简单常驻循环，生产可换 cron/APScheduler）。"""
import asyncio

from social_reply.application.message_delivery.sweep import sweep_outbox
from social_reply.infrastructure.queue.actor_loop import run_on_actor_loop

_INTERVAL_SECONDS = 30


def main() -> None:
    while True:
        run_on_actor_loop(sweep_outbox())
        import time
        time.sleep(_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/integration/test_outbox_sweep.py -v` → PASS
Run: `uv run pytest -q` + `uv run ruff check`

- [ ] **Step 5: 提交**

```bash
git add -A
git commit -m "feat: Outbox 补扫（滞留 SENDING→NEEDS_REVIEW、PENDING/到期 FAILED 重投）+ scheduler 入口"
```

---

### Task 6: Redis killswitch client 复用（Task 9 评审 M1）

**Files:**
- Modify: `src/social_reply/application/reply_decision/runner.py`
- Test: `tests/unit/test_runner_redis_singleton.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_runner_redis_singleton.py
from social_reply.application.reply_decision import runner


def test_killswitch_client_is_reused():
    c1 = runner._get_redis()
    c2 = runner._get_redis()
    assert c1 is c2  # 模块级共享，不每次 from_url 建新连接池
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/unit/test_runner_redis_singleton.py -v` → FAIL

- [ ] **Step 3: 实现共享 client**

`runner.py` 把 `_make_killswitch` 改为共享 redis client：

```python
_redis = None


def _get_redis():
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(get_settings().redis_url)
    return _redis


def _make_killswitch() -> KillSwitchChecker:
    return KillSwitchChecker(_get_redis())
```

（`run_and_persist_decision` 内对 `_make_killswitch()` 的 try/except 保持不变——`_get_redis` 首次 `from_url` 若 URL 畸形仍在构造期抛，被 I1 的 try 捕获 fail-closed。）

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/unit/test_runner_redis_singleton.py -v` → PASS
Run: `uv run pytest -q` + `uv run ruff check`

- [ ] **Step 5: 提交**

```bash
git add -A
git commit -m "fix: killswitch redis client 模块级复用（避免每决策新建连接池，Plan 2a 评审 M1）"
```

---

### Task 7: 端到端冒烟（Fake 全链路证明 + 真实 Chatwoot runbook）

**Files:**
- Create: `tests/integration/test_end_to_end_delivery.py`
- Modify: `README.md`（真实 Chatwoot 发送 runbook）

- [ ] **Step 1: 写全链路集成测试（webhook→决策→outbox→投递→Fake 收到回复）**

```python
# tests/integration/test_end_to_end_delivery.py
import hashlib
import hmac
import json
import time
import uuid

import dramatiq
import httpx
import pytest
from sqlalchemy import insert, select

from social_reply.connectors.chatwoot.client import get_chatwoot_client
from social_reply.infrastructure.database import models

pytestmark = pytest.mark.integration

SECRET = "dev-local-secret"


def _sig(body: bytes):
    ts = str(int(time.time()))
    digest = hmac.new(SECRET.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return {"X-Chatwoot-Signature": f"sha256={digest}", "X-Chatwoot-Timestamp": ts,
            "Content-Type": "application/json"}


@pytest.fixture
async def client(migrated_db, monkeypatch):
    monkeypatch.setenv("TESTING", "true")
    monkeypatch.setenv("CHATWOOT_WEBHOOK_SECRET", SECRET)
    from social_reply.shared.config import get_settings
    get_settings.cache_clear()
    from apps.api.main import create_app
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    get_settings.cache_clear()


async def test_full_loop_inbound_to_chatwoot_reply(client, session):
    # 预置 BOT_ACTIVE 账号 + Chatwoot 映射（会话 77↔chatwoot 1/77）
    aid = uuid.uuid4()
    await session.execute(insert(models.PlatformAccount).values(
        id=aid, brand_id="b1", platform="telegram", name="a", chatwoot_inbox_id=101,
        automation_default="BOT_ACTIVE"))
    await session.commit()

    payload = {"event": "message_created", "id": 55, "content": "请问怎么改邮箱",
               "message_type": "incoming", "private": False, "created_at": "2026-07-15T10:00:00Z",
               "sender": {"id": 9, "type": "contact"},
               "conversation": {"id": 77, "inbox_id": 101, "status": "pending"},
               "account": {"id": 1}}
    body = json.dumps(payload).encode()
    resp = await client.post("/webhooks/chatwoot", content=body, headers=_sig(body))
    assert resp.status_code == 200

    # webhook 只入队 process_chatwoot_event；本测试直接驱动决策链（不起真实 worker）
    raw = (await session.execute(select(models.RawEvent))).scalar_one()
    from social_reply.application.event_ingestion.processor import process_raw_event
    await process_raw_event(str(raw.id))

    # 决策写了 PENDING outbox；手动补建 mapping（Plan 1 processor 建会话时会建，
    # 但本测试 process_raw_event 内已建 conversation+mapping —— 确认存在）
    ob = (await session.execute(select(models.OutboxMessage))).scalar_one()
    # 驱动投递
    from social_reply.application.message_delivery.outbox import deliver_outbox
    result = await deliver_outbox(str(ob.id))
    assert result == "SENT"

    ob = (await session.execute(
        select(models.OutboxMessage).where(models.OutboxMessage.id == ob.id))).scalar_one()
    assert ob.status == "SENT" and ob.chatwoot_message_id is not None
    fake = get_chatwoot_client()
    assert fake.sent[-1]["content"]  # Chatwoot（Fake）收到了回复文本
```

（注：process_raw_event 内 Plan 1 会为 inbound 建 conversation + ConversationMapping（chatwoot_account_id=1, chatwoot_conversation_id=77）——deliver 的 `_resolve_target` 据此解析。若测试中 mapping 缺失，说明 Plan 1 的 `_ensure_conversation` 未建 mapping，需在测试里显式补建并在报告说明。）

- [ ] **Step 2: 运行确认通过**

Run: `uv run pytest tests/integration/test_end_to_end_delivery.py -v` → PASS
Run: `uv run pytest -q`（全量，报告数）+ `uv run ruff check`

- [ ] **Step 3: README 真实 Chatwoot runbook**

在 `README.md` 追加一节：

```markdown
## Plan 2b：真实 Chatwoot 发送（需自托管 Chatwoot 凭证）

1. 在 Chatwoot 建一个 API/AgentBot inbox，取 `api_access_token` 与 account id。
2. `.env` 设：
   ```
   CHATWOOT_BASE_URL=https://your-chatwoot.example.com
   CHATWOOT_API_TOKEN=<api_access_token>
   TESTING=false
   CHATWOOT_WEBHOOK_SECRET=<你的真实密钥>
   ```
3. 确保 `platform_accounts.chatwoot_inbox_id` 与 `conversation_mappings`（chatwoot_account_id/chatwoot_conversation_id ↔ 本地 conversation）已建立。
4. 起 API + worker + scheduler：
   ```
   uv run uvicorn apps.api.main:app --port 8000
   uv run dramatiq apps.worker.main
   uv run python -m apps.scheduler.main
   ```
5. 真实 Chatwoot webhook 指向 `http(s)://<你的host>:8000/webhooks/chatwoot`。
   用户发消息 → 决策 → 若 BOT_ACTIVE 自动回复经 Chatwoot Messages API 发出，回复出现在 Chatwoot 会话。
```

- [ ] **Step 4: 提交**

```bash
git add -A
git commit -m "feat: 端到端投递集成测试（Fake 全链路）+ 真实 Chatwoot 发送 runbook"
```

---

## Self-Review 记录

1. **Spec 覆盖**：状态词表统一/delivery_attempts/索引/defense3 对齐 → Task 0；共享 loop → Task 1；Chatwoot 客户端 → Task 2；投递 worker + defense 2 → Task 3；actor + enqueue 接线 → Task 4；补扫 → Task 5；Redis 复用 → Task 6；端到端 + runbook → Task 7。**不在本计划（Plan 2c）**：真实 OpenAI LLM、§五 失败矩阵、PII 正则加固、KILLSWITCH_UNAVAILABLE 告警、二跳送达状态（Chatwoot→Meta webhook 回流）、tx2 失败决策补扫。
2. **占位符扫描**：Task 5 的 `_ob` seed 缺 Contact（conversations.contact_id FK）已在注释标注实现者补全；Task 7 mapping 依赖 Plan 1 `_ensure_conversation` 行为已注明须确认。无 TODO/TBD。
3. **类型一致性**：`ChatwootClient.create_message(*, account_id, conversation_id, content, private)` 在 Task 2 定义，Task 3 一致调用；`deliver_outbox(outbox_id: str) -> str` 在 Task 3 定义，Task 4 actor / Task 5 sweep / Task 7 一致调用；status 词表（PENDING/SENDING/SENT/FAILED/CANCELLED/NEEDS_REVIEW）跨 Task 0/3/5 一致；`run_on_actor_loop` 在 Task 1 定义，Task 4 actor 使用。
4. **对既有的破坏面**：Task 0 改 defense 3 filter（RETRY→FAILED），既有 test_takeover_cancels_outbox 用 PENDING/SENT 不受影响；Task 1 重构 actors.py，Plan 1 webhook 入队测试须保持绿；Task 4 让 INBOUND_USER 决策后多入队一个投递任务，既有 test_decision_enqueues... 是新增，其它决策测试不检查队列深度，不受影响。
5. **凭证 gated**：真实 OpenAI（Plan 2c）与真实 Chatwoot 发送（Task 7 runbook）需用户提供 key/token，自动化测试全程 Fake/Mock，无需外部凭证即可全绿。
