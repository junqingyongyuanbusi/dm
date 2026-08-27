# 知识库意图清单

来源：`AI社媒编辑一览 - 自动评论回复模版.csv`（100 行 `question,reply`，外汇/交易社媒场景）。

用途：给每条知识文档打 `category`（导入 CSV 的可选列，见 `admin_console.py:2426`），用于覆盖率盘点、
风险分层和人工审核路由。**当前 100 行的 `category` 全为空**，且 `category` 不参与检索
（全仓无任何按 category 过滤/加权的代码），所以它今天只是标签，不改变召回。

> 本文是内容侧盘点，不是运行时契约。行为以代码与迁移为准。

---

## 一、11 个意图，按风险分层

| # | 意图 | 条数 | 典型问法 | 答复策略 | 风险位 |
|---|---|---|---|---|---|
| **T0 安全直答** | | **19** | | | |
| 1 | `greeting_smalltalk` 问候/引导私信 | 4 | Hello、Hi、Check DM. | 固定寒暄，无事实 | 无 |
| 2 | `praise_thanks` 致谢/正向反馈 | 15 | Thanks、Nice、Pure gold. | 固定致谢 | 无 |
| **T1 可自动答** | | **36** | | | |
| 3 | `education_concept` 术语/机制解释 | 21 | What is pip?、What is margin? | 静态知识，长期有效 | 无 |
| 4 | `beginner_onboarding` 入门引导/导流 | 12 | I'm new.、Learn forex free? | 指向 bio/置顶帖 | 链接失效 |
| 5 | `tooling_platform` 平台/指标推荐 | 3 | Which platform?、Which indicators? | 品牌口径 | 变相荐单 |
| **T2 需谨慎** | | **18** | | | |
| 6 | `market_outlook` 行情方向/预测 | 10 | Gold target today?、EURUSD going up? | **答案含时效事实** | ⚠️ 见 §二.2 |
| 7 | `signal_request` 信号/收益承诺请求 | 2 | Trade signal?、Daily profit target? | 明确拒答 + 免责 | 合规红线 |
| 8 | `trading_psychology` 心态/亏损复盘 | 3 | Lost trade.、handle emotions | 共情 + 风控 | 情绪升级 |
| 9 | `authenticity_ai_challenge` 内容真伪/AI 质询 | 3 | Is this AI？、AI slop、Fake? | 品牌统一口径 | 公关 |
| **T3 高危** | | **27** | | | |
| 10 | `broker_regulation_check` 经纪商合规核验 | 20 | Is this broker regulated?、Is FCA good? | 通用监管科普 + 引导私信 | ⚠️ 见 §二.1、§二.3 |
| 11 | `fraud_loss_complaint` 资金受损/诈骗投诉 | 7 | They stole my money.、blocked withdrawals | **应转人工** | ⚠️ 见 §二.1 |

合计 100 条。

---

## 二、意图与代码闸门的实际错配

### 1. 风险词闸门与高危意图反向错配（最严重）

`apply_multilingual_rules()`（`src/social_reply/domain/reply/rules.py:108`）对
`MULTILINGUAL_RISK_PHRASES` 做 casefold 子串匹配，命中即 `HANDOFF/MULTILINGUAL_RISK`，
在检索之前执行（`runner.py:744`），模板永远打不出来。

实测 100 行：

- **`broker_regulation_check` 被误拦 6 条**：L6 `Scam?`、L26 `How avoid scams?`、
  L39 `Avoid cloning broker scams.`、L45 `Is this platform a scam?`、
  L70 `Where can I report a scam broker?`、L84 `Big scam.` — 全部因为子串 `scam`。
  这些是纯科普问法，写了模板但永远不会被使用。
- **`fraud_loss_complaint` 穿透 6/7 条**：L18 `Lost money.`、L36 `They stole my money.`、
  L47 `I lost all my savings.`、L53 `This broker refuses to withdraw funds.`、
  L65 `...promised guaranteed weekly returns`、L99 `They blocked withdrawals.`
  —— 均不匹配任何风险短语，会走自动回复。
  L53/L99 是拒付出金，词表里有 `cannot withdraw` / `unable to withdraw`，但覆盖不到
  `refuses to withdraw` / `blocked withdrawals` 这两种主动语态写法。

净效果：**该拦的没拦，不该拦的全拦了。**

### 2. 行情类模板携带时效事实，不适合原文直答

`market_outlook` 的 10 条答案里写死了当日行情叙事：L25「watch out for the upcoming NFP
news release today」、L35「driven by the upcoming Fed meeting」、L43「pullback entries on
USDJPY near the daily support line」、L62「stronger-than-expected US economic data」。

知识库是静态的。`KNOWLEDGE_VERBATIM_REPLY=true` 时这些会被原文直出
（`pipeline.py:197`），等于对着今天的用户念上个月的行情。

### 3. L88 携带无主语的监管事实

`Regulated where?` → `This specific broker is licensed under the FCA in the United Kingdom.`

「This specific broker」在原始语境外无所指。任何问「在哪监管」的用户都会拿到「FCA / 英国」
这个具体结论。这是可执行的错误监管信息，风险高于 §二.2。

### 4. 近义问法密集，`exact_ambiguous` 与向量混淆风险

同意图内高度重叠但答案不同的组：

1. `Scam?` / `Is fake?` / `Fake?` / `Is this platform a scam?` / `Big scam.`
2. `What leverage?` / `Best leverage ratio?` / `Is leverage too risky?` / `Explain leverage please.`
3. `Regulated?` / `Is this broker regulated?` / `Regulated where?`
4. `What is margin?` / `What is margin call?`
5. `Help` / `Help please.`；`Guide` / `Guide me please friend.`；`Stop loss?` / `I need a stop loss guide.`

自动回复闸门要求 `similarity ≥ 0.8` 且 `margin ≥ 0.08`
（`config.py:141-142`，`runner.py:808-813`）。以上各组的 margin 大概率压不住 0.08，
结果是 `NO_STRONG_KNOWLEDGE_MATCH` → 转人工。**这批模板写了也用不上。**

---

## 三、CSV 数据缺陷（2 条，逗号错位）

1. **L101**：`...essential elements I need to include? Y,"our Trading Plan Checklist: 1. ..."`
   逗号切在 `Y` 之后而不是 `include?` 之后。question 尾部多个 ` Y`，reply 首部丢了 `Y`，
   变成 `our Trading Plan Checklist`。
2. **L65**：`This account manager promised guaranteed weekly returns,.Block them immediately!...`
   句号落到了 reply 开头，reply 以 `.Block` 起始。

导入器会 `strip()` 首尾空白（`importer.py:77-78`），所以 CSV 里普遍存在的前后导空格不影响
精确匹配；但上面两条是字段边界错位，strip 救不了。

---

## 四、KB 未覆盖但真实会到达的意图（按优先级）

1. `abuse_troll` 辱骂/挑衅 —— 社媒公开评论必然出现，当前无策略，会被当普通问题送进 LLM。
2. `business_partnership` 商务合作/投放咨询 —— 高价值线索，应转人工而非自动答。
3. `spam_promo` 引流/互粉/黑产广告 —— 应 IGNORE，不应消耗模型额度。
4. `pii_disclosure` 用户主动发身份证/银行卡/账号 —— `redact_pii()` 只作用于送进 LLM 的文本
   （`pipeline.py:213-227`），入站侧无对应意图与话术。
5. `competitor_smear` 竞品攻击/黑公关 —— 回复即表态，应转人工。

---

## 五、落地方式

`category` 是导入 CSV 的可选列，直接填本文第一列的 slug 即可：

```csv
question,reply,category
Hello,Hello! Welcome to our trading community. How can we help you today?,greeting_smalltalk
```

导入后 `category` 会显示在 admin 知识文档列表（`admin_console.py:2404`）。
它不影响检索排序——要让意图参与路由，需要另开改动。
