# Plan 4：任意语言同语种回复（detect-any → reply-same）

> **For agentic workers:** 使用 subagent-driven-development 逐任务实施。步骤用 checkbox 追踪。

**Goal:** 客户用任意语言发消息，机器人就用同一语言回复，不局限于任何固定语言清单。无强命中时保持静默 handoff（不发同语种兜底文案）。

**Architecture:** 语言解析改为三级级联（确定性检测 → LLM 兜底判语种 → und），`domain/reply/language.py` 保持纯同步确定性不变，异步兜底放在新的应用层模块；输出闸门按语言来源分级（strict / lenient），lenient 由 grounding verifier 兜底；投递层零改动，依靠"lenient 模式必须写入非 und 的 `reply_language`" + 既有的 `grounding_verified is True` 硬前提维持安全链条自洽。

**Tech Stack:** 复用既有设施，不引入新依赖——`LLMClient` Protocol 的可选能力模式（照抄 `translate_to_english`）、`language.py::_letter_scripts` 做文字系统一致性判断、`reply_decisions` 已有的 `request_language_source` / `reply_language` 字段（**无需迁移**）。

---

## Context：为什么做这件事

线上（Railway `reply-core` / worker）实测：日语 `こんにちは` 命中知识库（相似度 1.0）后仍走 HANDOFF。逐层排查确认这不是单点 bug，而是链路上五道独立闸门共同作用的结果。

### 一个必须先纠正的认知

环境变量 `MULTILINGUAL_SUPPORTED_LANGUAGES=en,zh,ja,es,fr,de,pt,ar,ru,th` **在代码里零引用**。全库 `rg`（含 `scripts/`、`.env.example`、`deploy/`）只命中两处文档，均把它记录为"已移除"：`docs/multilingual-reviewed-localization.md:16`、`docs/production-migration.md:82`。`MULTILINGUAL_EXPERIMENTAL_*` 同理。

`scripts/validate_railway_config.py` 也已单独确认——它的 `_REQUIRED_SHARED` / `_REQUIRED_EXPLICIT_GATES` 只要求 `MULTILINGUAL_KNOWLEDGE_REPLY_ENABLED`，不涉及这批变量，删除它们不会让配置校验失败。

"只支持 10 种语言"这个限制**并不存在于代码中**。真正的天花板在检测层分支覆盖、guard 的脚本表与词表、以及投递层的语言一致性校验。这批死配置已误导过一次调试——我们一度以为线上跑的是宽松阈值 0.5/0.001，实际跑的是 0.8/0.08。

### 六道闸门现状

| # | 位置 | 问题 | 本次 |
|---|---|---|---|
| 1 | `language.py::detect_language` | 拉丁短文本 `len(words) < 2` 或过不了 lingua 阈值 → und → `UNKNOWN_LANGUAGE` handoff。实测 `Hola` / `Bonjour` / `Hello` / `Olá` / `Gracias` / `Danke` / `Guten Tag` / `Hello there` 全部 und | ✅ 处理 |
| 2 | `runner.py::_assess_answer_match` | 阈值过严。已临时下调生产配置缓解（0.8→0.55、0.08→0.0） | ❌ 另列 |
| 3 | `openai_client.py::_build_system_prompt` | 无语言白名单，本身不阻塞 | — 无需改 |
| 4 | `guard.py::run_final_guard` | `allowed_scripts` 表只覆盖 29 种语言；`factual_tokens` 时间单位词表只覆盖约 16 种语言；拉丁字符占比启发式 | ✅ 处理 |
| 5 | `outbox.py:532-565` | `reply_language == "und"` 硬拒绝；`grounding_verified is not True` 硬拒绝 | ✅ 锁死（零代码改动） |
| 6 | `question_tsv` 用 `simple` 分词器索引英文 | 词法臂对非英语查询召回恒为零，混合检索退化成纯向量 | ❌ 另列（认知） |

**闸门 2 的后续正解**（不在本计划范围）：`margin=0` 是权宜之计，实测副作用明确——日语牌照类问题会选中答非所问的条目（选中「我的券商信息有误怎么举报」sim=0.741，而真正对口的 `Where can I verify the broker's licence number?` 根本没进 top1），模型看到证据对不上后正确地 handoff。正解是把 top-k 证据一起交给 LLM 挑选，而非让确定性门控硬选唯一答案。

**闸门 4(b) 的实证**：逐条核对 `_TIME_UNIT_PATTERNS` 的 `day` 正则 `business\s+days?|days?|工作日|天|jours?|días?|dias?|営業日|일|วัน|дн(?:я|ей)?|أيام?|दिन|दिवस|hari|දින|ວັນ|ថ្ងៃ|ရက်`，**德语 Tage/Werktage、意大利语 giorni、越南语 ngày、土耳其语 gün、荷兰语 dagen、波兰语 dni 均不在其中**（`rg "Tage|giorni|ngày|gün|dagen|dni" guard.py` 零命中）。德语本就在旧的"10 种支持语言"清单内，说明这个漏洞与语言清单无关，且现有测试未覆盖——`test_fact_guard_uses_target_locale_for_grouping_separators` 用德语但只测货币不测时间单位。

> 运行时实测已于 Task 0b 补齐：确认缺失语言为 **7 种**（de / it / vi / tr / nl / pl / sv），比此处原先估计的 6 种多一个瑞典语。

### 闸门 6 · 词法检索对非英语基本失效 —— 新发现

`migrations/versions/b7d1e4a9c2f3_hybrid_retrieval_asymmetric_embed.py:33` 与 `models.py:1275`：

```sql
question_tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', question)) STORED
```

`retrieval.py:271` 对应用 `plainto_tsquery('simple', normalized)`。

`simple` 分词器不做词干还原、不去停用词，只是小写化后按非词字符切分。而被索引的是**英文** question 文本。于是非英语查询切出来的词元与英文词元永远不会相交——**混合检索的词法臂对非英语查询召回恒为零，RRF 融合退化成纯向量检索**。

这解释了排查期观测到的现象：日语 `こんにちは` 原文向量相似度只有 0.489，经查询翻译成 `Hello` 后拿到精确匹配 1.0。翻译不只是"换个说法再试一次"——它是**唯一能让词法臂重新生效的路径**，因此 `query_translation` 在非英语链路上是承重的，它的失败模式（LLM 不可用、占位符还原不一致时静默返回 None）会直接把召回打回半残状态。

本次不修，但有两个后果要写进认知：
1. 评估非英语召回质量时，不能拿英语的阈值经验直接套用
2. 闸门 2 的后续改造（top-k 交给 LLM）应当一并考虑给非英语查询补一条真正有效的词法通路，或明确接受"非英语只有向量臂"

### 已确认的产品决策

| 决策点 | 结论 |
|---|---|
| 覆盖边界 | 任意语言；确定性检测不出时用 LLM 兜底判语种 |
| 无强命中时 | 保持静默 handoff，不发同语种兜底文案 |
| 罕见语言输出校验 | 信任模型 + grounding verifier 放行，不再 fail-closed |

---

## Task 0：grounding verifier 对抗样本验证（阻塞性门槛）

**Files:** Create `tests/unit/test_grounding_adversarial.py`（或一次性验证脚本，结论写回本文档）

Task 2 用一道 LLM 闸门替换一道确定性闸门，**必须先证明它兜得住**，否则按替代方案收窄范围。

`openai_client.py::verify_grounding` 的 system prompt 已要求 `preserves every fact, subject-object relationship, negation, condition, exception, time, amount, entity, and limitation`，理论上覆盖时间单位。但理论不算数，实测生产 `gpt-4o-mini`：

- [x] 批准答案 `Refunds take 3 to 5 business days.` vs `返金は3〜5営業月かかります。` → 期望 `faithful=false`
- [x] 同上 vs `Rückerstattungen dauern 3 bis 5 Monate.` → 期望 `faithful=false`
- [x] 同上 vs `Rückerstattungen dauern 30 bis 5 Werktage.` → 期望 `faithful=false`
- [x] 同上 vs `Rückerstattungen dauern 3 bis 5 Werktage.` → 期望 `faithful=true`

**通过标准：** 每条篡改样本重复 5 次全部判 false。这道闸门此后是唯一防线，不接受概率性漏检。

> ### ✅ 已执行（2026-08-22）：门槛通过，Task 2 走主方案
>
> 实测 `gpt-4o-mini`，7 组样本 × 5 次 = 35 次调用：
>
> | 样本 | 期望 | 实际 |
> |---|---|---|
> | ja 単位篡改 `営業日`→`営業月` | false | false ×5 |
> | de 单位篡改 `Werktage`→`Monate` | false | false ×5 |
> | de 数值篡改 `3`→`30` | false | false ×5 |
> | it 单位篡改 `giorni`→`mesi` | false | false ×5 |
> | de 捏造承诺（加 `und sind garantiert`） | false | false ×5 |
> | de 忠实译文 | true | true ×5 |
> | ja 忠实译文 | true | true ×5 |
>
> 全部符合预期。**结论：grounding verifier 能可靠拦截时间单位与数值篡改，Task 2 的时间单位降级方案安全。**
>
> **附带发现：** 35 次中有 1 次触发了网络异常。`verify_grounding` 对任何异常都返回 `False`（fail-closed，行为正确），意味着 **grounding verifier 的可用性直接决定自动回复率上限**——按本次约 3% 的中转错误率，会有同比例的合格回复被降级成 handoff。这不是本方案引入的问题，但上线后应监控该异常率。

**不通过时的替代方案：** 仅当英文批准答案不含任何时间单位时才允许 lenient 放行；含时间单位维持 fail-closed。覆盖大部分问候/流程类问答，只牺牲含时效承诺的少数条目。

### Task 0b：补齐排查期未跑通的运行时实测

Bash 权限分类器在排查期持续故障，以下三项只做到源码级确认，动手前必须补运行时证据：

- [x] `factual_tokens` 逐语言对比 `Refunds take 3 to 5 business days.` 的译文
- [x] `reply_language_matches` 对未收录语言实测
- [x] 精确清点 `allowed_scripts` 字典的语言数

> ### ✅ 已执行（2026-08-22）：结果如下
>
> **时间单位缺失语言 = 7 种**（18 种受测语言中）：`de` `it` `vi` `tr` `nl` `pl` **`sv`**。
> 比文档原先估计的 6 种多一个瑞典语。印尼语 `id` 反而通过（`hari` 已在词表内）。
> 通过的 11 种：`id` `ja` `fr` `zh` `es` `pt` `ru` `ko` `th` `ar` `hi`。
>
> **`allowed_scripts` 确认恰好 29 种**：`zh ja ko ar fa ur ru uk bg th el hi mr bn he pa gu ta te kn ml or si lo my am km hy ka`。
>
> **未收录语言实测**：`ne`（天城文）→ `(False, 'und')`、`am`（埃塞俄比亚文）→ `(False, 'und')`，确认被拦。`sw`（斯瓦希里语，拉丁文字）→ `(True, 'sw')` 正常通过。
>
> **⚠️ 新发现（改变 Task 2 范围）：近亲语言会被"自信地误判"**
>
> 短天城文文本的印地语被判成马拉地语，且 `is_reliable=True`：
>
> | 样本 | 期望 | 检测到 | is_reliable | 闸门通过 |
> |---|---|---|---|---|
> | hi 短句「धनवापसी में 3 से 5 दिन लगते हैं।」 | hi | **mr** | **True** | **False** |
> | hi 长句 | hi | hi | True | True |
> | vi 带声调 | vi | vi | True | True |
> | vi 去声调（测试串缺陷，非真实场景） | vi | tl | False | False |
>
> 印地语是主流语言，**短回复（问候语正是短的）会被 `GUARD_LANGUAGE_MISMATCH` 拦下**。
>
> 关键在于：这是"确定性检测给出了**可靠但错误**的答案"，`is_reliable=True` 意味着 **Task 1 的 LLM 兜底根本不会触发**（兜底只在 `is_reliable=False` 时启动），lenient 模式也救不了它。
>
> 根因是 `language.py:_DEVANAGARI_LANGUAGES = (HINDI, MARATHI)` 让 lingua 在短文本上做二选一。同类近亲语言对还有 `id`/`ms`、`bs`/`hr`/`sr`、`bokmal`/`nynorsk`、`zh` 变体。
>
> **对 Task 2 的影响：** 语言身份校验不能只做 `languages_match` 的严格相等。当 expected 与 observed **同属一个文字系统**时（hi vs mr 都是天城文），应判定为检测器局限而非模型失败，退到文字系统一致性校验，而不是硬失败。这个泛化恰好把 lenient 模式一并涵盖，见 Task 2。

---

## Task 1：语言解析级联

**Files:** Create `src/social_reply/application/reply_decision/language_resolution.py`、`tests/unit/test_language_resolution.py`；Modify `src/social_reply/domain/reply/llm.py`、`src/social_reply/domain/reply/openai_client.py`、`src/social_reply/application/reply_decision/runner.py`

**设计约束：** `domain/reply/language.py` 必须保持纯同步、确定性、行为不变。它被以下位置复用，任何行为漂移都会波及知识导入与投递：

- `guard.py::reply_language_matches`（输出闸门）
- `application/knowledge/importer.py:69`、`admin_console.py:2444`（`assess_knowledge_language`，判定语料语言）
- `apps/cli/knowledge_language_migration.py`
- `application/knowledge/localizations.py:220`
- `application/message_delivery/outbox.py:556`（`languages_match`）

- [x] `domain/reply/llm.py`：`LLMClient` Protocol 增加语言判定能力，返回 BCP-47 主语言标签或 `None`；`StubLLMClient` 返回 `None`（能力不可用则静默关闭回退，与既有 `translate_to_english` 约定一致）
- [x] `domain/reply/openai_client.py`：实现该能力。照抄 `translate_to_english`（`openai_client.py:369` 附近）的既有形态——独立 payload、复用 `self._grounding_timeout`、检查 `message.get("refusal")`、任何异常 `logger.exception` 后返回 `None`
- [x] 新建 `language_resolution.py`，三级级联：
  1. `detect_customer_language(text, history)` 可靠 → 直接返回，`source` 保持 `current_message` / `recent_user_history`
  2. 不可靠 → LLM 判语种，成功则 `source="llm_fallback"`、`confidence=None`
  3. LLM 也失败 → 保持 und → 沿用现有 `UNKNOWN_LANGUAGE` handoff（行为不变）
  - 调用方用 `getattr` 探测可选能力，复用 `application/knowledge/query_translation.py::translate_query_to_english` 已验证的模式
  - **成本控制**：仅当消息含实义字母时才调 LLM——复用 `language.py::_letter_scripts` 计数，emoji / 纯数字 / 空串直接返回 und
- [x] `runner.py:490` 改调用点，把 `request_language_source` 写入决策

数据库字段已存在，**无需迁移**（已核验）：`models.py:1202 reply_language String(35)`、`1218 request_language_confidence Float`、`1219 request_language_source String(32)`，`persist.py:203/217/218` 已在写入，对应迁移是 `migrations/versions/f3b8c1d4e726_multilingual_reply_evidence.py`。

`reply_decisions` 表上唯一的 CHECK 约束是 `ck_reply_decisions_localization_provenance`（`models.py:1173`），与语言标签取值无关——**没有任何 enum 或 CHECK 会限制语言标签**。`String(35)` 对 BCP-47 标签（最长如 `zh-Hant-HK`）余量充足，不存在截断风险。

**测试：**
- [x] `tests/unit/test_language.py` 的 `test_short_ambiguous_or_unsupported_text_is_unknown`（第 48-65 行）针对纯确定性 `detect_language`，**必须保持通过不变**；`test_ethiopic_script_is_ambiguous_and_fails_closed` 同理。LLM 兜底在新模块测，不污染这层断言
- [x] `test_language_resolution.py` 用 fake LLM 覆盖四条分支：确定性命中 / LLM 兜底成功 / LLM 返回 None / 无实义字母不调用 LLM

**风险：** 低。Step 1 单独上线就能解决拉丁语系短问候这个高频场景，不依赖后续任何任务，建议先合并验证一轮再继续。

---

## Task 2：输出闸门分级

**Files:** Modify `src/social_reply/domain/reply/guard.py`、`src/social_reply/domain/reply/language.py`、`src/social_reply/application/reply_decision/pipeline.py`、`src/social_reply/application/reply_decision/runner.py`、`tests/unit/test_final_guard.py`、`tests/unit/test_language.py`

前置：Task 0 已通过。

- [x] `run_final_guard` 增加校验强度参数，**默认 strict** 以保证现有全部调用点行为逐字节不变
- [x] lenient 模式（语言来自 LLM 兜底）：
  - 跳过 `reply_language_matches` 的语言身份断言
  - 改为文字系统一致性弱校验：复用 `_letter_scripts`，要求回复主文字系统 == 客户消息主文字系统
  - `reply_language` 置为目标语言标签而非 und（**这是闸门 5 的硬要求**，否则 `outbox.py:554` 直接拒发）
  - 追加 reason_code 标记语言是模型背书而非确定性验证
- [x] `allowed_scripts` 泛化：给 `run_final_guard` / `reply_language_matches` 增加可选的期望文字系统集合参数，由 runner 从客户原文算好传入。未收录语言用"客户消息主文字系统 ∪ latin"，已收录语言维持现有表
- [x] `factual_tokens` 词表缺口分层处理（不堆语言正则，违反 DRY 与 YAGNI 且永远追不完）：
  - 数值、货币、百分号**继续严格比对**——防篡改核心，与语言无关
  - 时间单位在目标语言侧识别不出时降级为"未校验"并记 reason_code，交 grounding verifier 兜底

**测试：**
- [x] 新增 lenient 模式用例
- [x] 新增德语 `Werktage` 时间单位未识别用例（当前是未被覆盖的真实漏洞）
- [x] 新增对抗样本验证时间单位降级不会放过数值篡改
- [x] 新增 `reply_language_matches` 传入期望文字系统集合的用例

**风险：** 中。用 LLM 闸门替换确定性闸门，缓解措施是 Task 0 的门槛。

---

## Task 3：投递层锁死

**Files:** Modify `src/social_reply/application/message_delivery/outbox.py`（仅注释）；Create 集成测试

**零代码改动。** Task 2 保证 lenient 模式下 `reply_language` 非 und，且 `grounding_verified is not True` 本就是投递硬前提（`outbox.py:540`），安全链条自洽。

- [x] `outbox.py:553` 附近补注释，说明它对 Task 2 的依赖关系
- [ ] `tests/integration/` 新增用例锁死"lenient 决策能过投递闸门，且 `grounding_verified` 必须为 True 才放行"，覆盖 `MULTILINGUAL_LANGUAGE_INVALID` / `MULTILINGUAL_PROVENANCE_INVALID` 两条路径 —— **未完成**：集成测试需要本地 Postgres，当前环境未提供（`tests/unit` 里也有 2 个用例因此失败）

**风险：** 低。

---

## Task 4：恢复多轮上下文（还技术债）

**Files:** Modify `src/social_reply/application/reply_decision/runner.py`（`_fetch_history`）、`tests/unit/`；Railway 环境变量

排查期为了绕开"历史污染"，把生产的 `CONVERSATION_HISTORY_LIMIT` 从默认 20 直接压到 **0**，等于**彻底关闭了多轮上下文能力**。这是权宜之计，不是终态——它会让机器人无法理解指代和上文，必须还回来。

根因已定位并复现：历史里只要出现**一条从未被回答的实质性问题**（本案是「金融庁のライセンスあるって言ってるけど、WikiFXには出てこない。どっちが正しいの？」，两次都被 handoff、从无 outbound 回复），模型就会把当前消息的 intent 判成那条旧问题，再按 contract「涉及 brokers/regulators/licenses 的事实必须有知识明确支持，否则 handoff」转人工。实测：真实 20 轮历史 → handoff 4/4；只放 1 条未回答的牌照问题 + 当前消息 → handoff 3/3；**只保留形成过"问—答"配对的轮次（13 轮）→ auto_reply 4/4**。

- [x] 只保留已应答的问答对，未被回复的 inbound 不进 prompt

  > **实施时修正了落点**：过滤没有放进 `_fetch_history`，而是放在调用点。原因是
  > `detect_customer_language` 的历史回退**需要**那些未应答的客户消息——它们仍是
  > 客户语种的有效证据。放进 `_fetch_history` 会连语言检测一起削弱，且会破坏
  > `tests/integration/test_fetch_history.py` 里 4 个测机制（排序/预算/脱敏）的用例。
  > 现在 runner 保留完整 `history` 供语言解析，另用 `model_history = _answered_turns(history)` 供生成。
- [~] 保留但标注（如 `[已转人工，本轮无需处理]`）的替代方案 —— **未采用**：直接过滤已实测有效，按 YAGNI 不引入更复杂的标注协议
- [x] 回归测试锁死"历史含未应答实质问题时，当前问候仍能自动回复"（`tests/unit/test_model_history.py`，6 个用例）
- [ ] 验证通过后把 `CONVERSATION_HISTORY_LIMIT` 恢复为默认 20 —— **必须在新代码部署到 Railway 之后**，否则旧代码没有过滤逻辑，会立刻退回原来的 handoff 问题

**风险：** 低。改动局限在 `_fetch_history`，且有明确的实测基线可比对。

---

## Task 5：清理与文档同步

**Files:** Modify `docs/configuration.md`、`docs/proposals/multilingual-knowledge-replies-adr.md`；Railway 环境变量

- [x] 删除 Railway 三个服务上的死配置（5 个 × 3 服务 = 15 项，已全部清零并复核）
- [x] 把 `api` / `scheduler` 的阈值与 worker 对齐（三个服务现均为 0.55 / 0.0）
- [x] ~~补决策可观测性：把模型返回的 `intent` / `confidence` 落进决策记录~~ —— **无需改动，此前的判断是错的**。`persist.py:192/194` 一直在写这两个字段，我先前查库时只是没 select 它们。

  > 生产数据反而直接印证了 Task 4 的诊断：`2026-08-22 04:30` 那条消息文本是「こんにちは」，落库的 `intent` 却是 `verify_broker_license`、`confidence=0.95`。意图漂移在库里有据可查，不必靠本地复现。
  >
  > 真正缺的是**各闸门判定值**的记录——`NO_STRONG_KNOWLEDGE_MATCH` 那几行 `intent` 为 NULL，因为决策没进 LLM。这属于检索侧可观测性，归到闸门 2 的后续工作。
- [x] `docs/configuration.md` 新增 Language resolution / Output guard verification strength / Conversation history 三节
- [x] `docs/proposals/multilingual-knowledge-replies-adr.md` 新增「2026-08-22 已实施的增量决策」，回填四条与 ADR 原判断相关的新证据

**风险：** 低。

---

## 线上验证

复用排查期写的离线端到端脚本（连生产 Postgres + 生产 OpenRouter 模型，跑完整链路：检索 → 门控 → 查询翻译 → 生成 → guard → grounding verifier），把用例集从 10 条扩到 20+ 种语言，**必须包含当前会失败的**：

- `de` `it` `vi` `tr` —— 时间单位词表缺口
- `ne` `am` —— `allowed_scripts` 缺口
- `Hola` `Bonjour` `Hello` `Danke` —— 短拉丁文本检测缺口

改完后在飞书用真实消息复验，核对 `reply_decisions` 表的 `request_language` / `reply_language` / `resolved_locale` / `request_language_source` / `grounding_verified` 五个字段。

---

## 风险与回滚

**不加 feature flag。** 本仓库已因遗留死配置误导过调试，每个新开关都是未来的一个死配置候选。本次改动天然渐进：lenient 路径只在确定性检测失败时触发，strict 路径行为逐字节不变。回滚等价于 `git revert`，四个 Task 各自可独立 revert，粒度已足够细。

Task 1 的 LLM 兜底判语种也有天然开关：`StubLLMClient` 返回 `None` 即静默关闭，与既有 `translate_to_english` 约定一致，不需要新增配置项。

**次要风险：** LLM 兜底会给每条确定性检测失败的消息增加一次模型往返（约 +0.3~1s）。已通过"仅在消息含实义字母时才调用"收窄触发面。若线上观测到调用量超预期，再考虑 Redis 短 TTL 缓存（YAGNI，先不做）。

**实施顺序：** Task 0 / 0b（门槛）→ Task 1 → Task 2 → Task 3 → Task 4 → Task 5。

---

## 配置影响：不新增任何配置项

本方案**不引入一个新的环境变量或配置字段**，净效果是配置项减少 5 个。

**新增：** 无。

Task 1 的 LLM 兜底判语种复用既有的 `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL` / `GROUNDING_VERIFIER_TIMEOUT_SECONDS`，天然开关是 `StubLLMClient` 返回 `None`（与既有 `translate_to_english` 约定一致）。Task 2 的 strict / lenient 由运行时的语言来源推导，不是配置。Task 3 零代码改动。

**删除（Task 5）：** `MULTILINGUAL_SUPPORTED_LANGUAGES`、`MULTILINGUAL_EXPERIMENTAL_MIN_SIMILARITY`、`MULTILINGUAL_EXPERIMENTAL_MIN_MARGIN`、`MULTILINGUAL_EXPERIMENTAL_REPLY_ENABLED`、`MULTILINGUAL_EXPERIMENTAL_ACCOUNT_IDS` —— 代码零引用。

**需要恢复的既有配置（当前处于权宜状态）：**

| 配置项 | 默认值 | 生产当前值 | 归属 |
|---|---|---|---|
| `CONVERSATION_HISTORY_LIMIT` | 20 | **0**（多轮上下文已关闭） | Task 4 修完后恢复 20 |
| `KNOWLEDGE_AUTO_REPLY_MIN_MARGIN` | 0.08 | **0.0** | 闸门 2 改造后恢复健康值 |
| `KNOWLEDGE_AUTO_REPLY_MIN_SIMILARITY` | 0.8 | 0.55 | 同上 |

前两项是排查期为跑通链路临时压下去的，**不是终态**，不要当成新基线。第三项在闸门 2 改造（top-k 交给 LLM）之前先维持。

**需要对齐的不一致：** `api` / `scheduler` 两个服务上的 `KNOWLEDGE_AUTO_REPLY_MIN_SIMILARITY` / `MIN_MARGIN` 仍是旧值 0.8 / 0.08，与 `worker` 不一致。它们不参与决策，但留着会误导排查，Task 5 一并对齐。

**必须保持开启：** `MULTILINGUAL_KNOWLEDGE_REPLY_ENABLED=true`（`outbox.py:535` 会据此拒发）、`KNOWLEDGE_RETRIEVAL_ENABLED=true`。
