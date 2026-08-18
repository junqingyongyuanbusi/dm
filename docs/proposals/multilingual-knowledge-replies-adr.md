# Proposed ADR: Multilingual knowledge replies from a canonical English knowledge base

> Status: Proposed. Not runtime authority. The Phase 1 trusted-local, SYNTHETIC internal schema/library slice is implemented. The recommended runtime/live architecture, real dm bake-off, real-data extraction and cloud candidates are not implemented; no architecture winner has been selected, and runtime configuration is unchanged.

结论先说：当前方案不是最好，更谈不上完美。它是一套偏保守的安全原型，优点是 fail-closed，缺点是运行链路太重、策略分散、运营成本高，而且仍没有真实业务数据证明效果。

基于论文、官方模型文档和当前代码，我更推荐验证下面这套架构：

> Canonical English Policy + Derived Localization + Candidate Union Reranking + Adaptive Query Translation
>
> 中文可以叫：英语权威策略库 + 派生本地化 + 候选合并重排 + 低置信度查询翻译回退。

它保留一个英语事实源，但不在每条客户消息上让 LLM 重新生成并再用同一个模型验证。用户最终看到的文字来自英语知识的版本化派生译文，运行时只做检索、决策和确定性渲染。

ADR 状态：Proposed。Phase 1 已实现受信任本地 Python candidate 与 `SYNTHETIC` 数据的内部 schema/library 首切片，并通过设计避免写入生产决策和发送表。Railway 的 multilingual live、shadow、English-only 配置保持关闭。推荐的 runtime/live 架构、真实 dm bake-off、真实数据 extraction、云 candidate、CLI/API/queue、受控 synthetic loader 和网络沙箱均未实施。

# 一、为什么当前方案不是最优

当前已部署但关闭的 live 路径，准确说是：

1. Lingua + HanzIdentifier + 大量自定义脚本规则做语言识别；
2. 英语知识做跨语言 dense retrieval；
3. lexical/RRF 虽然被计算，但自动回复资格只看纯向量 Top1、Top2 和固定 margin；
4. 强命中后让 LLM 生成目标语言回复；
5. 再调用一次通常相同的 LLM 做 grounding verification；
6. 最后叠加语言、数字、单位、专名和联系方式正则。

主要问题有五个。

第一，当前系统付出了 lexical/RRF 的查询成本，却没有使用 RRF 结果决定 live 命中。它实际上仍是纯向量阈值系统。

第二，Top1 与 Top2 按文档计算，不按批准答案或政策计算。两个不同问法如果指向同一答案，margin 会人为变小，系统会制造假歧义。

第三，正常路径是 embedding + generation LLM + grounding LLM。延迟和模型费用接近双倍，生成和验证又可能出现相关性错误，因为默认使用同一个模型。

第四，语言、安全和忠实度规则分散在语言检测、风险词表、Prompt、Guard 和 verifier 多个层面。每增加一种语言或一种政策实体，都要同步修改多处规则。

第五，知识内容和适用范围耦合。当前 content hash 只包含问答，唯一约束却是 Tenant 级；文档又只能绑定一个 Brand/Platform。相同批准内容不能自然复用于多个作用域，这属于数据模型问题，不是换 embedding 或 reranker 能解决的问题。

# 二、一手资料给出的结论

## 1. 直接跨语言 embedding 和查询翻译没有普遍赢家

BGE-M3 支持 dense、sparse 和 multi-vector 三种检索模式，覆盖 100 多种语言和最长 8192 tokens。在 MKQA 的 25 种非英语查询到英语 Wikipedia 检索中，BGE-M3 dense 的平均 Recall@100 为 75.1，all-mode 为 75.5。论文地址：https://arxiv.org/abs/2402.03216

一项 Sinhala/Tamil 查询到英语政府知识库的直接比较中，BGE-M3 Recall@15 分别达到 96.2% 和 95.6%，Google Translate 查询翻译为 92.4% 和 93.0%。但 Sinhala Recall@1 是 Google Translate 60.0%，BGE-M3 59.4%。这说明 direct embedding 在这个英语单库场景整体更强，但不是每个指标都赢。论文地址：https://arxiv.org/abs/2608.12820

Cross-Lingual Cost 研究的却是英语和阿拉伯语混合语料。在 Legal 数据集上，BGE-M3 同语言 Hit@20 为 89%，跨语言为 73%；mE5 从 88% 降到 46%。平衡双语检索或查询翻译都能改善结果。论文地址：https://aclanthology.org/2025.arabicnlp-main.6/

两组结果不冲突。英语单一语料更适合直接跨语言 embedding；混合语言语料存在文档语言竞争，查询翻译或分语言检索更有价值。

CLIRudit 还发现，短查询翻译容易破坏专名和术语，例如把姓氏、地名或短语翻错。对于 sparse retrieval，离线文档翻译通常比运行时查询翻译更好。论文地址：https://arxiv.org/abs/2504.16264

因此不建议每条请求都先翻译。更合适的是：直接检索为主，只在低置信度时使用查询翻译回退。

## 2. 固定 cosine threshold 不如候选合并后重排

Jina reranker v3 是 0.6B 多语言 listwise reranker，评测流程先用 embedding 取 Top100，再重排。论文报告 MIRACL nDCG@10 为 66.83，MKQA Recall@10 为 67.92。它不能找回 first-stage 没召回的文档，但可以改善候选之间的细粒度排序。论文地址：https://arxiv.org/abs/2509.25085

Anthropic 的 Contextual Retrieval 是厂商自报结果，但仍有参考价值。在其内部数据上，Contextual Embeddings + Contextual BM25 把 Top20 retrieval failure 从 5.7% 降到 2.9%，再加 Cohere reranker 降到 1.9%。它同时明确提醒 reranking 会增加延迟和成本，必须按自己的语料评测。官方文章：https://www.anthropic.com/news/contextual-retrieval

MIRACL 本身是查询和语料同语言的 monolingual retrieval benchmark，不能证明中文问题检索英语客服知识的效果。论文地址：https://aclanthology.org/2023.tacl-1.63/

这意味着当前 0.80 similarity + 0.08 margin 只能作为待验证 baseline。更合理的授权依据是：exact、dense、lexical、实体匹配产生候选集合，按批准答案去重，再由多语言 reranker 输出 answerability/relevance 分数。

## 3. 检索正确也不能保证回复语言正确

XRAG 专门评测问题语言和证据语言不一致的 RAG。所有五个模型都出现过回复语言错误。在 GPT-4o 的实验中，只把问题改成英语，平均分从 57.58 提升到 58.25；把支持文档也统一成英语后升到 61.16，接近全英语 61.58。说明证据语言不一致不只造成输出语言漂移，还会影响证据推理。论文地址：https://aclanthology.org/2025.findings-emnlp.849/

所以“Prompt 要求回复中文”不够。运行时必须把检索、呈现语言和输出校验拆开。

## 4. 第二次 verifier 有价值，但不该默认使用相同生成模型

Generate but Verify 比较了 intrinsic abstention、pre-answer sufficiency、post-answer NLI 等方法。Post-answer NLI 通常优于生成前的充分性判断。例如 NLI + InstructRAG 的平均 AwF F1 在多组模型上高于 pre-answer 版本；对部分模型，触发 fallback 后 BioASQ accuracy 可提升 8 到 10 个百分点。论文地址：https://aclanthology.org/2025.ijcnlp-long.56/

但论文使用英语 factoid QA，不能直接证明多语言客服政策安全。它还发现，用 Claude 驱动的 RAGAS verifier 相比专用 DeBERTa NLI 提升很小，不值得额外计算成本。

RAGAS 更适合离线评测，不适合作为唯一运行时发送门禁。论文地址：https://aclanthology.org/2024.eacl-demo.16/

Self-RAG 和 CRAG 能改进检索与自我反思，但都没有提供官方联系方式、政策条件或同语言输出的确定性保障。CRAG 还可能引入 web search，不适合直接用于官方客服事实。论文地址：https://arxiv.org/abs/2310.11511 和 https://arxiv.org/abs/2401.15884

# 三、推荐验证的目标架构

```text
客户消息
  -> Language Policy
  -> exact/entity candidates
  -> direct multilingual dense candidates
  -> lexical candidates
  -> candidate union
  -> answer/policy-level dedupe
  -> multilingual reranker
  -> low confidence?
       -> protected query translation to English
       -> retrieve again
       -> rerank merged candidates
  -> structured Action Policy
       -> general FAQ / case-specific / risk / handoff
  -> Rendering Policy
       -> English canonical reply
       -> derived reviewed localization
       -> deterministic official-contact renderer
  -> deterministic safety checks
  -> Outbox or Handoff
```

## 1. 数据模型

将当前 KnowledgeDocument 拆成稳定身份、不可变内容、作用域、检索表达和派生呈现几个边界：

### KnowledgePolicy、PolicyRevision 和 PolicyRetrievalExample

`KnowledgePolicy` 是稳定业务身份，`PolicyRevision` 保存不可变英语事实和批准答案：

- canonical approved answer；
- answer class；
- required conditions and exceptions；
- protected values and entity schema；
- slot schema；
- risk and handoff metadata。

`PolicyRetrievalExample` 保存多条 approved question、paraphrase、关键词和实体别名，全部指向同一 `PolicyRevision`。Dense/lexical 索引可以使用 example、approved answer 或二者组合；候选必须先按 `policy_revision_id` 聚合，再进行 rerank 和歧义判断，不能把同一政策的不同问法当成互相冲突的 Top1/Top2 文档。

### KnowledgeApplicability

单独描述可用范围：

- Tenant;
- Brand;
- Platform;
- account scope；
- effective time window.

这样同一条退款政策可以复用到多个 Brand/Platform，不需要复制内容，也不会被 Tenant 级 content hash 错误去重。

### KnowledgeLocalization

它是英语内容的派生构建产物，不是第二套事实源：

- policy/content hash;
- target language;
- translated text;
- translator/model version;
- protected-slot manifest;
- review status;
- reviewer;
- generated and approved time.

英语源一旦修改，旧本地化不再有资格进入新的 PolicyRelease，也不得接受新的 Job assignment；它不会从已经被 immutable assignment revision 引用的旧 release manifest 中动态消失。已 pin 到旧 release 的在途 Job 仍按原 artifact 复现。若修改属于紧急安全修正，应通过 authorization DENY 禁止发送并执行幂等 HANDOFF disposition，而不是单独动态失效 artifact。运营仍只维护英语 policy，任何修正都生成新的派生产物和新的 PolicyRelease。

运行时翻译、按需生成后缓存、离线预生成的取舍：

| 模式 | 首次延迟 | 后续延迟 | 长尾语言 | 审核成本 | 过期风险 |
| --- | --- | --- | --- | --- | --- |
| 每次运行时翻译 | 高 | 高 | 覆盖最好 | 低但风险高 | 英语更新后自动使用新源 |
| 首次生成后缓存 | 首次高 | 低 | 较灵活 | 可按使用量审核 | content hash 变化后禁止进入新 release；旧 pinned release 保持可复现 |
| 离线预生成 | 无冷启动 | 最低 | 只覆盖预设语言 | 初期最高 | 新 release 禁止引用过期 artifact；旧 pinned release 保持可复现 |

对 922 条左右的小语料，推荐“常用语言离线预生成，长尾语言首次只生成 release 外的待审 draft”。未审批 draft/build cache 不属于任何 PolicyRelease，也不能被运行时查询。审批会生成新的 immutable artifact；任何可运行 artifact 集合变化都必须创建新的 PolicyRelease。已 pin 的旧 job 永远只读取原 release 固定的 manifest。

品牌表达再拆一层，避免把语气混进事实翻译：

- `SemanticLocalization` 保存中性的语义译文，key 包含 `policy_revision_id + locale + translator_version`；
- `BrandRenderingArtifact` 保存面向某 Brand 的最终表达，key 包含 `semantic_localization_id + brand_id + voice_revision + renderer_version`。

品牌语气 revision 变化时，为后续 release 重建 Brand rendering，不需要重新翻译政策事实。旧 artifact 不再有资格进入新 release，但已 pin 旧 release 的 Job 仍可复现。Brand Voice 默认只能通过确定性 wrapper 或受限模板实现。如果确实需要 LLM 生成 BrandRenderingArtifact，该 artifact 必须有独立 hash、完整 slot manifest、语义校验和按风险分级的人工审批。运行时只能读取 release manifest 中的 approved artifact，不能临时改写。

### SlotSchema 和 ScopedValueBinding

`PolicyRevision` 只声明 `{{support_email}}`、`{{official_url}}`、`{{processing_days}}` 等 required slot、类型和约束。Tenant/Brand/Platform 对应的邮箱、URL、电话和产品代码放在 `ScopedValueBinding`，不写死在 canonical policy 中。发布时必须证明每个 required slot 在当前 Applicability 下唯一且完整；重叠作用域出现两个不同值时拒绝发布。

Applicability 与 ReleaseScopeAuthorization 必须复用同一个正式 scope resolver。首版匹配向量定义为 `(account_exact, platform_exact, brand_exact, locale_exact)`，每维 exact=1、wildcard=0。候选 A 仅在每个维度都不低于 B 且至少一维更高时支配 B；不能用实现顺序把 account-specific 和 locale-specific 等不可比候选强行排序。首版不包含 product scope，因为当前耐久消息和 Job 没有权威 product_id；引入前必须先定义 routing 来源、失败语义和完整 provenance。

Applicability 选择所有未被支配的 maximal matches。多个 maximal match 若指向不同 PolicyRevision 或 binding，或时间窗重叠且结果不同，release 构建必须拒绝。Authorization 先应用父级 DENY 覆盖规则；无 DENY 时再对 ALLOW 使用同一 maximal-match resolver。不可比且结果不同的 overlap 在配置写入或发布时拒绝。检索和发送均调用同一 resolver，scope 冲突不能交给 reranker。

### 不可变 revision 和决策 provenance

英语内容修改时创建新的 `PolicyRevision`，不原地覆盖。Applicability pin 到 revision；SemanticLocalization pin 到 source revision；BrandRenderingArtifact pin 到 localization、voice 和 renderer revision。

构建完成后生成不可变 `PolicyRelease`，一次固定 PolicyRevision、PolicyRetrievalExample、Applicability、ScopedValueBinding、Localization/Rendering artifact manifest、`RetrievalIndex`，并通过一对一关系拥有且只拥有一个 `DecisionContract`。Job 只 pin `policy_release_id`，所有 contract、index 和 artifact 都必须经该 release 关系解析，避免 release A 与 contract B 的不一致组合。

`DecisionContract` 固化 retrieval/index、Language Policy、ActionClassifier、renderer、prompt/schema、阈值、immutable `supported_locales`、Safety Policy、conversation history limit/max_chars、history ordering、normalization/redaction version，以及各组件版本。Contract 只 pin 运行时可能调用的 ModelBinding，例如 embedding、query_translation、reranker、action_classifier，以及确实位于热路径时的 optional runtime_grounding_verifier。Contract 还要声明 Language/Action 路由是否依赖历史。可变的 Canary enablement、流量比例和 kill switch 状态存放在独立运行控制表中，不得修改已 pin job 的 contract。

离线 localization、翻译质量检查、NLI/grounding 和数据集评测使用独立 immutable `ArtifactBuildContract`、`ArtifactBuildRun` 或 `EvaluationContract`，不放入 runtime DecisionContract。Artifact 固化 source revision、build contract/run、translator/verifier binding revisions 和 build evidence。轮换离线 builder endpoint/timeout 不要求重发已有 production release；只有生成新的 artifact 并让新 PolicyRelease 引用时，才影响运行时内容快照。

`ProviderDeploymentRevision` 是系统级 immutable deployment catalog，固定 endpoint/base-URL revision、provider account boundary、region/data residency、retention/logging policy、API compatibility，以及每个 component/endpoint 的 `idempotency_capability`。该能力明确为 NONE、HEADER 或 REQUEST_FIELD，并记录受支持 header/field、请求 schema、验证证据和验证时间；预生成 key 不代表 Provider 实际支持幂等。Deployment 不保存 secret 字节。

Tenant 使用 deployment 前必须存在 append-only `TenantProviderDeploymentGrantRevision(tenant_id, provider_deployment_revision_id, allowed_component_roles, secret_alias, state, reason, effective_at, created_at)`，并由 tenant-scoped `TenantProviderDeploymentGrantHead(deployment_id, active_revision_id, epoch)` 激活。Tenant-owned ModelBinding 通过 `(tenant_id, grant_head_id)` 复合外键绑定获授权 deployment，并固定 component role、model、timeout、retry policy、request/response schema version 和允许的 UNKNOWN/retry 策略。Grant 变化创建新 revision，并在同一事务切换 active head、递增 epoch，不能原地覆盖。

Secret alias 只能在当前 active grant 允许的同一 provider account、endpoint、region 和数据政策边界内轮换；跨 endpoint、region、account 或 policy 的变化必须创建新的 ProviderDeploymentRevision、grant revision、ModelBinding，以及引用它的 build/runtime contract。PolicyRelease 和 artifact build validation 必须证明 Tenant 对全部 binding revision 持有有效 grant。

旧 Job 可以继续加载 pinned DecisionContract 以复现决策，但每次 embedding、translation、rerank、classifier、runtime verifier 或离线 build/evaluation 外部调用前，都必须执行 grant invocation boundary。调用方按固定 key 取得 Tenant/provider-deployment shared advisory transaction lock，重新读取 grant head/revision/epoch，并从 secret manager 解析非敏感 `secret_material_version` 和 provider-account fingerprint，验证 component role、endpoint、region、account、retention policy 与 secret boundary。

Invocation 的原子单位是每一次 Provider HTTP/RPC attempt，不是高层 `decide()`、`embed()` 或 `verify()` 调用。Retry orchestration 必须位于统一 invocation runner；低层 Provider client 的每次 POST/RPC 都必须经该 dispatcher/callback，禁止 client 内部存在不可见的 schema retry、timeout retry 或 grounding retry。

每个网络 attempt 前，在同一事务持久化 immutable `ModelInvocationIntent`，包含 tenant、关联 DecisionJob/EvaluationDecision/ArtifactBuildRun、component role、binding/deployment/grant revisions、grant epoch、secret alias/material version、request fingerprint、预生成 provider request/idempotency key、attempt number 和 idempotency capability，并记录 DISPATCHING event 后 commit。Provider 网络调用只在 commit 后开始，不持数据库行锁跨网络请求。

Grant revoke 使用相同 key 的 exclusive advisory lock，创建新 grant revision、切换 head 并递增 epoch。Revoke 发生在 invocation boundary commit 前时必须阻止调用；发生在 commit 后时属于已进入外部调用边界的 in-flight/best-effort，结果进入审计，运行时根据结果 HANDOFF/NEEDS_REVIEW。Grant 已撤销、过期或不匹配时，在任何客户数据离开系统前 fail closed，构建/评测任务失败。

`ModelInvocationIntent` 不被覆盖。每个状态变化写入 append-only `ModelInvocationEvent`，例如 DISPATCHING、SUCCEEDED、FAILED、UNKNOWN，并记录实际 provider request ID、latency、token/cost、result/error 和 schema version。进程在 Provider 接收后、结果落库前崩溃时，由 reconciler 将无终态 intent 标记 UNKNOWN。

UNKNOWN 只有在 ProviderDeploymentRevision 明确且经验证支持幂等，并且本次请求实际使用已持久化 provider key/header/field 时，才能自动重试。非幂等 endpoint 的 UNKNOWN 在运行时必须 HANDOFF/NEEDS_REVIEW，build/evaluation fail closed；只有显式人工批准才能创建新 attempt。数据库对 `(intent_owner, component_role, attempt_no)` 和 provider idempotency key 建唯一约束，reconciler 与 worker 使用 CAS，不能并发重复 dispatch。重试创建新的 intent/event 或受约束的新 attempt，绝不覆盖旧记录。

测试必须覆盖 schema retry、timeout retry、grounding/runtime verifier 和 embedding 调用，证明每次实际网络 attempt 都有独立 intent/event、grant boundary 和 request ID。ReplyDecision/EvaluationDecision 只保存 invocation set hash 和聚合 latency/cost；DeliveryAttempt 继续只表示发送与 authorization attempt。

Client registry key 至少为 `(tenant_id, component_role, provider_deployment_revision_id, grant_revision_id, secret_material_version, model, contract_version)`。Secret material version 变化时关闭或淘汰旧 client。Secret 字节永不持久化；material version 和 account fingerprint 必须证明实际 credential 仍在当前 grant 边界内。跨 endpoint、region、account 或 policy 的 alias target 变化必须创建新的 deployment/grant/binding。

新 Job 选择 release 使用 tenant-scoped、append-only 的 `ReleaseScopeAssignmentRevision(tenant_id, brand_id?, platform?, account_id?, locale?, stable_release_id, canary_release_id?, traffic_bps, bucket_salt, revision, config_hash, effective_at)`，另由 active pointer 选择当前 revision。它只负责 release selection，ReleaseScopeAuthorization 只负责选中后是否允许发送。

`reserve_decision_job()` 在同一事务中使用稳定 `conversation_id + bucket_salt` hash 分桶，不能按每条 message 随机，解析唯一 active assignment revision，并固化 `policy_release_id + assignment_revision_id/config_hash + bucket_input/hash + selected_branch`。Assignment scope overlap 复用正式 maximal-match resolver，不可比或同级且结果不同的 active assignment 必须拒绝。

PromotionDecision 创建并激活新的 immutable assignment revision，不能原地修改 traffic、salt 或 release IDs。首版 assignment 只使用 reserve 时已知的 Tenant/Brand/Platform/account；locale assignment 只有在 pre-reservation durable language routing 已启用时才允许。当前语言在 reserve 后检测时，locale Canary 只能作为选中 release 的发送 gate：未启用语言进入 Draft/Handoff，不能自动回落 stable release。

Release 级授权使用 append-only `ReleaseScopeAuthorizationRevision(policy_release_id, tenant_id, brand_id?, platform?, account_id?, locale?, state, reason, actor, effective_at, created_at)`，并由 `ReleaseScopeAuthorizationHead(scope_key, active_revision_id, epoch)` 指向当前 revision。Revision 不原地修改，Outbox/Attempt 记录 immutable revision ID 和解析结果。

跨 release 的授权使用 append-only `SystemAuthorizationRevision`、`TenantAuthorizationRevision`，分别由 `SystemAuthorizationHead(active_revision_id, epoch)` 和 tenant-scoped `TenantAuthorizationHead(active_revision_id, epoch)` 激活。Revision 保存 state、reason、actor、effective_at 和 created_at。System/Tenant DENY 在任何 release-scoped 规则前生效。

任何授权变化都创建新 revision，并在同一数据库事务切换对应 head、递增 epoch。数据库授权是可审计事实源；Redis kill switch 只能作为急停加速和可重建缓存。

ReplyDecision 只保存 decision-time authorization provenance：System revision ID/epoch、Tenant revision ID/epoch、匹配到的 ReleaseScopeAuthorization revision ID set/head epochs、resolver result hash 和最终结果。PromotionDecision 只保存晋级时的 assignment revision 与 authorization snapshot，不承载单条消息的 send-time 状态。

每个 Outbox 和 DraftApprovalAttempt 保存最终 committed send-time provenance。每次 claim/retry 的授权解析，无论成功或失败，都写入 immutable `DeliveryAttempt` 或 `AuthorizationCheckEvent`，包括各层 revision IDs/epochs、resolver result hash、authorization_as_of、结果和失败原因，不能覆盖 ReplyDecision。

Decision-time 与 send-time epoch 不一致时不直接拒绝，也不能信任旧 child ALLOW；必须重新读取所有 heads 并解析当前完整层级。只有当前 scope 仍得到唯一 ALLOW 时才能继续，并固化新的 send-time provenance，否则 fail closed。

所有单 conversation 的 claim、approval 和 send 使用统一顺序：先取得 conversation delivery lock，再按 `System -> Tenant -> Release` 顺序取得 PostgreSQL shared advisory transaction locks，最后锁业务行。Shared authority locks 允许不同 conversation 并行；不能让每条发送对全局或 Tenant head 行执行排他 `FOR UPDATE`。

取得 locks 后重新读取所有 heads、解析授权，并在同一数据库事务写入 `Outbox.status=SENDING` 和 send authorization provenance 后 commit。Provider 网络调用只在该 commit 后开始。

Global/Tenant/release revoke 分两阶段：第一阶段只按 `System -> Tenant -> Release` 取得 exclusive authority advisory locks，创建 immutable DENY revision、原子切换对应 active head、递增 epoch 并 commit 释放，绝不能持有 exclusive authority lock 等待 conversation；第二阶段逐 conversation 取得 conversation lock，再取得 shared authority locks 和业务行锁执行补偿。Revoke 发生在 SENDING commit 前时发送必须失败，发生在 commit 后时属于 best-effort/NEEDS_REVIEW。

并发测试必须证明不同 conversation 的 send 可并行，revoke 会等待已经进入原子边界的事务并阻止尚未越过边界的发送，approval/new inbound/revoke 无锁序反转，且两个阶段都无死锁。

Authorization 使用与 Applicability 相同的 scope vector/maximal-match resolver，父级 DENY 覆盖子级 ALLOW。不可比或同级冲突在配置写入时拒绝。Release 标记为 published/eligible、assignment 选择、claim、approval、send 和 revoke compensation 都必须检查上层和 release-scoped authority。Redis 只同步为缓存或急停加速。

每个 RetrievalIndex 独立记录 `embedding_model`、dimension、distance metric 和 index build version；不同维度使用独立表/列和物理索引，禁止在当前固定 `Vector(1536)` 中混存或原地覆盖。新 index 构建完整后由新的 PolicyRelease 固定引用，旧 release 继续引用旧 index，旧 index 保留到回滚窗口结束。

PolicyRelease 只有 built、validated、published/eligible、retired 等内容生命周期状态。Published/eligible 仅表示可被 assignment revision 引用，不路由任何流量。完整性、作用域冲突、slot、embedding、artifact 和 authorization eligibility 校验全部通过后，才可标记 published/eligible。所有上线、晋级和回滚都由 PromotionDecision 创建并激活新的 immutable ReleaseScopeAssignmentRevision；不得再创建 `active_policy_release` 指针或直接切换 release 状态来分配流量。

`DecisionJob` 必须在 reserve 时与入站事件同一事务固化 `policy_release_id`、`policy_as_of` 和 assignment revision/config/bucket provenance。DecisionContract、RetrievalIndex 和 artifact manifest 只能通过 PolicyRelease 的一对一/成员关系加载。`policy_as_of` 优先使用通过平台验签且位于允许时钟偏差内的 inbound occurred_at，否则使用数据库 received_at；Applicability、slot binding 和 index filter 在所有重试中都使用该 immutable 时间。重试只能加载同一 release、assignment 和 policy_as_of；不支持 release 所属 contract 的旧 Worker 必须 fail closed，不能回落到旧 free-generation。

Authorization 使用独立的 `authorization_as_of`，取当前数据库事务时间和当前 active System/Tenant/Release heads，绝不能使用 inbound `policy_as_of`。延迟重试必须看到消息到达后发生的 kill/revoke。Future-effective authorization revision 不能提前被 resolver 选择；调度器只能在 effective_at 到达后的数据库事务中切换 active head 并递增 epoch。

`reserve_decision_job()` 遇到同 message 的完整既有 Job 时，只校验 tenant、conversation、account 和 message scope 一致，然后直接复用其已固化 release/as-of/assignment provenance；不得重新解析当前 rollout，也不得因 PromotionDecision 已切换 assignment 而把合法重复 webhook 标记 NEEDS_REVIEW。只有 legacy NULL 或 provenance 不完整的 row 才在尚未 claim、目标 release/assignment 可唯一证明时 CAS backfill，否则 fail closed。并发 insert 由 message_id 唯一约束的赢家成为权威。测试覆盖首次 reserve 后 rollout 晋级再收到重复 webhook、old/new writer race 和不同 release 并发 reserve。

Reserve 时的 PolicyRelease assignment 只使用当时已经确定的 Tenant/Brand/Platform/account scope，不按尚未检测的语言选择 release。语言只负责在固定 release 内选择 artifact，并通过 ReleaseScopeAuthorization 控制 locale Canary 和发送资格。若未来确实需要不同语言使用不同 release，必须先增加独立耐久的 pre-reservation language-routing job，固化结果后再 reserve DecisionJob，不能在 Worker 内临时改 release。

当前推荐路径中，语言和 Action 路由是 DecisionJob 内单独的 durable routing stage。它按 DecisionContract 选择历史，并固化所用 message IDs、顺序、内容 hash 和 history normalization/redaction version，或保存受限且加密的 history snapshot。重试必须复用同一证据，不得按实时 Settings 或当前数据库重新选择不同历史。

历史读取失败必须记录显式 `HISTORY_UNAVAILABLE`，不能与“确实没有历史”都表示为空 tuple。Contract 声明历史必需时，`HISTORY_UNAVAILABLE` 必须 HANDOFF；历史可选时也要在 provenance 中保存缺失状态。

语言检测完成后，用 CAS 将 `request_language`、confidence、source 和 `language_policy_version` 写回 job，再进入 retrieval；重试不得重新检测。开启语言级开关时，language 为 NULL/und 或 routing 未完成的 job 必须 fail closed。Outbox 发送前继续复检该固化语言和语言级 kill switch。

发布顺序是先部署兼容 reader 到 API/Worker/Scheduler，等待旧 Worker 和旧队列工作清空，再把新 PolicyRelease 标记为 published/eligible；随后由 PromotionDecision 创建并激活 assignment revision，才开始分配流量。

DecisionJob claim、所有 release-derived Outbox claim 及真正发送前，都要复检 `policy_release_id`、System/Tenant/Release authorization head epochs、decision generation、scope/language kill switch 和 release 所属 contract，并重新解析当前有效 revision set。Release-derived 包括 BOT/DECISION、自动 draft 的原文 `DRAFT_APPROVAL`，以及仍以旧 draft 文本为基础的轻微编辑；不能仅因 actor_kind 变成 ADMIN_HUMAN 就脱离 release 授权。

ReplyDecision 派生的 draft 和 release-derived approval Outbox 必须持久化或可无歧义解析 `policy_release_id`、decision-time authorization resolver result hash、各层 matched revision IDs/epochs、decision generation、request language 和 scope。管理员审批入口与最终发送前都必须在 conversation delivery fence 下读取当前 heads、重新解析授权并保存 send-time provenance。触发熔断时，事务性隔离或取消该 authorization scope 下尚未发送的 release-derived Outbox、pending draft 和未完成 job；已经进入外部网络调用的 SENDING 只能 best-effort 中止，不能承诺瞬时撤回。

所有 `DRAFT_APPROVAL`，包括 ACCEPTED 和 EDITED，都必须在审批入口和最终发送前针对 pinned PolicyRelease、approved artifact 和 slot manifest 运行独立 ApprovalGuard。它至少检查目标语言、required slot、官方联系方式、PII、数字/币种/期限、protected entity/ID。Outbox 固化 `final_text_hash`、`approval_guard_version`、`slot_manifest_hash` 和 guard result；失败不得创建或发送 Outbox。若产品允许人工完全脱离政策和 slot 约束，必须显式转为 `HUMAN_REWRITE/MANUAL_REPLY`，不能继续标记为 release-derived DRAFT_APPROVAL。

Chatwoot 和 direct delivery 是两条明确不同的草稿工作流。对新的 PolicyRelease-governed multilingual 路径，安全默认是不向 Chatwoot 发送 private-note draft，草稿保留在本系统 review UI；direct delivery 且没有 presentation Outbox 的 draft 才进入本系统 DRAFT_APPROVAL。

若业务 owner 明确签署并保留 Chatwoot private-note 模式，该 note 必须带 release ID、generation 和有效状态。Release 撤销后发送醒目的后续 private-note warning，并在数据库标记原 lineage stale/revoked；这只是运营提示，无法技术性撤回或阻止客服复制文本。Chatwoot 中后续人工发送属于外部 MANUAL_REPLY 风险边界，ADR 不能声称已被本系统 release fence 阻止。若未来希望一个决策同时支持 private-note presentation 和 public approval send，必须把单一 `ReplyDecision.outbox_id` 重构为带 purpose 的一对多 Outbox lineage。

每次审批或 override 使用不可变 `DraftApprovalAttempt(reply_decision_id, approval_revision, outbox_id UNIQUE, status, ...)`，idempotency key 至少为 `draft-approval:{decision_id}:{approval_revision}`。Attempt 记录 source release/generation、action type、最终文本/hash、ApprovalGuard 结果和版本、slot manifest hash、override reason、操作者、状态及 Outbox ID。Outbox 创建时必须在同一事务绑定 attempt。

Delivery claim 和最终发送前按 `outbox_id` 锁定 DraftApprovalAttempt，校验 attempt 仍 active、release/generation 有效、`sha256(payload.text)==final_text_hash`，且 guard version/result 与 slot manifest hash 一致。Outbox 的 SENT/CANCELLED/NEEDS_REVIEW/REVOKED 转换必须在同一事务推进 attempt 状态。数据库约束同一决策最多一个 active unsent attempt；CANCELLED/REVOKED attempt 不得被新 revision 复用。ReplyDecision 上的 review 字段只能作为当前摘要或指针，不能覆盖历史 attempt。

PolicyRelease-governed 发送严格要求 `origin_kind=DRAFT_APPROVAL`、`actor_kind=ADMIN_HUMAN`、非空 `approval_attempt_id`，并具备完整 release/generation/guard provenance。数据库增加 origin/actor/lineage CheckConstraint，发送端通过 DraftApprovalAttempt FK fail closed。

旧的 `origin_kind=DECISION + actor_kind=ADMIN_HUMAN + payload.approval=admin` fallback 只能服务数据库中由迁移明确标记 `authority_contract_version=legacy`、`policy_release_id IS NULL` 且早于迁移截止版本的记录；不能依据 payload 自报 legacy 身份。兼容 reader 部署后先盘点、回填或 drain 旧行，队列清空后删除 `_effective_origin_kind()` payload fallback 和对应旧测试契约。

测试必须证明伪造 legacy payload、缺失 attempt、attempt/outbox tenant 或 release 不匹配、attempt generation/guard hash 不匹配、guard provenance 缺失均不得发送。跨表一致性在 claim/send 事务内验证。

Approval、HUMAN_REWRITE、新 inbound 和 revoke 第二阶段 compensation 使用统一顺序：先取得 conversation delivery advisory lock，再按 `System -> Tenant -> Release` 取得 shared authority locks，最后依次锁 Conversation、ReplyDecision、DraftApprovalAttempt、Outbox、AutomationState。Revoke 第一阶段只持 exclusive authority locks 写 DENY/epoch 并提交，绝不取得或等待 conversation lock。现有 `_load_draft()` 需要拆成无锁 scope lookup，以及 fence 内的 `FOR UPDATE` 加载，禁止先锁 ReplyDecision 再获取 conversation lock。必须增加 approval/new inbound/revoke 并发死锁回归测试。

`reserve_conversation_generation()` 在同一 fence 中除取消旧 generation 的 BOT/DECISION Outbox 外，还必须把旧 generation 的 release-derived DRAFT_APPROVAL Outbox 和 DraftApprovalAttempt 主动标记 CANCELLED/STALE；不能只依赖最终发送时发现。独立 MANUAL_REPLY 仍按现有人工作业规则处理。

Release 撤销不能只把 Job 设为 `CANCELLED`，也不能为同一 message 创建第二个 DecisionJob/ReplyDecision，或修改原 Job 已 pin 的 PolicyRelease。每个受影响消息必须创建幂等 `DecisionDisposition`，唯一键至少为 `(message_id, revoked_policy_release_id, disposition_type)`，并在同一事务中持久化安全 HANDOFF、AutomationState 和 HumanWorkItem/notification intent。

补偿按 conversation 分批执行，并获取现有 conversation delivery advisory lock。事务内锁定 Job、Outbox 和 AutomationState，复检 `decision_generation`、superseded 状态、claim/send 状态及 release authorization 后，才能取消并创建 disposition。若 Outbox 已 SENDING、generation 已改变或消息已 superseded，进入明确的 best-effort/`DECISION_NEEDS_REVIEW` 分支，不能把补偿标记为成功。

只有补偿 disposition 成功后，Job 才能进入 `CANCELLED` + `RELEASE_REVOKED` 终态，并允许 RawEvent 聚合为 PROCESSED。补偿失败或状态不确定时，RawEvent 必须进入 `DECISION_NEEDS_REVIEW`，不能标记 PROCESSED。已经完成 DecisionJob、但其 pending draft 或尚未发送的 DRAFT_APPROVAL/其他 Outbox 被撤销时，也必须执行同一幂等补偿闭环。

普通 `ACCEPTED` 和轻微编辑不能覆盖已撤销 release。`HUMAN_REWRITE/revoked_release_override` 必须复用并扩展现有 `send_human_reply()` 工作流，不能在 draft approval route 中复制一条发送链。它继续执行 conversation delivery lock、HumanWorkItem claim/assignee 校验、AutomationState 转为 HUMAN_ACTIVE、MANUAL_REPLY intent 和审计。

进入 HUMAN_ACTIVE/HUMAN_REWRITE 的同一 fenced 事务中，除取消 BOT Outbox 外，还必须取消该 conversation/generation 下所有 release-derived approval Outbox，并同步对应 DraftApprovalAttempt 为 CANCELLED/REVOKED；真正独立的 MANUAL_REPLY 不取消。扩展字段包括 `source_reply_decision_id`、`source_release_id`、override reason、旧/新文本 diff、当前 generation、最终文本 hash 和 deterministic slot/contact/PII/language guard provenance。必须先按现有规则进入 HUMAN_ACTIVE，再创建 MANUAL_REPLY；不得放宽 `_send_state_allowed()` 让 HANDOFF_PENDING 直接发送。

`send_human_reply()` 的既有 idempotency intent 只能避免重复创建，不能跳过授权。每次调用，包括浏览器重试，都必须在 conversation fence 下重新校验 current generation、HUMAN_ACTIVE、work-item ownership、override/attempt status、text hash 和 guard provenance。失效 intent 标记 CANCELLED/NEEDS_REVIEW，不得再次 dispatch。

HUMAN_REWRITE Outbox 的授权来自现有 HUMAN_ACTIVE 和 work-item ownership，不来自已撤销 release；它不得重新启用 BOT 自动发送。Superadmin override 仍需显式审计。若业务不允许这种 override，则 HANDOFF 后只能在外部客服系统人工回复。

新增取消状态仍需同步 `_TERMINAL_JOB_STATUSES`、`_ACTIVE_JOB_STATUSES`、RawEvent aggregate、sweeper/retry 查询、admin warning、API 展示和迁移测试；不能只清除 `next_attempt_at` 后让 job 留在 PENDING/FAILED。

`ReplyDecision` 至少持久化：

- `policy_release_id`；
- 从 PolicyRelease 解析出的 `decision_contract_id` 和 hash；
- `policy_as_of`；
- assignment revision ID/config hash、bucket input/hash 和 selected branch；
- decision-time System/Tenant/Release authorization revision IDs/epochs、resolver result hash 和最终结果；
- `request_language` 和 `language_policy_version`；
- history message IDs/order/hash、normalization version 和 availability status；
- `policy_revision_id`；
- `applicability_id`；
- `localization_artifact_id` 和 hash；
- rendered slot manifest；
- retriever/reranker/model/version；
- final rendered text snapshot。

迁移期同时保留旧 `knowledge_content_hash`，完成历史和回滚验证后再切断旧读路径。

### 独立 EvaluationRun 和 EvaluationDecision

多候选 bake-off 不能复用生产 `DecisionJob`/`ReplyDecision`，因为生产模型按 message_id 和 decision_job_id 保证唯一。新增独立 `EvaluationRun` 与 `EvaluationDecision`，唯一键至少为 `(evaluation_run_id, source_message_token, candidate_contract_id)`，允许同一来源消息运行 A-E、L0-L3、Q0-Q4 和不同呈现方案。

`source_message_token` 使用 Tenant-keyed HMAC 或等价不可逆 token。原始 `message_id -> token` 映射只在受控生产侧按短 TTL 保存；评测库不同时保存生产 message_id 和 token。删除/DSAR tombstone 只保留 token/fingerprint。

EvaluationDecision 只保存候选输入指纹、contract、证据、预测、延迟、成本和错误，不进入 production generation、AutomationState、HumanWorkItem 或 Outbox。只有最终选中的 production contract 才组装正式 ReplyDecision。若需要单候选 production persistence contract 验收，必须使用隔离测试租户或数据库，并明确该入口不承担多架构 bake-off。

### 数据库级 Tenant 边界

PolicyRelease 及其整个 Policy/Applicability/Binding/Artifact/Index 图不得跨 Tenant。所有 tenant-owned 表，包括 PolicyRelease、DecisionContract、ModelBinding、TenantProviderDeploymentGrantRevision/Head、ArtifactBuildContract/Run、EvaluationContract/Run/Decision、ModelInvocationIntent/Event、PolicyRevision/Applicability、ScopedValueBinding、Artifact、DraftApprovalAttempt 和 DecisionDisposition，都必须带 `tenant_id` 与 `UNIQUE(tenant_id, id)`；关系使用 `(tenant_id, foreign_id)` 复合外键，而不是裸 UUID FK。System-level ProviderDeploymentRevision 只能通过 tenant grant 被引用。

DecisionJob、ReplyDecision、Outbox、DraftApprovalAttempt、ModelInvocationIntent/Event 与 Conversation、Message、Account、PolicyRelease、ModelBinding 的关系也必须在数据库层证明 tenant 一致。应用层过滤不是安全边界。迁移和集成测试必须用直接 SQL 尝试 tenant A release 关联 tenant B policy/artifact/message/evaluation/provider grant，并证明数据库拒绝；同时验证租户删除、grant revoke、secret alias/material rotation、回填和索引不会绕过复合约束。

## 2. 检索

第一阶段从原始客户文本直接做 multilingual dense retrieval。不要先全量翻译查询。

候选建议：

- exact and identifier match;
- dense Top20 to Top50;
- PostgreSQL lexical Top20 to Top50;
- optional entity-preserving query branch.

先按 canonical answer/policy ID 去重，再送入 reranker。Reranker 比较的是客户问题和完整 policy evidence，不再使用 raw cosine margin 作为最终授权。

如果 direct retrieval 或 reranker 低置信度，再做一次受保护查询翻译：品牌名、许可证号、Ticket ID、金额和账号占位后翻译，恢复后用英语查询检索，再与原始查询候选合并重排。

这个 cascade 保留 direct retrieval 的低延迟，又能覆盖部分低资源语言或 lexical mismatch。

## 3. 当前决策

保留“强相关不等于直接发送”。Reranker 只说明知识相关，不说明用户当前问题能安全自动处理。

Action Policy 输出固定枚举：

- AUTO_REPLY_GENERAL;
- HANDOFF_CASE_SPECIFIC;
- HANDOFF_RISK;
- HANDOFF_AMBIGUOUS;
- HANDOFF_UNSUPPORTED.

这些值属于新增的 `ActionOutcome`/`ActionClassifier` 协议，不替换现有持久化 `ReplyAction(auto_reply/draft/handoff/ignore)`，也不能塞入现有 `forced_decision` 通道。处理顺序必须是：ActionOutcome → approved artifact/slot renderer → 组装现有 ReplyDecision → Final Guard → BOT_DRAFT_ONLY/Email draft downgrade → Outbox CAS。

`AUTO_REPLY_GENERAL` 映射为候选 `auto_reply`，HANDOFF 子类映射为 `handoff` 并写 reason code；`ignore` 继续由现有规则保留。分类器不生成 `reply_text`，避免继续扩展当前把 action 与 reply_text 绑定的 generation client。AutomationState、kill switch 和现有确定性 pipeline 可以在后续任何阶段把候选 `auto_reply` 降为 `draft` 或 `handoff`。

分类器输入包括当前消息、最近客户历史、选中 policy 的 usage metadata。可以用一个短结构化 LLM 调用，也可以逐步替换成小型分类模型。

## 4. 回复呈现

英语用户直接使用英语批准文本。

非英语用户优先使用 KnowledgeLocalization。运行时不再让 LLM 自由改写英语政策，因此没有每条消息重复产生语言漂移、数字变化或条件翻转的风险。

官方邮箱、URL、电话和账号必须作为结构化 slot 存储，由 renderer 恢复，不允许翻译模型生成或复制。

对于新语言或缺少 localization 的条目：

- 低风险简单 FAQ 可以生成 draft，进入人工复核和本地化缓存；
- 复杂条件、否定、例外或官方联系方式直接 handoff；
- 不能在没有校准的情况下即席生成并自动发送。

## 5. Grounding

普通 FAQ 的用户文本来自已审核 localization 后，通常不需要每条消息再调用 grounding LLM。

建议：

- deterministic slots and entities：运行时硬检查；
- multilingual NLI：离线评测、抽样监控，或只用于新生成 localization 的审批；
- RAGAS/AwF：离线回归和 release gate；
- same-model verifier：保留为 bake-off 对照，不作为默认热路径。

这样能把当前 embedding + generation + verifier 三次外部调用，压缩为 embedding/rerank + action classifier。若 reranker 本地运行，外部大模型通常只剩一次短分类调用。

# 四、当前已部署方案的处理建议

| 当前组件 | 建议 | 理由 |
| --- | --- | --- |
| 英语知识确认、inventory、readiness、corpus fingerprint | 保留 | 单一英语事实源和可审计迁移仍然需要 |
| Shadow 和标注框架 | 保留并扩展 | bake-off 和阈值校准的基础 |
| Lingua + Hanz + 400 行启发式 | 暂时保留，必须参加替换评测 | 170 MB wheel 和维护成本很高，不应因已实现就默认长期保留 |
| 固定 10 语言 allowlist | 替换为流量驱动 allowlist | 应根据真实语言分布、样本量和风险决定 |
| dense Top1/Top2 margin | 降级为 baseline | 文档级 margin 会制造假歧义，且没有 query-document 交互 |
| lexical/RRF | 作为 candidate generator 保留，或没有收益时删除 | 当前 live 付出成本但不使用其授权结果 |
| 自由多语言生成 | 替换 | 每条消息重新生成批准政策，风险和成本都高 |
| 第二次同模型 grounding | 从热路径删除 | 延迟/成本近翻倍，错误相关；可改为离线/抽样 NLI |
| 官方联系方式 exact/verbatim Guard | 保留 | 这是当前最可靠的确定性边界 |
| 多语言风险词正则 | 简化或替换 | 逐语言维护会持续漂移，应由结构化 policy/action classifier 接管 |
| calibration report gate | 保留，但改成多架构 bake-off gate | 不应只校准一个预设方案 |

# 五、必须做的真实 bake-off

公开 benchmark 只能挑候选，不能证明哪套方案对当前业务最好。

模型比较前，先建立离线评测投影，不先提交推荐方案的 production schema：

1. 在快照内按 canonical answer/policy 聚类；
2. 补齐 Tenant/Brand/Platform scope 和 gold_available_in_scope；
3. 建立 slot 占位、实体别名和旧 ID 映射；
4. 立即修复架构无关的 scope-aware 原子写入；
5. 将删除式 query redaction 改为可逆安全占位。

`PolicyRevision`、Localization、BrandRendering 和 PolicyRelease 的生产迁移必须等 bake-off 选出赢家后再开始。

然后在同一份人工标注集比较五条检索路径：

- A：当前 OpenAI text-embedding-3-small direct dense；
- B：direct BGE-M3 或 mE5；
- C：protected query translation -> English retrieval；
- D：dense + lexical union -> multilingual reranker，并在低置信度时加入 C 的候选；
- E：按 Tenant/Brand/Platform 过滤后，使用本地 multilingual cross-encoder 对 policy/answer 簇直接打分；如果使用云 LLM relevance classification，必须先取可审计的 Top-N 候选，禁止把整个作用域语料外发。922 条总语料在作用域过滤后可能只剩几十条，这个简单基线可能比多层检索更容易维护。E 需要分别比较 question-only 与 question + approved-answer 输入，并记录候选预算、外发字段和 p95。

同时比较四种呈现：

- 当前自由生成 + same-model verifier；
- 运行时受控翻译；
- 首次生成后缓存；
- derived localization + deterministic rendering。

Language Policy 必须单独 bake-off，不能只看最终同语言率：

- L0：当前 Lingua + Hanz + 启发式；
- L1：fastText lid.176 或其他轻量 LID + 文字系统约束；
- L2：检测器 + 最近三条客户消息；
- L3：L2 + 平台 locale 弱先验，locale 不能单独授权发送。

按语言评测 precision、recall、unknown/handoff、wrong-locale artifact、短词、共享文字系统、code-switch、品牌名/数字主导消息、历史 fallback、p95、RSS 和镜像体积。只有 Language Policy 子系统单独达标，对应语言才能进入检索和自动发送；这也是 Phase 6 删除 Lingua 的前提。

Action Policy 也必须独立 bake-off：

- Q0：现有风险规则和当前 decision baseline；
- Q1：确定性 policy metadata + 规则；
- Q2：短结构化 LLM ActionClassifier；
- Q3：本地小型多语言分类器；
- Q4：Q1 先行，只有低置信度或未覆盖样本进入 Q2。

按 `AUTO_REPLY_GENERAL`、`CASE_SPECIFIC`、`RISK`、`AMBIGUOUS`、`UNSUPPORTED` gold 评测 confusion matrix，并按语言、scope、历史依赖、code-switch、实体和风险分层。核心错误是任何非 general 样本被判为 AUTO_REPLY。另需报告 safe AUTO_REPLY coverage、外发字段、p95、成本、结构化输出失败率和 provider error；任何无效或超时输出必须 HANDOFF。

只有 Action Policy 子系统和完整 E2E locked holdout 同时达标，才可启用自动发送或删除现有多语言风险规则。若 Q1/Q4 在安全相同且 coverage 与 Q2/Q3 相差不超过 2 个百分点，优先选择确定性更强、外发更少的 Q1/Q4。

数据必须来自真实短消息，按语言、Tenant、Brand、Platform、风险、歧义、文本长度、错别字、code-switch、品牌名和数字编号分层。

每条系统还要记录外部数据暴露：

| 外部阶段 | 发送内容 | 必须保护的实体 | 失败策略 |
| --- | --- | --- | --- |
| Embedding API | 原始或占位后的查询 | 邮箱、账号、许可证号、订单号、产品代码 | 超时或错误 HANDOFF |
| Query Translation | 占位后的查询和目标语言 | 品牌、域名、ID、数字、code-switch 片段 | 翻译失败不覆盖原查询，回到 direct 或 HANDOFF |
| Cloud Reranker | 查询和候选知识 | 客户 PII、内部 Tenant/Brand 元数据 | 不允许静默退化为错误 Top1 |
| Action Classifier | 查询、历史摘要、选中 policy metadata | 客户身份数据、受保护账户信息 | 结构错误或超时 HANDOFF |
| Grounding/Eval | approved answer 和候选回复 | 不应包含无关客户私密历史 | 只用于离线、抽样或明确的 fail-closed gate |

Bake-off 必须记录供应商 region、retention、日志策略和每条路径的外部调用次数。当前 blanket redaction 会删除所有 6 位以上数字，可能同时删掉许可证号和产品代码，因此应比较“删除实体”和“安全占位后保留实体语义”两条路径。

## 内部评测数据治理

当前 Phase 1 只允许 `SYNTHETIC`。使用授权去标识数据或真实客户消息前，产品、安全/合规和数据 owner 必须签署内部 `EvaluationDataUsePolicy`，明确合法使用依据、Tenant opt-in/opt-out、允许用途、访问角色、retention 和删除责任，并实现受控 extraction boundary；这些能力不属于当前切片。

评测存储与生产、开发环境隔离，使用最小权限、静态/传输加密和不可变访问审计。默认只保存 Tenant-keyed `source_message_token`、证据 fingerprint 和安全占位后的文本；评测库不得同时保存生产 message ID。原始 message ID 到 token 的映射只在受控生产侧短 TTL 保存。原文或加密 history snapshot 只有在已证明指标需要时才能保存，并设置更短 TTL。真实消息、历史和 PII 禁止写入 git、普通日志、测试快照或临时研究目录。

数据协议必须定义 TTL、定期 GC、Tenant 退出、删除请求和 DSAR 流程。Locked holdout 不得凌驾于删除权：样本删除后保留不含原文的 tombstone，废止原 dataset fingerprint，构建新的 dataset version，并重新运行或明确失效此前签署的结果，不能静默修改旧数据集。

未用于正式签署结果的 EvaluationRun/EvaluationDecision 按 TTL 删除；已用于正式 ADR/Release Gate 的运行保留最小审计证据、代码/contract/dataset fingerprint、聚合指标和签署记录，原始客户内容仍按删除与 retention 政策处理。

检索指标：Recall@1/3/5/20、MRR、nDCG、正确 policy/answer 命中、answer-cluster margin。

完整链指标：错误自动回复、错误知识命中、handoff 率、同语言率、事实忠实率、官方联系方式安全、错误 Outbox。

运维指标：p50/p95、每消息模型调用、token/费用、Worker 吞吐、超时率、镜像大小、RSS、冷启动、索引重建和运营审核时间。

## 数据切分和冻结

按 policy/answer cluster 分组，确保同一政策的 paraphrase 不跨集合泄漏；再按 Tenant/Brand/Platform、语言、positive/negative/ambiguous/risk 分层，固定 development/calibration/locked holdout 三份数据。建议比例为 50%/25%/25%。

Locked holdout 的标签和结果在候选架构、阈值、模型版本、语言 allowlist、成本预算全部冻结前不可查看，只允许一次正式验收。任何阈值调整都必须创建新的 holdout 或进入下一评测版本。

以下数值只是建议的首版门槛，不是公开论文推导出的事实。解锁 locked holdout 前必须设置正式 ADR 决策点，由产品、运营和安全/合规 owner 签署最终的错误率置信上界、最低自动化覆盖、每语言样本量、延迟/成本预算、外部供应商与数据保留约束，以及适用的 EvaluationDataUsePolicy，并记录签署版本和时间。

未完成签署时，Phase 2 只能产出 benchmark 报告，不能宣布架构赢家、进入生产 schema 迁移或解锁自动发送。

| 维度 | 最低样本/预算 | Release Gate |
| --- | --- | --- |
| 总体负向、歧义和风险样本 | 至少 3,000 | 0 次错误自动回复；单侧 95% Clopper-Pearson 上界不高于 0.1% |
| 每个启用语言 | 至少 200 个 answerable positive，300 个 negative/ambiguous/risk | 0 次错误自动回复；每语言错误率上界不高于 1%；Candidate Recall@20 至少 97%，单侧 95% 下界至少 95% |
| 高风险层 | 至少 600 | 0 次 AUTO_REPLY 或客户 Outbox；上界不高于 0.5% |
| 联系方式、slot、ID、产品代码 | 至少 300 | 0 次增删、错绑或跨 scope 泄露 |
| 总体检索 | 全部 positive | Candidate Recall@20 至少 99%，单侧 95% 下界至少 98.5%；MRR/nDCG 同时报告 |
| 自动化覆盖 | answerable low-risk positives | AUTO_REPLY recall 至少 80% 总体、70% 每语言；单侧 95% 下界至少 75% 总体、60% 每语言；不能靠全部 HANDOFF 通过 |
| Language Policy | 每语言至少 500 个实质文本，另有短词、混合和实体专集 | 被自动发送样本的语言 precision 至少 99.5% 总体、99% 每语言；wrong-locale artifact 为 0 |
| Action Policy | 复用每语言 300 个 non-general，并至少 400 个 general positives | 0 个 non-general 被授权 AUTO_REPLY；总体单侧 95% 错误率上界不高于 0.1%，每语言不高于 1%；general safe AUTO_REPLY recall 至少 80% 总体、70% 每语言；无效输出 100% HANDOFF |
| 在线延迟 | production-like 并发 | 非翻译路径端到端 p95 不高于 3 秒；翻译 fallback p95 不高于 5 秒 |
| 单消息变量成本 | 按真实 token/调用计费 | 平均不高于 0.005 美元，且不高于 A baseline 的 1.25 倍 |
| 可用性 | 故障注入 | reranker、translation、LLM、Redis 或 provider 错误均稳定 HANDOFF/DRAFT，不产生错误 Outbox |

样本不足的语言保持 Draft/Handoff，不能借用总体指标启用。多个候选都达标时，主指标依次是：更低的错误自动回复置信上界、更高的 safe AUTO_REPLY coverage、更低的外部数据暴露、更低的 p95/成本、更小的运维复杂度。如果简单 E 基线在安全相同且 coverage 与最优方案相差不超过 2 个百分点时，优先选择 E。没有候选全部达标就是 No-Go。

# 六、实施 ADR 和迁移路线

## Phase 0：冻结当前热路径

保持 Railway live=false，不继续向当前“自由生成 + same-model verifier”路径叠加策略。保留已经上线的 schema、英语审核、corpus fingerprint、readiness 和 shadow 工具，把当前代码当作 baseline。

## Phase 1：建立只读评测投影

当前切片只从调用方提供的 `SYNTHETIC` workload 建立离线、只读、带指纹的规范化投影，不改生产读路径，也不提供真实数据抽取或受控 synthetic dataset loader：

- 旧 document ID 到 answer/policy cluster 的评测映射；
- Tenant/Brand/Platform scope；
- approved question/paraphrase 和 answer；
- slot 占位及实体别名；
- gold policy、risk、ambiguity 和 expected action 标签。

这一阶段只立即修复会污染评测或现有生产安全的架构无关问题：scope-aware 身份与原子写入、错误的实体删除式 redaction、gold 是否实际存在于作用域。

当前切片建设隔离的 EvaluationRun/EvaluationDecision 存储、immutable candidate registry 和独立 evaluation runner。Runner 不调用生产 `run_and_persist_decision()` 或 `persist_decision()`；测试只证明它不修改生产 DecisionJob、ReplyDecision、Outbox、AutomationState、HumanWorkItem 和 handoff notification 数据库表。Candidate 是受信任本地 Python 代码，本切片没有网络沙箱或可强制阻止任意 socket/httpx 调用的安全边界。

当前首切片的退出条件是 synthetic workload 的 typed local candidate 执行/持久化、lease/finalize、tenant/scenario/workload 约束和 AUTO_REPLY/DRAFT/HANDOFF × Chatwoot/direct 生产表隔离测试通过。`EvaluationRun.expires_at` 只禁止新的 reservation，不是硬取消期限；已经持有有效 claim 的 work item 可以继续 heartbeat、完成并在该时间后 finalize。`result_set_fingerprint` 是 repository 生成和复核的审计摘要，不是对 privileged database writer 不可伪造的 sealed-holdout 边界；直接数据库终态写入、单行删除和受审计 run GC 的权限隔离尚未实现。Candidate coroutine 必须保持 event-loop cooperative 且不得吞掉 cancellation；当前 heartbeat 不是 CPU-bound 或同步阻塞代码的硬 exactly-once 边界。这个切片不代表完整 Phase 1、真实数据授权、运营入口或真实 dm bake-off 已可运行。

## Phase 2：离线 bake-off

在同一个只读快照上比较：

1. 当前 OpenAI dense baseline；
2. direct BGE-M3/mE5；
3. protected query translation；
4. candidate union + reranker；
5. scope 过滤后使用本地 cross-encoder，或只对审计 Top-N 调用 LLM relevance classification。

同时执行 L0-L3 Language Policy 和 Q0-Q4 Action Policy 子系统评测，并比较 free generation + verifier、运行时受控翻译、首次生成后缓存和 derived localization。若简单的第 5 条检索路径胜出，就不采用复杂多阶段检索；若运行时翻译在安全、成本和延迟上胜出，也不预建 BrandRenderingArtifact。公开论文只决定参赛方案，真实结果决定生产架构。

退出条件是 signed acceptance contract 已存在，且有候选在 locked holdout 上同时达到安全、召回、延迟、成本和数据暴露门槛。未签署、没有赢家或 holdout 失效时，都继续 HANDOFF-only，不进入 schema 迁移。

## Phase 3：实施与赢家匹配的生产模型

选型后才实施生产 schema 和 dual-write。无论检索赢家是谁，以下数据正确性边界仍需要：稳定 KnowledgePolicy ID、不可变 PolicyRevision、PolicyRetrievalExample、Applicability、ScopedValueBinding、原子 PolicyRelease、PolicyRelease 一对一拥有的 DecisionContract、system ProviderDeploymentRevision catalog、tenant-owned TenantProviderDeploymentGrantRevision/Head 和 ModelBinding、ArtifactBuildContract/Run、EvaluationContract、ModelInvocationIntent/Event、独立 RetrievalIndex、append-only ReleaseScopeAssignmentRevision 与 active pointer、System/Tenant/Release authorization revisions 和 heads、DecisionJob release/as-of/assignment snapshot、耐久语言路由、ReplyDecision decision-time provenance、不可变 DraftApprovalAttempt、DeliveryAttempt/AuthorizationCheckEvent 和可区分 purpose 的 Outbox lineage。DecisionContract、ModelBinding、RetrievalIndex 与 artifact manifest 均经 PolicyRelease 解析，不是 Job 的独立选择键。

Phase 3 退出条件必须包含 assignment revision 可复现、上层与 release authorization revision/epoch 可审计、provider grant revoke/expiry/secret-material boundary fail-closed、grant shared/exclusive invocation lock、cross-tenant grant FK 拒绝、ModelInvocationIntent/Event 关联完整、UNKNOWN reconciliation、非幂等 UNKNOWN fail-closed、provider idempotency capability 验证、intent owner/role/attempt 和 provider key 唯一约束、禁止低层隐藏 retry、schema/timeout/grounding/embedding 每个网络 attempt 独立审计、重试不覆盖旧 attempt、client cache rotation、shared/exclusive authority lock 并发测试和 provider-I/O 原子边界验证，不能只验证 release 内容快照。

SemanticLocalization、BrandRenderingArtifact、reranker index、query-translation cache 等属于赢家专属建设，只有 bake-off 选中后才创建。迁移期间旧表和新模型双写，客户回复继续走旧的 disabled/baseline 路径；为现有知识生成旧 document ID 到 policy revision ID 的不可变映射。

退出条件是双写一致性、旧新只读结果对账、PolicyRelease published/eligible 校验、PromotionDecision/assignment revision 原子切换、provenance 完整和无需数据库 downgrade 的回滚演练全部通过。

## Phase 4：建立赢家所需的派生本地化

仅当 derived localization 在 bake-off 胜出时，按英语 PolicyRevision 和 content hash 生成常用语言产物。英语 revision、voice revision、renderer version 或 slot binding 变化时，旧 artifact 不得进入新的 PolicyRelease，也不得接受新的 Job assignment；已 pin 到旧 release 的在途 Job 仍按原 manifest 复现。若变化属于紧急安全修正，创建适用的 immutable authorization DENY revision、切换 active head 并执行幂等 HANDOFF disposition，不能动态失效单个 artifact 或修改 PolicyRelease。

简单低风险 FAQ 可以自动生成 release 外的待审 draft；条件、否定、例外、官方联系方式必须人工审核或确定性模板化。审批后的 immutable artifact 必须由新的 published/eligible PolicyRelease 固定引用，不能被既有 release 动态发现；是否获得流量只能由新的 assignment revision 决定。即使内部保留 artifact-manifest revision，也只能由新 PolicyRelease 固定引用，不能在既有 release 上切换。

Phase 4 可复用 Phase 1 的 EvaluationRun/EvaluationDecision 和生产决策/发送数据库副作用隔离机制，但接入真实数据或外部 Provider 前仍须增加数据授权、受控 extraction 和网络/provider dispatcher 边界。长尾语言首次请求只生成 release 外的本地待审记录，不直接发送；运行时只读取 `DecisionJob.policy_release_id` manifest 中列出的 artifact，禁止查询“最新 approved”。

## Phase 5：Shadow 和 Canary

按 Tenant、Brand、Platform 和语言逐步放量。先记录候选和 action，不发送；再只对内部测试账号在本系统 review UI 产生草稿，不发送 Chatwoot private note；最后按小流量 Canary 打开 Outbox。只有在业务 owner 已签署 Chatwoot private-note 例外模式时，测试阶段才可按该模式发送带 release/generation 标识的 private note。

任一错误 Outbox、错误知识命中、slot/官方联系方式 mismatch 或 policy release 混用，应触发全局或 Tenant/Brand/Platform/语言级 kill switch；异常率、检索/LLM 超时率、p95 或 Worker backlog 越界时触发自动熔断。熔断后的状态是 HANDOFF-only 或 Draft-only，不是回到旧 free-generation 自动发送路径。

### Canary observability 和晋级合同

可机器确认的安全信号包括 release/contract/index 混用、stale generation、artifact/slot/contact hash mismatch、ApprovalGuard/Final Guard failure、无效结构化输出、Provider error/timeout、Outbox/attempt 状态不一致和 Worker backlog。此类信号写入指标和结构化事件，达到 signed threshold 时在数据库创建适用的 immutable System/Tenant/Release DENY revision，原子切换 active head 并递增 epoch；Redis 只同步为急停缓存。

错误 policy、语义错误、错误 ActionOutcome、条件或例外理解错误属于需人工确认信号。系统必须提供 QA 抽样队列、客服一键“错误知识/错误动作/错误语言/不安全回复”标记、客户投诉与原 Conversation/Message/ReplyDecision/Outbox 的关联，以及从任一 Outbox 追溯 PolicyRelease、DecisionContract、PolicyRevision、artifact、slot manifest 和 generation 的界面或审计查询。

建议的默认响应合同是：SEV0 客户已收到错误政策、联系方式/PII 泄露、release 混用或错误 Outbox 时，自动或人工 kill 不超过 1 分钟，工程 on-call 5 分钟内确认，15 分钟内完成未发送 Outbox 隔离和 disposition；SEV1 guard/异常率/超时/backlog 越界时 15 分钟内确认、30 分钟内稳定；SEV2 疑似语义或 action 错误由运营/产品 30 分钟内确认，确认后立即 scope kill。最终 SLA、Pager/告警渠道、工程 on-call、产品、运营和安全 owner 必须写入 signed acceptance contract。

恢复发送必须经过事故记录、根因和影响范围确认、补偿 disposition 对账、修复后的新 PolicyRelease、相关回归和 holdout 重跑，并由工程 owner 与产品/运营 owner 双人批准。不能只清除告警或重新打开变量。

Canary 阶段按 Tenant/Brand/Platform/语言分别冻结。建议默认晋级表如下，最终数值仍需 owner 签署：

Signed acceptance contract 必须为每个 Tenant/Brand/Platform/语言 scope 冻结实际审核样本数、单侧 95% Clopper-Pearson 错误率上界、连续或分层抽样规则和最大未审核积压。晋级同时满足观察期、曝光量、已审核样本量和线上置信上界。以下是建议默认值：

| 阶段 | 最大流量 | 最短观察期和曝光 | 最低累计人工审核 | 0 错误时单侧 95% 上界 | 晋级条件 |
| --- | --- | --- | --- | --- | --- |
| C0 Shadow/Review | 0% 客户 Outbox | 至少 7 天；覆盖每种语言和 scope | 评审所有拟 AUTO_REPLY | 不用于线上错误率证明 | 无阻塞错误，offline gate 仍有效 |
| C1 | 1% | 至少 72 小时且 100 个真实 AUTO_REPLY | 100 | 约 3.0% | 仅作为回归探测；coverage/latency/cost 达标 |
| C2 | 5% | 至少 7 天且 500 个 AUTO_REPLY | 300 | 约 1.0% | 0 confirmed error，无未处置 SEV0/1 |
| C3 | 25% | 至少 7 天且 1,000 个 AUTO_REPLY | 1,000 | 约 0.3% | 0 confirmed error，所有 scope 指标达标 |
| GA | 签署范围 | C3 后至少 14 天稳定 | 3,000 | 约 0.1% | 产品、运营、工程批准并持续抽检 |

未达到最小样本、观察期或签署的线上置信上界不得晋级；低流量语言继续 Draft/Handoff。如果运营无法承担证明目标上界所需的审核量，Canary 只能被描述为回归探测，不能声称证明达到 0.1% 错误率。每次晋级、暂停、回滚和恢复都保存 immutable PromotionDecision，记录 scope、release assignment、authorization epoch、dataset/contract version、曝光与审核样本、置信上界、最大未审核积压、指标、已知事件、批准人和时间，并关联 signed acceptance contract。

Canary 前必须覆盖：draft 后到达新 inbound、approval 与 inbound/revoke 并发且无锁序死锁、旧 generation 的 approval/attempt 在 reserve fence 中主动取消、PENDING/FAILED DRAFT_APPROVAL 的最终发送复检、撤销后普通原文 approval 被拒、管理员编辑邮箱/URL/数字/locale/protected ID 时 ApprovalGuard 拦截、Chatwoot safe-default 和签署保留模式的 warning/lineage、direct approval 完整生命周期、DraftApprovalAttempt 与 Outbox 状态同步、旧 generation fencing 测试契约更新、HUMAN_REWRITE 在未 claim work item、他人已 claim、HANDOFF_PENDING、并发新 inbound 和 superadmin override 下的行为、既有 MANUAL_REPLY intent 重试仍重新鉴权、人工 override 不会重新启用 BOT、revoke 后同文/编辑后 override、并发两次 override，以及 cancelled attempt 不被新 revision 复用。

门槛同时包含：

- 错误自动回复为 0；
- 错误知识命中为 0；
- 每种语言达到最低召回；
- 不允许靠全部 HANDOFF 通过；
- p95、成本和 Worker backlog 达标。

## Phase 6：删除无收益组件

只有 bake-off 和 Canary 证明替代方案更好后，才删除或收缩：

- Lingua 大模型和复杂启发式；
- raw cosine Top1/margin 授权；
- same-model grounding verifier；
- 无收益的 lexical/RRF 分支；
- 分散的逐语言风险词表。

如果某个组件在特定语言或实体查询中仍有价值，保留为小范围 fallback，而不是全局默认。

## 回滚

首要回滚动作是在数据库创建适用的 immutable System/Tenant/Release DENY revision，原子切换 active head 并递增对应 authority epoch，停止新 claim，隔离未发送 Outbox。PolicyRelease 本身保持 immutable。每个被取消 Job 或 Outbox 都必须在 conversation delivery lock 和 generation fence 下完成幂等补偿 disposition，并事务性创建安全 HANDOFF/HumanWorkItem。不能创建第二个生产 DecisionJob，也不能修改原 Job 已 pin 的 release。补偿未成功时，RawEvent 保持 `DECISION_NEEDS_REVIEW`，不得成为 PROCESSED。

只有前一版已经通过同等验收、仍在支持期内且当前 authorization 解析为 ALLOW 的 immutable `PolicyRelease` 才能恢复自动发送。恢复由 PromotionDecision 创建并激活新的 immutable assignment revision，将 stable/canary 分桶指向该 release；不能直接切换 PolicyRelease active 状态。旧 free-generation 代码路径只可用于 shadow/baseline，不得作为自动发送回滚目标。

未批准且未被引用的 localization/rendering build cache 可以重建或 GC。已发布、被 ReplyDecision 引用或已进入 Outbox 的 artifact 不得硬删除，只能标记 `retired_at`；Decision 还应固化 source text/hash、slot manifest、renderer/voice revision，确保历史事故可重建。保留旧 document ID 到 policy revision ID 映射、审核记录和前一 corpus fingerprint。应用代码回滚不要求数据库 downgrade。

# 七、最终判断

当前方案不应该继续作为最终架构扩建。它适合作为安全 baseline 和 shadow 工具，但不适合作为长期热路径。

最值得验证的方案是：

> 不可变英语 PolicyRevision + Applicability/ScopedValueBinding + 可重建的 SemanticLocalization/BrandRenderingArtifact，配合 direct multilingual retrieval、answer-level reranker 和低置信度 query translation fallback。运行时只做 action classification 和确定性渲染。

这个方向比当前方案更有机会同时降低语言漂移、事实改写、双 LLM 延迟、维护成本和多语言规则膨胀。但在真实业务 bake-off 完成前，我只能称它为最值得验证的推荐，不能称为已经证明的最优方案。

# 来源矩阵

| 来源 | 直接证据 | 对本项目的意义 | 限制 |
| --- | --- | --- | --- |
| BGE-M3 | 100+ 语言，dense/sparse/multi-vector；MKQA cross-lingual Recall@100 75.5 | direct multilingual 与多阶段检索候选 | 模型大、索引维度不同，公开 benchmark 不是客服语料 |
| Query Translation vs CLE | BGE-M3 在 Sinhala/Tamil 英语库 Recall@15 为 96.2/95.6，高于 Google Translate 92.4/93.0 | 英语单库优先验证 direct embedding | 单一政府领域，预印本，无延迟/成本数据 |
| Cross-Lingual Cost | mixed Arabic/English 下跨语言检索显著下降，平衡检索/翻译有帮助 | mixed corpus 需要分语言或翻译策略 | 非英语单库场景，只有两种语言 |
| XRAG | 存在输出语言漂移；support documents 统一语言能改善推理 | 必须单独验证语言和证据推理 | 新闻、多跳问题，不是客服 FAQ |
| Jina reranker v3 | 二阶段 Top100 rerank；MIRACL 66.83，MKQA R@10 67.92 | 候选 union 后 rerank 值得比较 | 没有生产延迟/费用，first-stage recall 仍是上限 |
| Contextual Retrieval | 厂商自报 rerank 后 failure 5.7% -> 1.9% | reranking 可能明显提升召回/精度 | 厂商自报、不同语料和模型 |
| Generate but Verify | post-answer NLI 通常优于 pre-answer sufficiency | 专用 NLI 值得作为 verifier 候选 | 英语 factoid QA，不证明多语言政策忠实度 |
| CLIRudit | dense 无翻译很强；sparse 更受益于 document translation；QT 会破坏专名 | 不要默认运行时 query translation | 英法学术检索，查询是关键词，不是客服消息 |
| Lingua | 支持短文本但单词和混合语言更弱 | unknown/handoff 与历史 fallback 仍必要 | 170 MB wheel，真实支持流量未评测 |
| fastText lid.176 | 176 语言，ftz 917 KB | 低镜像成本候选 | 官方文档没有短消息准确率数据 |

## Further Reading

1. BGE-M3，跨语言检索模型的最佳起点：https://arxiv.org/abs/2402.03216
2. XRAG，跨语言生成与语言漂移的最佳起点：https://aclanthology.org/2025.findings-emnlp.849/
3. Cross-Lingual Cost，理解 mixed-language retrieval bias：https://aclanthology.org/2025.arabicnlp-main.6/
4. Generate but Verify，理解 post-answer verifier 与 abstention：https://aclanthology.org/2025.ijcnlp-long.56/
5. CLIRudit，理解 query/document translation 和专名风险：https://arxiv.org/abs/2504.16264

完整来源清单见 [multilingual-knowledge-replies-sources.md](multilingual-knowledge-replies-sources.md)。
