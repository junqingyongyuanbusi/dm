# Plan 2c：真实 OpenAI LLM 接入 + 投递失败语义收紧 实施计划

> **For agentic workers:** 使用 subagent-driven-development 逐任务实施。步骤用 checkbox 追踪。

**Goal:** 决策管线接入真实 OpenAI（structured outputs，全部凭证走环境变量占位，用户后续自行填写），并落实 Plan 2b 终审遗留（5xx 歧义化、_finalize 守卫、真退避、PII 加固、token 生产校验）。

**Architecture:** `OpenAILLMClient` 实现既有 `LLMClient` Protocol（httpx 直调 Chat Completions + `response_format: json_schema strict`，可注入 transport），runner 按 `settings.llm_provider` 切换 stub/openai；LLM 任何失败 fail-safe 降级 HANDOFF（绝不外发不确定内容）。

**Tech Stack:** httpx（复用既有依赖，不新增 openai SDK）、pydantic 校验 LLM 输出、pytest（Fake/MockTransport，无外部凭证即全绿）。

---

### Task 0: 配置扩展 + 生产校验（.env 占位）

**Files:** Modify `src/social_reply/shared/config.py`、`.env.example`；Test `tests/unit/test_config.py`（追加）

- Settings 新增：`openai_api_key: str = ""`、`openai_base_url: str = "https://api.openai.com/v1"`、`openai_model: str = "gpt-4o-mini"`、`openai_timeout_seconds: float = 30.0`。
- `_reject_default_secret_in_prod` 扩展两条：非测试环境 (a) `chatwoot_api_token in ("", "dev-local-token")` 拒绝；(b) `llm_provider == "openai"` 且 `openai_api_key == ""` 拒绝。
- `.env.example` 追加占位（用户后续填写）：

```
# --- Plan 2c：OpenAI（用户填写真实值） ---
LLM_PROVIDER=stub            # 改为 openai 启用真实 LLM
OPENAI_API_KEY=              # sk-...
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-4o-mini
# --- 真实 Chatwoot（用户填写真实值） ---
CHATWOOT_BASE_URL=http://localhost:3000
CHATWOOT_API_TOKEN=          # api_access_token
```

- 测试：TESTING=true 下默认值可用；testing=False 时默认 chatwoot_api_token 抛 ValueError；testing=False + llm_provider=openai + 空 key 抛 ValueError。
- Commit: `feat: OpenAI/Chatwoot 配置项与生产校验（凭证走环境变量占位）`

### Task 1: OpenAILLMClient（structured outputs + 失败矩阵）

**Files:** Create `src/social_reply/domain/reply/openai_client.py`；Test `tests/unit/test_openai_client.py`

- `OpenAILLMClient(api_key, base_url, model, timeout, transport=None)`，实现 `async def decide(self, context: LLMContext) -> ReplyDecision`。
- 请求：`POST {base_url}/chat/completions`，`Authorization: Bearer`，`response_format={"type":"json_schema","json_schema":{"name":"reply_decision","strict":true,"schema":...}}`。schema 字段：action(enum auto_reply/draft/handoff/ignore)、reply_text、intent、risk_level(enum low/medium/high)、confidence(number 0-1)、reply_visibility(enum public/private)，全 required、additionalProperties=false（strict 模式要求）。
- system prompt：中文客服助手，规则要点（不确定→handoff、高风险→draft、不回显用户敏感信息）；user 内容 = context.text。
- 解析：pydantic 模型校验 `choices[0].message.content` 的 JSON → 映射 ReplyDecision（source="llm"，reason_codes=("OPENAI",)，附 confidence 等）。
- **失败矩阵（PLAN §五，全部 fail-safe → HANDOFF）**：
  - JSON/schema 校验失败：重试一次（同请求），再失败 → HANDOFF，reason `LLM_SCHEMA_FAIL`；
  - 超时/网络错误/HTTP 4xx/5xx：不重试（上层入站 actor 已有 Dramatiq 重试）→ HANDOFF，reason `LLM_UNAVAILABLE`；
  - `refusal` 字段非空 → HANDOFF，reason `LLM_REFUSAL`。
  - 所有失败路径 `logger.warning/exception` 记录。
- 测试用 `httpx.MockTransport`：成功解析、schema 失败重试一次后降级、超时降级、refusal 降级。
- Commit: `feat: OpenAI LLM 客户端（structured outputs + 失败矩阵 fail-safe HANDOFF）`

### Task 2: runner 按 llm_provider 切换

**Files:** Modify `src/social_reply/application/reply_decision/runner.py`；Test `tests/unit/test_runner_llm_provider.py`

- 把模块级 `_llm = StubLLMClient()` 改为惰性 `_get_llm()`（模仿 `_get_redis` 单例模式）：provider=="openai" → 用 settings 构造 OpenAILLMClient；否则 Stub。未知 provider 抛 ValueError（配置错误应显式失败而非静默 Stub）。
- `run_and_persist_decision` 内 `run_decision_pipeline(snapshot, llm=_get_llm(), ...)`。注意 `_get_llm()` 构造异常应与 killswitch 同路 fail-closed？——不需要：构造仅拼参数不联网，配置校验已在 Settings 层完成；但把 `_get_llm()` 调用放进现有 try 块之外保持简单，写明理由注释。
- 测试：monkeypatch settings llm_provider 断言返回类型；unknown provider 抛错。注意 `_llm`/settings 缓存重置（`get_settings.cache_clear()` + `runner._llm = None`）。
- Commit: `feat: runner 按 llm_provider 切换 Stub/OpenAI（默认 stub）`

### Task 3: Final Guard PII 正则加固

**Files:** Modify `src/social_reply/domain/reply/guard.py`；Test 追加 `tests/unit/test_final_guard.py`

- 真 LLM 会回显用户输入，`\d{6,}` 只命中连续数字，`8812-3456`、`138 0013 8000` 逃逸。改法：
  - 新增归一化：`_normalized_digits = re.sub(r"[\s\-–—.·]", "", text)` 后再跑 `\d{6,}`（原文与归一化后都检查）；
  - 保留 email 模式不变。
- 回归测试：带空格/连字符手机号、卡号被拦；正常含短数字（如“3 天内”）不误伤；原有用例不回归。
- Commit: `fix: Final Guard PII 去分隔符归一化匹配（真 LLM 接入前加固）`

### Task 4: 投递失败语义收紧（Plan 2b 终审）

**Files:** Modify `src/social_reply/application/message_delivery/outbox.py`；Test 追加 `tests/integration/test_deliver_outbox.py`

- **5xx 歧义化**：`httpx.HTTPStatusError` 且 `response.status_code >= 500` → NEEDS_REVIEW / `AMBIGUOUS_SEND`（服务端可能已建消息）；4xx 保持 FAILED / `SEND_ERROR` 可重试路径（或明确 4xx 也基本不可恢复——统一按现有 FAILED 语义，但 5xx 必须歧义化）。
- **ConnectError 细分**：`httpx.ConnectError`（请求未发出，明确未送达）从歧义类挪到 FAILED 可重试；其余 Timeout/TransportError 仍 NEEDS_REVIEW。
- **_finalize 守卫**：所有终态 UPDATE 加 `WHERE status == "SENDING"`，rowcount==0 时 logger.warning（行已被 sweep 转走），消除对 120s/10min 时间参数的隐式耦合。
- **真退避**：FAILED 时 `next_attempt_at = now + timedelta(seconds=30 * 2 ** attempt_count)`（上限 1 小时）。
- 回归测试：5xx → NEEDS_REVIEW；ConnectError → FAILED 且 next_attempt_at > now；_finalize 在行已非 SENDING 时不覆盖。
- Commit: `fix: 5xx 歧义化 + ConnectError 可重试 + _finalize SENDING 守卫 + 指数退避`

### Task 5: 全量验证 + 终审

- `uv run pytest -q` 全绿 + `uv run ruff check`；派发整体终审（重点：LLM 失败矩阵 fail-safe 完备性、guard 与 LLM 输出组合、outbox 新语义与 sweep/defense 的组合）。

---

**Self-Review:** 凭证全程环境变量占位，自动化测试全 Fake/Mock 无需真实 key；LLM 失败一律 HANDOFF 不外发；5xx 歧义化后重试仅剩 4xx/ConnectError 明确失败路径，与"无幂等键不盲重"一致；两跳 SENT_TO_CHATWOOT 回流、tx2 补偿、KILLSWITCH_UNAVAILABLE 告警仍留后续（备忘录已记）。
