# Phase 1 / Plan 2a — 自动回复决策与 Outbox 写入 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Plan 1 入站链路之上接入自动回复决策管线（规则 → Stub LLM 结构化决策 → Final Guard → 三级 kill switch），把决策结果持久化为 `reply_decisions` 并把可发送的回复写入事务性 `outbox_messages`（`PENDING`），带人工接管竞态三重防线的第 1、3 层。**本计划不实际发送**——投递 worker、Chatwoot Messages API、defense 2 属 Plan 2b。

**Architecture:** 入站事务（tx1，Plan 1 原样：存消息 + `PROCESSED`）提交后，对 `INBOUND_USER` 消息在 tx1 之外运行纯函数决策管线（不持有事务、不阻塞入站），再在独立事务 tx2 写 `reply_decisions` + `outbox_messages`，outbox 写入由 `state_version` CAS 守护（defense 1）。`flip_to_human_active` 扩展为取消该会话所有 `PENDING`/`RETRY` outbox 行（defense 3）。LLM 与未来的 Chatwoot 发送都以 Protocol + 确定性 Stub 注入，先 Stub 后接真。

**Tech Stack:** 延续 Plan 1（Python 3.13 + uv、FastAPI、SQLAlchemy 2 async + asyncpg、Alembic、Dramatiq[redis]、PostgreSQL 17、Redis 8、pytest）。新增 `redis.asyncio`（`dramatiq[redis]` 已传递依赖 `redis`，无需加依赖）。

**约定（延续 Plan 1）：**
- 命令在仓库根 `/Users/junqing/data/github/dm` 执行。
- 集成测试标记 `integration`，需 `docker compose -f deploy/docker-compose.yml up -d`。
- 中文注释，仅解释代码无法自述的约束。ruff 基线保持全绿。
- tenant_id 常量 `"default"`；新会话默认态 `BOT_DRAFT_ONLY`。
- 不 push，不建分支，每任务一次 commit（用户已授权）。
- Plan 1 起点：HEAD = `582d7be`，10 张表，43 tests。

**Plan 1 backlog 在本计划兑现：** messages.conversation_id 索引（Task 0）、CLOSED→HUMAN_ACTIVE 对账（Task 8）、Settings 拒绝 change-me 密钥（Task 0）。

---

### Task 0: reply_decisions 迁移、messages 索引与 Settings 加固

**Files:**
- Modify: `src/social_reply/infrastructure/database/models.py`（新增 `ReplyDecision` 模型 + messages.conversation_id 索引）
- Modify: `src/social_reply/shared/config.py`（新增 llm/kill-switch 配置 + 密钥校验）
- Create: `migrations/versions/0002_reply_decisions.py`（autogenerate）
- Test: `tests/integration/test_schema.py`（扩展表集合断言）

- [ ] **Step 1: 扩展 schema 测试（失败先行）**

在 `tests/integration/test_schema.py` 的 `EXPECTED_TABLES` 集合中加入 `"reply_decisions"`，并追加一个新测试：

```python
async def test_reply_decisions_columns_and_message_index(migrated_db):
    engine = get_engine()
    async with engine.connect() as conn:
        cols = {r[0] for r in await conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='reply_decisions'"
        ))}
        idx = {r[0] for r in await conn.execute(text(
            "SELECT indexname FROM pg_indexes WHERE tablename='messages'"
        ))}
    assert {"id", "conversation_id", "message_id", "action", "risk_level",
            "confidence", "reply_text", "reply_visibility", "reason_codes",
            "source", "prompt_version", "state_version_at_decision",
            "created_at"} <= cols
    assert any("conversation_id" in name for name in idx)
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_schema.py::test_reply_decisions_columns_and_message_index -v`
Expected: FAIL（reply_decisions 不存在）

- [ ] **Step 3: 新增 ReplyDecision 模型与 messages 索引**

在 `models.py` 末尾（`AuditLog` 之后）追加：

```python
class ReplyDecision(Base):
    __tablename__ = "reply_decisions"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[str] = mapped_column(Text, default="default")
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id"))
    message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("messages.id"))
    action: Mapped[str] = mapped_column(Text)  # auto_reply / draft / handoff / ignore
    intent: Mapped[str | None] = mapped_column(Text)
    risk_level: Mapped[str] = mapped_column(Text, default="low")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    reply_text: Mapped[str | None] = mapped_column(Text)
    reply_visibility: Mapped[str] = mapped_column(Text, default="public")
    reason_codes: Mapped[list] = mapped_column(JSONB, default=list)
    source: Mapped[str] = mapped_column(Text)  # rule / llm / guard
    prompt_version: Mapped[str | None] = mapped_column(Text)
    state_version_at_decision: Mapped[int | None] = mapped_column(Integer)
    outbox_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("outbox_messages.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
```

在 `Message` 类的 `conversation_id` 字段上加索引（改这一行）：

```python
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id"), index=True
    )
```

在 `models.py` 顶部 import 补 `Float`：把 `from sqlalchemy import (BigInteger, Boolean, DateTime, ForeignKey, Integer, Text, UniqueConstraint, func,)` 改为包含 `Float`：

```python
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
```

- [ ] **Step 4: Settings 新增配置与密钥校验**

`config.py` 全文替换为：

```python
from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://dev:dev@localhost:5432/social_reply"
    redis_url: str = "redis://localhost:6379/0"
    chatwoot_webhook_secret: str = "change-me"
    chatwoot_signature_tolerance_seconds: int = 300
    tenant_id: str = "default"
    default_automation_state: str = "BOT_DRAFT_ONLY"
    llm_provider: str = "stub"  # stub / anthropic / openai（Plan 2b/后续接真）
    prompt_version: str = "v0-stub"
    testing: bool = False

    @model_validator(mode="after")
    def _reject_default_secret_in_prod(self) -> "Settings":
        # 生产环境（非测试）拒绝空/默认 webhook 密钥（Plan 1 安全评审 backlog）
        if not self.testing and self.chatwoot_webhook_secret in ("", "change-me"):
            raise ValueError(
                "CHATWOOT_WEBHOOK_SECRET 未配置（不能为空或 change-me）；测试环境请设 TESTING=true"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

- [ ] **Step 5: 生成迁移并验证**

```bash
docker compose -f deploy/docker-compose.yml exec -T postgres \
  psql -U dev -d social_reply -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
uv run alembic revision --autogenerate -m "reply_decisions and message index"
uv run alembic upgrade head
docker compose -f deploy/docker-compose.yml exec -T postgres \
  psql -U dev -d social_reply -c "\dt" | grep reply_decisions
```

检查生成的 `migrations/versions/*_reply_decisions*.py` 含 `create_table('reply_decisions')` 与 messages 的 `create_index`。若 ruff 对生成文件报错，已有 per-file-ignore 覆盖 `migrations/versions/*`。

- [ ] **Step 6: 运行全部门禁**

Run: `uv run pytest -q`
Expected: 45 passed（43 + schema 扩展 1 + 原 schema 测试仍过；实际数以运行为准，报告准确值）
Run: `uv run ruff check`
Expected: All checks passed

注意：`test_reject_default_secret` 不在本任务——config 校验靠 `testing=true`（集成测试 conftest 已设 TESTING）放行；生产校验留待手动/Plan 2b。若既有测试因 Settings 实例化在非 testing 上下文失败，确认 `tests/conftest.py` 的 `os.environ.setdefault("TESTING","true")` 生效。

- [ ] **Step 7: 提交**

```bash
git add -A
git commit -m "feat: reply_decisions 表、messages 索引与 Settings 密钥校验"
```

---

### Task 1: 决策领域类型

**Files:**
- Create: `src/social_reply/domain/reply/__init__.py`
- Create: `src/social_reply/domain/reply/decision.py`
- Test: `tests/unit/test_reply_decision.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_reply_decision.py
from dataclasses import replace

from social_reply.domain.reply.decision import (
    ReplyAction, ReplyDecision, RiskLevel, Visibility,
)


def test_defaults_and_replace():
    d = ReplyDecision(action=ReplyAction.AUTO_REPLY, reply_text="hi")
    assert d.risk_level is RiskLevel.LOW
    assert d.reply_visibility is Visibility.PUBLIC
    assert d.reason_codes == ()
    d2 = replace(d, action=ReplyAction.DRAFT)
    assert d2.action is ReplyAction.DRAFT and d2.reply_text == "hi"


def test_str_enums_are_strings():
    assert ReplyAction.HANDOFF == "handoff"
    assert RiskLevel.HIGH == "high"
    assert Visibility.PRIVATE == "private"
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/unit/test_reply_decision.py -v`
Expected: FAIL

- [ ] **Step 3: 实现**

```python
# src/social_reply/domain/reply/decision.py
from dataclasses import dataclass, field
from enum import StrEnum


class ReplyAction(StrEnum):
    AUTO_REPLY = "auto_reply"
    DRAFT = "draft"        # 只写 Chatwoot 私有备注，不对外发
    HANDOFF = "handoff"    # 转人工
    IGNORE = "ignore"      # 不回复也不接管


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Visibility(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"


@dataclass(frozen=True)
class ReplyDecision:
    action: ReplyAction
    reply_text: str | None = None
    intent: str | None = None
    risk_level: RiskLevel = RiskLevel.LOW
    confidence: float = 0.0
    reply_visibility: Visibility = Visibility.PUBLIC
    handoff_team: str | None = None
    reason_codes: tuple[str, ...] = ()
    source: str = "llm"  # rule / llm / guard
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/unit/test_reply_decision.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add -A
git commit -m "feat: 回复决策领域类型（action/risk/visibility）"
```

---

### Task 2: 规则引擎（LLM 前确定性短路）

**Files:**
- Create: `src/social_reply/domain/reply/rules.py`
- Test: `tests/unit/test_reply_rules.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_reply_rules.py
from social_reply.domain.reply.decision import ReplyAction, RiskLevel
from social_reply.domain.reply.rules import apply_rules


def test_greeting_returns_template_auto_reply():
    d = apply_rules("你好")
    assert d is not None
    assert d.action is ReplyAction.AUTO_REPLY
    assert d.source == "rule"
    assert "GREETING_TEMPLATE" in d.reason_codes


def test_risk_word_forces_handoff():
    d = apply_rules("你们是不是诈骗，我无法出金")
    assert d is not None
    assert d.action is ReplyAction.HANDOFF
    assert d.risk_level is RiskLevel.HIGH
    assert "RISK_WORD" in d.reason_codes


def test_empty_text_handoff():
    d = apply_rules("   ")
    assert d is not None and d.action is ReplyAction.HANDOFF
    assert "EMPTY_OR_NON_TEXT" in d.reason_codes
    assert apply_rules(None).action is ReplyAction.HANDOFF


def test_normal_question_falls_through_to_llm():
    assert apply_rules("请问怎么修改绑定邮箱？") is None
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/unit/test_reply_rules.py -v`
Expected: FAIL

- [ ] **Step 3: 实现**

```python
# src/social_reply/domain/reply/rules.py
from social_reply.domain.reply.decision import ReplyAction, ReplyDecision, RiskLevel

# PLAN.md §五：高风险词默认转人工
RISK_WORDS = ("诈骗", "无法出金", "无法提现", "律师", "起诉", "退款", "账户冻结", "冻结")
GREETINGS = frozenset({"hi", "hello", "hey", "你好", "您好", "thanks", "thank you", "谢谢"})
GREETING_REPLY = "您好，请问有什么可以帮您？"


def apply_rules(text: str | None) -> ReplyDecision | None:
    """确定性前置规则；命中即短路返回决策，否则返回 None 交给 LLM。"""
    if text is None or not text.strip():
        return ReplyDecision(
            action=ReplyAction.HANDOFF,
            reason_codes=("EMPTY_OR_NON_TEXT",),
            source="rule",
        )
    if text.strip().lower() in GREETINGS:
        return ReplyDecision(
            action=ReplyAction.AUTO_REPLY,
            reply_text=GREETING_REPLY,
            intent="greeting",
            reason_codes=("GREETING_TEMPLATE",),
            source="rule",
        )
    if any(w in text for w in RISK_WORDS):
        return ReplyDecision(
            action=ReplyAction.HANDOFF,
            risk_level=RiskLevel.HIGH,
            reason_codes=("RISK_WORD",),
            source="rule",
        )
    return None
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/unit/test_reply_rules.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add -A
git commit -m "feat: 规则引擎（问候模板/风险词转人工/空文本兜底）"
```

---

### Task 3: LLM 客户端协议与确定性 Stub

**Files:**
- Create: `src/social_reply/domain/reply/llm.py`
- Test: `tests/unit/test_llm_stub.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_llm_stub.py
import pytest

from social_reply.domain.reply.decision import ReplyAction
from social_reply.domain.reply.llm import LLMContext, StubLLMClient


async def test_stub_returns_deterministic_auto_reply():
    client = StubLLMClient()
    d = await client.decide(LLMContext(text="怎么改邮箱", conversation_key="telegram:acc:9"))
    assert d.action is ReplyAction.AUTO_REPLY
    assert d.reply_text
    assert d.source == "llm"
    assert "STUB_LLM" in d.reason_codes


async def test_stub_is_pure_same_input_same_output():
    client = StubLLMClient()
    ctx = LLMContext(text="x", conversation_key="k")
    assert await client.decide(ctx) == await client.decide(ctx)
```

（注：`asyncio_mode=auto` 已在 pyproject，async 测试无需装饰器。）

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/unit/test_llm_stub.py -v`
Expected: FAIL

- [ ] **Step 3: 实现**

```python
# src/social_reply/domain/reply/llm.py
from dataclasses import dataclass
from typing import Protocol

from social_reply.domain.reply.decision import (
    ReplyAction, ReplyDecision, RiskLevel, Visibility,
)


@dataclass(frozen=True)
class LLMContext:
    text: str
    conversation_key: str


class LLMClient(Protocol):
    async def decide(self, context: LLMContext) -> ReplyDecision: ...


class StubLLMClient:
    """确定性桩：真实供应商接入前用于跑通管线（先 Stub 后接真）。
    不做任何网络调用，输出与输入无关的固定 auto_reply，便于端到端验证。"""

    async def decide(self, context: LLMContext) -> ReplyDecision:
        return ReplyDecision(
            action=ReplyAction.AUTO_REPLY,
            reply_text="您好，已收到您的问题，我们会尽快为您解答。",
            intent="general_question",
            risk_level=RiskLevel.LOW,
            confidence=0.6,
            reply_visibility=Visibility.PUBLIC,
            reason_codes=("STUB_LLM",),
            source="llm",
        )
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/unit/test_llm_stub.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add -A
git commit -m "feat: LLM 客户端协议与确定性 Stub（先 Stub 后接真）"
```

---

### Task 4: Final Guard（输出侧确定性闸门）

**Files:**
- Create: `src/social_reply/domain/reply/guard.py`
- Test: `tests/unit/test_final_guard.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_final_guard.py
from social_reply.domain.reply.decision import (
    ReplyAction, ReplyDecision, Visibility,
)
from social_reply.domain.reply.guard import run_final_guard


def test_non_auto_reply_passes_through_untouched():
    d = ReplyDecision(action=ReplyAction.HANDOFF, reason_codes=("RISK_WORD",))
    assert run_final_guard(d, "telegram") is d


def test_public_reply_with_pii_downgraded_to_handoff():
    d = ReplyDecision(action=ReplyAction.AUTO_REPLY,
                      reply_text="您的账户 88123456 已处理",
                      reply_visibility=Visibility.PUBLIC)
    out = run_final_guard(d, "telegram")
    assert out.action is ReplyAction.HANDOFF
    assert "GUARD_PII_LEAK" in out.reason_codes


def test_email_in_public_reply_blocked():
    d = ReplyDecision(action=ReplyAction.AUTO_REPLY,
                      reply_text="请联系 a@b.com", reply_visibility=Visibility.PUBLIC)
    assert run_final_guard(d, "telegram").action is ReplyAction.HANDOFF


def test_too_long_downgraded():
    d = ReplyDecision(action=ReplyAction.AUTO_REPLY, reply_text="x" * 5000)
    out = run_final_guard(d, "telegram")
    assert out.action is ReplyAction.HANDOFF
    assert "GUARD_TOO_LONG" in out.reason_codes


def test_empty_reply_blocked():
    d = ReplyDecision(action=ReplyAction.AUTO_REPLY, reply_text="  ")
    assert run_final_guard(d, "telegram").action is ReplyAction.HANDOFF


def test_clean_reply_passes():
    d = ReplyDecision(action=ReplyAction.AUTO_REPLY, reply_text="您好，请提供订单号。")
    assert run_final_guard(d, "telegram").action is ReplyAction.AUTO_REPLY
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/unit/test_final_guard.py -v`
Expected: FAIL

- [ ] **Step 3: 实现**

```python
# src/social_reply/domain/reply/guard.py
import re
from dataclasses import replace

from social_reply.domain.reply.decision import ReplyAction, ReplyDecision, Visibility

# 账户号/长数字串、邮箱——公开回复禁止回显（PLAN.md §五 Final Guard）
_PII_PATTERNS = (
    re.compile(r"\d{6,}"),
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
)
_MAX_TEXT_LENGTH = {"telegram": 4096, "facebook": 2000, "instagram": 1000}
_DEFAULT_MAX = 2000


def _downgrade(decision: ReplyDecision, code: str) -> ReplyDecision:
    return replace(
        decision,
        action=ReplyAction.HANDOFF,
        reason_codes=decision.reason_codes + (code,),
        source="guard",
    )


def run_final_guard(decision: ReplyDecision, platform: str) -> ReplyDecision:
    """纯确定性输出闸门；任一项失败降级为 handoff 并记录 reason_code。
    仅对 auto_reply 生效——其它 action 原样返回。"""
    if decision.action is not ReplyAction.AUTO_REPLY:
        return decision
    text = decision.reply_text or ""
    if not text.strip():
        return _downgrade(decision, "GUARD_EMPTY")
    if (
        decision.reply_visibility is Visibility.PUBLIC
        and any(p.search(text) for p in _PII_PATTERNS)
    ):
        return _downgrade(decision, "GUARD_PII_LEAK")
    if len(text) > _MAX_TEXT_LENGTH.get(platform, _DEFAULT_MAX):
        return _downgrade(decision, "GUARD_TOO_LONG")
    return decision
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/unit/test_final_guard.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add -A
git commit -m "feat: Final Guard（PII 回显/长度/空文本输出侧闸门）"
```

---

### Task 5: 三级 Kill Switch

**Files:**
- Create: `src/social_reply/infrastructure/killswitch.py`
- Test: `tests/unit/test_killswitch.py`

- [ ] **Step 1: 写失败测试（用内存 Fake，无需 Redis）**

```python
# tests/unit/test_killswitch.py
from social_reply.infrastructure.killswitch import KillSwitchChecker


class _FakeRedis:
    def __init__(self, present: set[str]):
        self._present = present

    async def mget(self, keys):
        return [b"1" if k in self._present else None for k in keys]


async def test_no_flags_enabled():
    checker = KillSwitchChecker(_FakeRedis(set()))
    assert await checker.is_disabled("b1", "acc1") is False


async def test_global_flag_disables_all():
    checker = KillSwitchChecker(_FakeRedis({"killswitch:global"}))
    assert await checker.is_disabled("b1", "acc1") is True


async def test_brand_flag_disables_brand():
    checker = KillSwitchChecker(_FakeRedis({"killswitch:brand:b1"}))
    assert await checker.is_disabled("b1", "acc1") is True
    assert await checker.is_disabled("b2", "acc1") is False


async def test_account_flag_disables_account():
    checker = KillSwitchChecker(_FakeRedis({"killswitch:account:acc1"}))
    assert await checker.is_disabled("b1", "acc1") is True
    assert await checker.is_disabled("b1", "acc2") is False
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/unit/test_killswitch.py -v`
Expected: FAIL

- [ ] **Step 3: 实现**

```python
# src/social_reply/infrastructure/killswitch.py
from typing import Protocol


class _RedisLike(Protocol):
    async def mget(self, keys: list[str]) -> list[bytes | None]: ...


class KillSwitchChecker:
    """全局 / 品牌 / 账号 三级自动回复急停（PLAN.md §十九 P0）。
    任一层标志位存在即视为禁用——秒级停发的最后一道产品级开关。"""

    def __init__(self, redis: _RedisLike):
        self._redis = redis

    async def is_disabled(self, brand_id: str, account_id: str) -> bool:
        keys = [
            "killswitch:global",
            f"killswitch:brand:{brand_id}",
            f"killswitch:account:{account_id}",
        ]
        values = await self._redis.mget(keys)
        return any(v is not None for v in values)
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/unit/test_killswitch.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add -A
git commit -m "feat: 三级 kill switch（全局/品牌/账号急停）"
```

---

### Task 6: 决策管线编排

**Files:**
- Create: `src/social_reply/application/reply_decision/__init__.py`
- Create: `src/social_reply/application/reply_decision/pipeline.py`
- Test: `tests/unit/test_decision_pipeline.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_decision_pipeline.py
from social_reply.domain.reply.decision import ReplyAction
from social_reply.domain.reply.llm import StubLLMClient
from social_reply.application.reply_decision.pipeline import (
    DecisionSnapshot, run_decision_pipeline,
)


class _OpenSwitch:
    async def is_disabled(self, brand_id, account_id):
        return False


class _ClosedSwitch:
    async def is_disabled(self, brand_id, account_id):
        return True


def _snap(state="BOT_ACTIVE", text="请问怎么改邮箱"):
    return DecisionSnapshot(
        text=text, platform="telegram", brand_id="b1", account_id="acc1",
        conversation_key="telegram:acc1:9", automation_state=state, state_version=1,
    )


async def test_bot_active_normal_question_auto_replies_via_llm():
    d = await run_decision_pipeline(_snap(), llm=StubLLMClient(), killswitch=_OpenSwitch())
    assert d.action is ReplyAction.AUTO_REPLY
    assert "STUB_LLM" in d.reason_codes


async def test_human_active_forces_ignore():
    d = await run_decision_pipeline(_snap(state="HUMAN_ACTIVE"), llm=StubLLMClient(),
                                    killswitch=_OpenSwitch())
    assert d.action is ReplyAction.IGNORE
    assert "HUMAN_ACTIVE" in d.reason_codes


async def test_draft_only_downgrades_auto_reply_to_draft():
    d = await run_decision_pipeline(_snap(state="BOT_DRAFT_ONLY"), llm=StubLLMClient(),
                                    killswitch=_OpenSwitch())
    assert d.action is ReplyAction.DRAFT


async def test_killswitch_forces_draft():
    d = await run_decision_pipeline(_snap(), llm=StubLLMClient(), killswitch=_ClosedSwitch())
    assert d.action is ReplyAction.DRAFT
    assert "KILLSWITCH" in d.reason_codes


async def test_risk_word_handoff_before_llm():
    d = await run_decision_pipeline(_snap(text="我要起诉你们"), llm=StubLLMClient(),
                                    killswitch=_OpenSwitch())
    assert d.action is ReplyAction.HANDOFF
    assert "RISK_WORD" in d.reason_codes
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/unit/test_decision_pipeline.py -v`
Expected: FAIL

- [ ] **Step 3: 实现**

```python
# src/social_reply/application/reply_decision/pipeline.py
from dataclasses import dataclass, replace

from social_reply.domain.reply.decision import ReplyAction, ReplyDecision
from social_reply.domain.reply.guard import run_final_guard
from social_reply.domain.reply.llm import LLMClient, LLMContext
from social_reply.domain.reply.rules import apply_rules


@dataclass(frozen=True)
class DecisionSnapshot:
    """决策入口读到的会话快照——纯数据，脱离数据库会话。
    state_version 用于 tx2 的 CAS（防接管竞态 defense 1）。"""
    text: str | None
    platform: str
    brand_id: str
    account_id: str
    conversation_key: str
    automation_state: str
    state_version: int


class _KillSwitch:
    async def is_disabled(self, brand_id: str, account_id: str) -> bool: ...  # Protocol 占位


async def run_decision_pipeline(
    snapshot: DecisionSnapshot, *, llm: LLMClient, killswitch
) -> ReplyDecision:
    """纯管线：状态门 → kill switch → 规则 → LLM → Final Guard → 草稿降级。
    不触碰数据库、不持有事务（真实 LLM 慢调用不阻塞入站与接管翻转）。"""
    # 状态门：人工接管中，AI 一律不自动发（PLAN.md §六）
    if snapshot.automation_state == "HUMAN_ACTIVE":
        return ReplyDecision(action=ReplyAction.IGNORE,
                             reason_codes=("HUMAN_ACTIVE",), source="rule")

    # 全局/品牌/账号急停：降级为草稿（仍生成供人工参考，但不外发）
    if await killswitch.is_disabled(snapshot.brand_id, snapshot.account_id):
        return ReplyDecision(action=ReplyAction.DRAFT,
                             reason_codes=("KILLSWITCH",), source="rule")

    # 确定性规则优先于 LLM
    ruled = apply_rules(snapshot.text)
    if ruled is not None:
        decision = ruled
    else:
        decision = await llm.decide(
            LLMContext(text=snapshot.text or "", conversation_key=snapshot.conversation_key)
        )

    # 输出侧闸门
    decision = run_final_guard(decision, snapshot.platform)

    # 草稿先行：BOT_DRAFT_ONLY 把 auto_reply 降级为 draft（PLAN.md §十八）
    if snapshot.automation_state == "BOT_DRAFT_ONLY" and decision.action is ReplyAction.AUTO_REPLY:
        decision = replace(decision, action=ReplyAction.DRAFT)

    return decision
```

（`_KillSwitch` 占位仅为可读性；实际注入 Task 5 的 `KillSwitchChecker` 或测试 fake，靠鸭子类型。可删除该占位类，`killswitch` 不加注解——保持与测试一致。实现时删除 `_KillSwitch` 类，`killswitch` 参数不加类型注解。）

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/unit/test_decision_pipeline.py -v`
Expected: PASS（5 个测试）

- [ ] **Step 5: 提交**

```bash
git add -A
git commit -m "feat: 决策管线编排（状态门/急停/规则/LLM/Guard/草稿降级）"
```

---

### Task 7: Outbox 写入与 reply_decisions 持久化（CAS defense 1）

**Files:**
- Create: `src/social_reply/application/reply_decision/persist.py`
- Test: `tests/integration/test_decision_persist.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/integration/test_decision_persist.py
import uuid

import pytest
from sqlalchemy import insert, select

from social_reply.application.reply_decision.persist import persist_decision
from social_reply.application.reply_decision.pipeline import DecisionSnapshot
from social_reply.domain.reply.decision import ReplyAction, ReplyDecision, Visibility
from social_reply.domain.automation.state_machine import ensure_state
from social_reply.infrastructure.database import models

pytestmark = pytest.mark.integration


async def _seed(session):
    account_id, contact_id, conv_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await session.execute(insert(models.PlatformAccount).values(
        id=account_id, brand_id="b1", platform="telegram", name="acc", chatwoot_inbox_id=101))
    await session.execute(insert(models.Contact).values(
        id=contact_id, platform="telegram", platform_account_id=account_id, external_user_id="9"))
    await session.execute(insert(models.Conversation).values(
        id=conv_id, brand_id="b1", platform="telegram", platform_account_id=account_id,
        contact_id=contact_id, conversation_key="telegram:x:9"))
    msg_id = uuid.uuid4()
    await session.execute(insert(models.Message).values(
        id=msg_id, conversation_id=conv_id, direction="inbound", sender_type="contact",
        text="hi", chatwoot_message_id=55))
    await ensure_state(session, conv_id, "BOT_ACTIVE")
    await session.commit()
    return account_id, conv_id, msg_id


def _snap(conv_id, account_id, state="BOT_ACTIVE", version=1):
    return DecisionSnapshot(
        text="hi", platform="telegram", brand_id="b1", account_id=str(account_id),
        conversation_key="telegram:x:9", automation_state=state, state_version=version,
    )


async def test_auto_reply_writes_decision_and_outbox(session):
    account_id, conv_id, msg_id = await _seed(session)
    decision = ReplyDecision(action=ReplyAction.AUTO_REPLY, reply_text="您好",
                             reply_visibility=Visibility.PUBLIC, reason_codes=("STUB_LLM",))
    outbox_id = await persist_decision(
        session, _snap(conv_id, account_id), conv_id, msg_id, account_id, decision, "v0")
    await session.commit()
    assert outbox_id is not None
    dec = (await session.execute(select(models.ReplyDecision))).scalar_one()
    assert dec.action == "auto_reply" and dec.outbox_id == outbox_id
    ob = (await session.execute(select(models.OutboxMessage))).scalar_one()
    assert ob.status == "PENDING" and ob.payload["text"] == "您好"
    assert ob.message_type == "text"


async def test_handoff_writes_decision_no_outbox(session):
    account_id, conv_id, msg_id = await _seed(session)
    decision = ReplyDecision(action=ReplyAction.HANDOFF, reason_codes=("RISK_WORD",))
    outbox_id = await persist_decision(
        session, _snap(conv_id, account_id), conv_id, msg_id, account_id, decision, "v0")
    await session.commit()
    assert outbox_id is None
    assert (await session.execute(select(models.OutboxMessage))).first() is None
    # handoff 把会话置 HANDOFF_PENDING
    st = (await session.execute(select(models.AutomationState))).scalar_one()
    assert st.state == "HANDOFF_PENDING"


async def test_draft_writes_private_outbox(session):
    account_id, conv_id, msg_id = await _seed(session)
    decision = ReplyDecision(action=ReplyAction.DRAFT, reply_text="草稿供参考")
    outbox_id = await persist_decision(
        session, _snap(conv_id, account_id, state="BOT_DRAFT_ONLY"), conv_id, msg_id,
        account_id, decision, "v0")
    await session.commit()
    assert outbox_id is not None
    ob = (await session.execute(select(models.OutboxMessage))).scalar_one()
    assert ob.message_type == "private_note"


async def test_cas_fails_when_state_version_moved(session):
    # 决策快照 version=1，但会话已被翻转（version=2）→ auto_reply 不写 outbox
    account_id, conv_id, msg_id = await _seed(session)
    from social_reply.domain.automation.state_machine import flip_to_human_active
    await flip_to_human_active(session, conv_id, "3", "agent_public_reply")  # version→2
    await session.commit()
    decision = ReplyDecision(action=ReplyAction.AUTO_REPLY, reply_text="您好")
    outbox_id = await persist_decision(
        session, _snap(conv_id, account_id, version=1), conv_id, msg_id, account_id, decision, "v0")
    await session.commit()
    assert outbox_id is None  # CAS 落空
    assert (await session.execute(select(models.OutboxMessage))).first() is None
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_decision_persist.py -v`
Expected: FAIL

- [ ] **Step 3: 实现**

```python
# src/social_reply/application/reply_decision/persist.py
import hashlib
import uuid

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.application.reply_decision.pipeline import DecisionSnapshot
from social_reply.domain.automation.state_machine import AutomationStateEnum
from social_reply.domain.reply.decision import ReplyAction, ReplyDecision
from social_reply.infrastructure.database import models


def _idempotency_key(account_id: uuid.UUID, conversation_id: uuid.UUID,
                     message_id: uuid.UUID, action: str) -> str:
    # PLAN.md §十二：不含 prompt_version（换版重投不得产生重复发送）
    raw = f"{account_id}:{conversation_id}:{message_id}:{action}"
    return hashlib.sha256(raw.encode()).hexdigest()


async def persist_decision(
    session: AsyncSession, snapshot: DecisionSnapshot, conversation_id: uuid.UUID,
    message_id: uuid.UUID | None, account_id: uuid.UUID, decision: ReplyDecision,
    prompt_version: str,
) -> uuid.UUID | None:
    """在调用方事务内写 reply_decisions（永远写）+ 按 action 落地副作用。
    auto_reply/draft → 写 outbox（auto_reply 受 state_version CAS 守护，defense 1）。
    返回 outbox_id 或 None。调用方负责 commit。"""
    outbox_id: uuid.UUID | None = None
    message_type: str | None = None

    if decision.action is ReplyAction.AUTO_REPLY:
        # CAS defense 1：仅当会话仍是 BOT_ACTIVE 且 version 未变时才写 outbox
        current = (await session.execute(
            select(models.AutomationState.state, models.AutomationState.state_version)
            .where(models.AutomationState.conversation_id == conversation_id)
        )).first()
        if (current is not None
                and current.state == AutomationStateEnum.BOT_ACTIVE
                and current.state_version == snapshot.state_version):
            message_type = "text"
    elif decision.action is ReplyAction.DRAFT:
        message_type = "private_note"
    elif decision.action is ReplyAction.HANDOFF:
        # 转人工：置 HANDOFF_PENDING（仅当当前非终态）
        await session.execute(
            update(models.AutomationState)
            .where(
                models.AutomationState.conversation_id == conversation_id,
                models.AutomationState.state.notin_(
                    [AutomationStateEnum.HUMAN_ACTIVE, AutomationStateEnum.CLOSED]
                ),
            )
            .values(state=AutomationStateEnum.HANDOFF_PENDING,
                    state_version=models.AutomationState.state_version + 1,
                    state_changed_reason="rule_or_guard_handoff")
        )

    if message_type is not None:
        outbox_id = uuid.uuid4()
        await session.execute(insert(models.OutboxMessage).values(
            id=outbox_id, tenant_id=snapshot.brand_id and "default", conversation_id=conversation_id,
            platform_account_id=account_id,
            destination_type="chatwoot_conversation", destination_id=snapshot.conversation_key,
            message_type=message_type,
            payload={"text": decision.reply_text or "", "visibility": decision.reply_visibility},
            idempotency_key=_idempotency_key(account_id, conversation_id,
                                             message_id or conversation_id, decision.action),
            status="PENDING",
        ))

    await session.execute(insert(models.ReplyDecision).values(
        id=uuid.uuid4(), tenant_id="default", conversation_id=conversation_id,
        message_id=message_id, action=decision.action, intent=decision.intent,
        risk_level=decision.risk_level, confidence=decision.confidence,
        reply_text=decision.reply_text, reply_visibility=decision.reply_visibility,
        reason_codes=list(decision.reason_codes), source=decision.source,
        prompt_version=prompt_version, state_version_at_decision=snapshot.state_version,
        outbox_id=outbox_id,
    ))
    return outbox_id
```

（注：`tenant_id=snapshot.brand_id and "default"` 是笔误占位——实现时用 `tenant_id="default"`。修正为 `tenant_id="default"`。）

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/integration/test_decision_persist.py -v`
Expected: PASS（4 个测试）

- [ ] **Step 5: 提交**

```bash
git add -A
git commit -m "feat: 决策持久化与 Outbox 写入（state_version CAS defense 1）"
```

---

### Task 8: 接管竞态 defense 3 + CLOSED 重开对账

**Files:**
- Modify: `src/social_reply/domain/automation/state_machine.py`（flip 取消 PENDING outbox；补 CLOSED→HUMAN_ACTIVE）
- Test: `tests/integration/test_takeover_cancels_outbox.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/integration/test_takeover_cancels_outbox.py
import uuid

import pytest
from sqlalchemy import insert, select

from social_reply.domain.automation.state_machine import (
    can_transition, AutomationStateEnum, ensure_state, flip_to_human_active,
)
from social_reply.infrastructure.database import models

pytestmark = pytest.mark.integration


async def _seed_conv_with_outbox(session, ob_status="PENDING"):
    account_id, contact_id, conv_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await session.execute(insert(models.PlatformAccount).values(
        id=account_id, brand_id="b1", platform="telegram", name="a", chatwoot_inbox_id=101))
    await session.execute(insert(models.Contact).values(
        id=contact_id, platform="telegram", platform_account_id=account_id, external_user_id="9"))
    await session.execute(insert(models.Conversation).values(
        id=conv_id, brand_id="b1", platform="telegram", platform_account_id=account_id,
        contact_id=contact_id, conversation_key="telegram:x:9"))
    await ensure_state(session, conv_id, "BOT_ACTIVE")
    ob_id = uuid.uuid4()
    await session.execute(insert(models.OutboxMessage).values(
        id=ob_id, conversation_id=conv_id, platform_account_id=account_id,
        destination_type="chatwoot_conversation", destination_id="k", message_type="text",
        payload={"text": "在途回复"}, idempotency_key=str(ob_id), status=ob_status))
    await session.commit()
    return conv_id, ob_id


async def test_flip_cancels_pending_outbox(session):
    conv_id, ob_id = await _seed_conv_with_outbox(session, "PENDING")
    await flip_to_human_active(session, conv_id, "3", "agent_public_reply")
    await session.commit()
    ob = (await session.execute(
        select(models.OutboxMessage).where(models.OutboxMessage.id == ob_id))).scalar_one()
    assert ob.status == "CANCELLED"  # defense 3：接管取消在途发送


async def test_flip_does_not_cancel_already_sent(session):
    conv_id, ob_id = await _seed_conv_with_outbox(session, "SENT")
    await flip_to_human_active(session, conv_id, "3", "agent_public_reply")
    await session.commit()
    ob = (await session.execute(
        select(models.OutboxMessage).where(models.OutboxMessage.id == ob_id))).scalar_one()
    assert ob.status == "SENT"  # 已发送的不动


def test_closed_can_reopen_to_human_active():
    # Plan 1 backlog：CLOSED 收到坐席公开消息应可重开为 HUMAN_ACTIVE
    assert can_transition(AutomationStateEnum.CLOSED, AutomationStateEnum.HUMAN_ACTIVE)
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_takeover_cancels_outbox.py -v`
Expected: FAIL

- [ ] **Step 3: 修改 state_machine.py**

在 `_ALLOWED` 的 `CLOSED` 条目加入 `HUMAN_ACTIVE`（Plan 1 backlog 对账）：

```python
    AutomationStateEnum.CLOSED: {
        AutomationStateEnum.BOT_ACTIVE,
        AutomationStateEnum.HUMAN_ACTIVE,
    },
```

在 `flip_to_human_active` 的 `if flipped:` 块内，审计写入之后追加取消 PENDING/RETRY outbox（defense 3）：

```python
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
        # defense 3：接管即取消该会话所有未发送 outbox（PLAN.md §六 竞态第 3 层）
        await session.execute(
            update(models.OutboxMessage)
            .where(
                models.OutboxMessage.conversation_id == conversation_id,
                models.OutboxMessage.status.in_(["PENDING", "RETRY"]),
            )
            .values(status="CANCELLED", last_error_code="TAKEOVER")
        )
    return flipped
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/integration/test_takeover_cancels_outbox.py -v`
Run: `uv run pytest tests/unit/test_state_machine.py -v`（确认 CLOSED 改动没破坏既有转换测试；`test_closed_is_terminal_except_reopen` 断言的是 `CLOSED→HUMAN_ACTIVE` 为 False——**该测试需更新**）
Expected: 新测试 PASS；更新后的 state_machine 单测 PASS

注意：Plan 1 的 `test_closed_is_terminal_except_reopen` 里有 `assert not can_transition(CLOSED, HUMAN_ACTIVE)`——本任务改变了该语义，实现者需把该断言改为 `assert can_transition(CLOSED, HUMAN_ACTIVE)` 并在 commit message 注明这是 Plan 1 backlog 的对账修正。

- [ ] **Step 5: 提交**

```bash
git add -A
git commit -m "feat: 接管取消在途 outbox（defense 3）+ CLOSED→HUMAN_ACTIVE 对账"
```

---

### Task 9: 决策管线接入 processor

**Files:**
- Modify: `src/social_reply/application/event_ingestion/processor.py`（tx1 后运行管线 + tx2 持久化）
- Create: `src/social_reply/application/reply_decision/runner.py`（组织 tx2 与依赖注入）
- Test: `tests/integration/test_processor_decision.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/integration/test_processor_decision.py
import uuid

import pytest
from sqlalchemy import func, insert, select

from social_reply.application.event_ingestion.processor import process_raw_event
from social_reply.infrastructure.database import models

pytestmark = pytest.mark.integration


def _payload(**o):
    p = {"event": "message_created", "id": 55, "content": "请问怎么改邮箱",
         "message_type": "incoming", "private": False,
         "created_at": "2026-07-14T10:00:00Z",
         "sender": {"id": 9, "type": "contact"},
         "conversation": {"id": 77, "inbox_id": 101, "status": "pending"},
         "account": {"id": 1}}
    p.update(o)
    return p


async def _seed_account(session, automation_default="BOT_ACTIVE"):
    aid = uuid.uuid4()
    await session.execute(insert(models.PlatformAccount).values(
        id=aid, brand_id="b1", platform="telegram", name="a", chatwoot_inbox_id=101,
        automation_default=automation_default))
    await session.commit()
    return aid


async def _seed_raw(session, payload):
    r = (await session.execute(insert(models.RawEvent).values(
        source="chatwoot", payload=payload).returning(models.RawEvent.id))).scalar_one()
    await session.commit()
    return str(r)


async def _count(session, model):
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


async def test_bot_active_inbound_produces_pending_outbox(session):
    await _seed_account(session, "BOT_ACTIVE")
    await process_raw_event(await _seed_raw(session, _payload()))
    assert await _count(session, models.ReplyDecision) == 1
    ob = (await session.execute(select(models.OutboxMessage))).scalar_one()
    assert ob.status == "PENDING" and ob.message_type == "text"


async def test_draft_only_inbound_produces_private_note_outbox(session):
    await _seed_account(session, "BOT_DRAFT_ONLY")
    await process_raw_event(await _seed_raw(session, _payload()))
    ob = (await session.execute(select(models.OutboxMessage))).scalar_one()
    assert ob.message_type == "private_note"


async def test_risk_word_inbound_handoff_no_outbox(session):
    await _seed_account(session, "BOT_ACTIVE")
    await process_raw_event(await _seed_raw(session, _payload(content="我要起诉，无法出金")))
    assert await _count(session, models.OutboxMessage) == 0
    dec = (await session.execute(select(models.ReplyDecision))).scalar_one()
    assert dec.action == "handoff"
    st = (await session.execute(select(models.AutomationState))).scalar_one()
    assert st.state == "HANDOFF_PENDING"


async def test_agent_reply_does_not_trigger_decision(session):
    # 坐席公开回复只触发 HUMAN_ACTIVE，不产生决策/outbox
    await _seed_account(session, "BOT_ACTIVE")
    await process_raw_event(await _seed_raw(session, _payload()))  # 先建会话 + 1 decision
    await process_raw_event(await _seed_raw(session, _payload(
        id=56, message_type="outgoing", sender={"id": 3, "type": "user"})))
    assert await _count(session, models.ReplyDecision) == 1  # 仍是 1，坐席回复不决策
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_processor_decision.py -v`
Expected: FAIL

- [ ] **Step 3: 实现 runner.py**

```python
# src/social_reply/application/reply_decision/runner.py
import uuid

import redis.asyncio as aioredis
from sqlalchemy import select

from social_reply.application.reply_decision.persist import persist_decision
from social_reply.application.reply_decision.pipeline import (
    DecisionSnapshot, run_decision_pipeline,
)
from social_reply.domain.reply.llm import StubLLMClient
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.killswitch import KillSwitchChecker
from social_reply.shared.config import get_settings

_llm = StubLLMClient()  # 先 Stub 后接真：Plan 2b/后续按 settings.llm_provider 切换


def _make_killswitch():
    return KillSwitchChecker(aioredis.from_url(get_settings().redis_url))


async def run_and_persist_decision(
    snapshot: DecisionSnapshot, conversation_id: uuid.UUID,
    message_id: uuid.UUID, account_id: uuid.UUID,
) -> uuid.UUID | None:
    """tx1 提交后调用：跑纯管线（不持事务），再在 tx2 写决策+outbox。
    返回 outbox_id（供 Plan 2b enqueue 投递）。"""
    settings = get_settings()
    decision = await run_decision_pipeline(snapshot, llm=_llm, killswitch=_make_killswitch())
    async with get_session_factory()() as session:
        outbox_id = await persist_decision(
            session, snapshot, conversation_id, message_id, account_id,
            decision, settings.prompt_version,
        )
        await session.commit()
    # Plan 2b：if outbox_id: enqueue deliver_outbox(outbox_id)
    return outbox_id
```

- [ ] **Step 4: 修改 processor.py 接入管线**

在 `processor.py` 顶部 import 追加：

```python
from social_reply.application.reply_decision.pipeline import DecisionSnapshot
from social_reply.application.reply_decision.runner import run_and_persist_decision
```

把 `process_raw_event` 改为在 tx1 提交后运行决策（用 `_process` 返回的快照）。将 `_process` 的返回类型从 `str` 改为返回 `(status, snapshot | None, ids)`。最小改法——`_process` 末尾 INBOUND_USER 分支构造并返回快照：

`_process` 的返回改为 `tuple[str, DecisionSnapshot | None, uuid.UUID | None, uuid.UUID | None]`（status, snapshot, conversation_id, message_id, account_id）。为简洁用一个小 dataclass：

在 `processor.py` 顶部（import 后）定义：

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class _Outcome:
    status: str
    snapshot: DecisionSnapshot | None = None
    conversation_id: uuid.UUID | None = None
    message_id: uuid.UUID | None = None
    account_id: uuid.UUID | None = None
```

把 `_process` 所有 `return "XXX"` 改为 `return _Outcome("XXX")`，并在 INBOUND_USER 成功末尾（`return _Outcome("PROCESSED")` 之前）构造快照。具体：把 `_process` 结尾

```python
    if event_class is EventClass.AGENT_PUBLIC_REPLY:
        await flip_to_human_active(...)
    return "PROCESSED"
```

改为：

```python
    if event_class is EventClass.AGENT_PUBLIC_REPLY:
        await flip_to_human_active(
            session, conversation.id, msg.sender_id, "agent_public_reply"
        )
        return _Outcome("PROCESSED")

    # 仅 INBOUND_USER 走决策管线：读当前状态快照供 tx2 CAS
    state_row = (await session.execute(
        select(models.AutomationState.state, models.AutomationState.state_version)
        .where(models.AutomationState.conversation_id == conversation.id)
    )).one()
    snapshot = DecisionSnapshot(
        text=msg.content, platform=account.platform, brand_id=account.brand_id,
        account_id=str(account.id), conversation_key=conversation.conversation_key,
        automation_state=state_row.state, state_version=state_row.state_version,
    )
    return _Outcome("PROCESSED", snapshot, conversation.id, message_id, account.id)
```

把 `process_raw_event` 改为：

```python
async def process_raw_event(raw_event_id: str) -> None:
    async with get_session_factory()() as session:
        raw = (await session.execute(
            select(models.RawEvent).where(models.RawEvent.id == uuid.UUID(raw_event_id))
        )).scalar_one()
        try:
            outcome = await _process(session, raw)
        except (KeyError, ValueError, TypeError, AttributeError):
            await session.rollback()
            outcome = _Outcome("PARSE_FAILED")
        await session.execute(
            update(models.RawEvent)
            .where(models.RawEvent.id == uuid.UUID(raw_event_id))
            .values(processing_status=outcome.status, processed_at=datetime.now(UTC))
        )
        await session.commit()

    # tx1 已提交。仅 INBOUND_USER 有快照 → tx2 决策与 outbox（PLAN.md §十一 分事务）
    if outcome.snapshot is not None:
        await run_and_persist_decision(
            outcome.snapshot, outcome.conversation_id,
            outcome.message_id, outcome.account_id,
        )
```

同时把 `_process` 的类型注解从 `-> str` 改为 `-> _Outcome`。

- [ ] **Step 5: 运行确认通过 + 全量回归**

Run: `uv run pytest tests/integration/test_processor_decision.py -v`
Expected: PASS（4 个测试）
Run: `uv run pytest -q`
Expected: 全部 PASS（Plan 1 的 test_processor.py 仍需过——注意：那些测试断言的是 tx1 行为，`process_raw_event` 现在多跑一次 tx2；对 INBOUND_USER 会多出 1 个 ReplyDecision + 可能 1 个 OutboxMessage。**Plan 1 的 test_processor.py 若有 `_count(OutboxMessage)==0` 之类断言会被打破**——实现者需检查并更新：`test_inbound_user_message_full_chain` 现在会额外产生 decision/outbox；`test_self_echo_via_outbox_is_skipped` 预置了一条 SENT outbox，其断言 `_count(Message)==1` 不受影响但需确认 outbox 计数断言。仔细核对并更新受影响的 Plan 1 断言，在报告中列出每一处改动及理由。）
Run: `uv run ruff check`
Expected: clean

- [ ] **Step 6: 提交**

```bash
git add -A
git commit -m "feat: 决策管线接入 processor（tx1 入站 / tx2 决策+outbox 分事务）"
```

---

## Self-Review 记录

1. **Spec 覆盖**：规则引擎 → Task 2；Stub LLM 结构化决策 → Task 1/3；Final Guard → Task 4；kill switch（PLAN §十九 P0）→ Task 5；决策管线 → Task 6；事务性 Outbox 写入 + CAS defense 1 → Task 7；defense 3 → Task 8；接入 → Task 9。**不在本计划（Plan 2b）**：Outbox 投递 worker、Chatwoot Messages API 客户端、defense 2（发送前复检）、scheduler 补扫、真实 LLM 接入。
2. **占位符扫描**：Task 6 的 `_KillSwitch` 占位类与 Task 7 的 `tenant_id=snapshot.brand_id and "default"` 笔误已在正文标注为"实现时删除/修正"——实现者必须按注释修正为 `killswitch` 无注解、`tenant_id="default"`。
3. **类型一致性**：`ReplyDecision` 字段（action/reply_text/intent/risk_level/confidence/reply_visibility/handoff_team/reason_codes/source）在 Task 1 定义，Task 2/3/4/6/7 一致使用；`DecisionSnapshot` 字段在 Task 6 定义，Task 7/9 一致；`persist_decision` 签名在 Task 7 定义，Task 9 runner 一致调用；OutboxMessage 字段沿用 Plan 1 Task 2 模型。
4. **对 Plan 1 的破坏面**：Task 8 改 `CLOSED` 转换语义（需更新 Plan 1 的 `test_closed_is_terminal_except_reopen`）；Task 9 让 INBOUND_USER 多产生 decision/outbox（需更新 Plan 1 `test_processor.py` 受影响断言）——两处都已在任务内显式要求核对并说明。
