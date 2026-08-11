# Feishu integration operator runbook

本集成使用 **飞书企业自建应用 Bot**。它不使用「自定义机器人 / 群机器人 webhook」：后者是向群聊推送消息的出站 webhook，不提供本系统所需的入站消息订阅、签名/AES 校验、账号隔离和回复目标，因此不能替代企业自建应用。

本文只说明操作契约，不表示仓库或生产环境已经持有真实飞书凭证，也不表示真实飞书端到端测试已经完成。

## 支持范围

- 事件：`im.message.receive_v1`
- HTTP Callback：`${PUBLIC_BASE_URL}/webhooks/feishu/{public_id}`
- 私信：支持 Bot P2P 文本消息
- 群聊：只支持群内明确 `@Bot` 的文本消息
- 群普通消息、`@所有人` / group-all 监听：不支持，不应申请或配置
- 出站：回复原私信消息或原群消息；线程消息保持 thread/root 范围
- 应用模型：一个 Tenant 下一个企业自建应用 Bot 对应一个账号级 `PlatformAccount`，不创建共享 `PlatformApp`
- 人工通知：同一应用 Bot 可向一个配置的客服群发送交互卡片；客服权限使用显式 app-scoped `open_id` allowlist

飞书官方参考：

- [将事件发送至开发者服务器（Webhook 模式）](https://open.feishu.cn/document/event-subscription-guide/event-subscriptions/event-subscription-configure-/choose-a-subscription-mode/send-notifications-to-developers-server)
- [`im.message.receive_v1` 接收消息事件](https://open.feishu.cn/document/server-docs/im-v1/message/events/receive)
- [Encrypt Key 加密配置说明](https://open.feishu.cn/document/server-docs/event-subscription-guide/event-subscription-configure-/encrypt-key-encryption-configuration-case)
- [开启应用权限](https://open.feishu.cn/document/home/message-development-tutorial/turn-on-app-permissions)

## 准备飞书应用

1. 在飞书开放平台创建 **企业自建应用**，不要创建自定义群机器人。
2. 在应用能力中启用 **机器人（Bot）**，配置名称和头像。
3. 为应用申请并获批以下权限，字段必须完全对应：
   - `im:message.p2p_msg:readonly`
   - `im:message.group_at_msg:readonly`
   - `im:message:send_as_bot`
4. 在事件订阅安全设置中准备 Verification Token 和 Encrypt Key。它们必须与之后提交给 Social Reply 的值完全相同。
5. 暂不猜测 Callback URL。URL 中的账号 `public_id` 由 Social Reply provisioning 成功后生成并返回。

## 部署开关

API、Worker、Scheduler 必须运行同一个镜像 digest，并使用相同配置：

```env
FEISHU_ENABLED=true
FEISHU_HANDOFF_NOTIFICATIONS_ENABLED=false
FEISHU_HEALTH_CHECK_INTERVAL_SECONDS=600
FEISHU_HANDOFF_SWEEP_INTERVAL_SECONDS=3
FEISHU_HANDOFF_SENDER_LEASE_SECONDS=30
FEISHU_HANDOFF_MAX_ATTEMPTS=8
```

先将支持 Feishu 的镜像以 `FEISHU_ENABLED=false` 部署到三个角色，确认数据库唯一 head 为 `e9a1c4f7b620` 且旧容器全部退出；再协调重启三个角色并同时设为 `true`。人工通知保持独立的 `FEISHU_HANDOFF_NOTIFICATIONS_ENABLED=false`，直到客服群、operator allowlist、Card Action Callback 和测试卡片全部验证。不要在 API 已开启而 Worker 或 Scheduler 仍关闭/仍运行旧镜像时接入账号。

## 在 Admin 创建账号

1. 登录 `${PUBLIC_BASE_URL}/admin/accounts`。
2. 选择当前管理员有权操作的 Tenant 和 Brand，再选择 **Feishu**。
3. 填写四个必填字段：
   - App ID（`app_id`）
   - App Secret（`app_secret`）
   - Verification Token（`verification_token`）
   - Encrypt Key（`encrypt_key`）
4. 可填写账号显示名称，然后提交。Feishu 新账号只允许 `BOT_DRAFT_ONLY`，不能在 provisioning 请求中直接选择 `BOT_ACTIVE`。
5. 等待异步 ProvisioningJob 完成。任务会验证 tenant access token、Bot 信息和 Bot 激活状态。
6. 在成功结果/账号详情中复制 **Callback URL**，格式必须为：

   ```text
   ${PUBLIC_BASE_URL}/webhooks/feishu/{public_id}
   ```

服务间调用可使用 `POST /api/v1/platform-accounts/feishu`，但它同样只创建账号并返回 Callback URL 与人工步骤；**它不会调用飞书 API 配置事件回调**。

## 在飞书开放平台配置 Callback

1. 打开该企业自建应用的事件订阅页面，选择把事件发送至开发者服务器的 HTTP/Webhook 模式。
2. 粘贴 Admin 返回的完整 Callback URL，不要自行复用其他账号的 `public_id`。
3. 确认飞书控制台中的 Verification Token 和 Encrypt Key 与 Admin 提交值一致。
4. 保存 URL 并完成 URL verification。飞书发送 challenge 时，本系统会校验账号并返回 challenge；challenge 可以是明文或 AES 加密。该验证路由即使 `FEISHU_ENABLED=false` 仍保持注册。
5. 订阅事件 `im.message.receive_v1`。
6. 发布应用版本，使权限和事件订阅在目标企业生效。
7. 将应用 Bot 添加到需要测试的群。群内只有明确 `@Bot` 的消息会进入决策链；不要把自定义群机器人 webhook 当作 Callback URL。

## 配置人工接管卡片

消息接入通过后，再单独配置客服群。该功能不使用自定义群机器人 webhook，也不会把所有群成员自动授权为客服：

1. 登录 `${PUBLIC_BASE_URL}/admin/feishu-handoff`，选择已经 active 且健康为 `READY` 的 Feishu 账号。
2. 填写客服群的 `chat_id` 并保存路由。一个 Tenant 第一版只配置一个通知账号和一个客服群。
3. 逐个添加客服的 app-scoped `open_id`，分别授予认领和解决权限。只在群里并不代表有权限。
4. 复制页面显示的 Card Action Callback：

   ```text
   ${PUBLIC_BASE_URL}/webhooks/feishu/{public_id}/card-actions
   ```

5. 在飞书开放平台单独配置 `card.action.trigger` 交互卡片回调，完成 URL verification，并发布应用版本。它与 `im.message.receive_v1` 事件订阅是两个控制台能力，即使 URL 同源也必须分别确认。
6. 把 Bot 加入客服群，点击一次“发送测试卡片”。测试卡片不创建工单，也不包含客户数据。若页面显示结果未知，先检查客服群，不能立即重复点击。
7. 测试卡片到达后，在 API、Worker、Scheduler 同时设置 `FEISHU_HANDOFF_NOTIFICATIONS_ENABLED=true` 并协调重启到同一个 digest。
8. 触发一条受控 HANDOFF，确认只出现一张卡片；授权客服可认领，非认领人不能解决，认领人点击“已回复，恢复 Bot”后，下一条新客户消息按账号当前 automation policy 处理。

卡片中的消息摘要经过长度限制和联系方式脱敏。解决动作记录
`FEISHU_OPERATOR_ATTESTED`，代表客服声明已经在原社交平台回复；Reply Core 无法验证外部
手工发送。需要可验证证据时，应从 `/admin/inbox` 发送 manual reply，使 Outbox 和 outbound
Message 保留在 PostgreSQL。

## Draft-first smoke checklist

账号保持 `BOT_DRAFT_ONLY`，按顺序验证：

- `/admin/accounts` 显示账号为 active，健康状态最终为 `READY`；
- URL verification 成功，飞书控制台不再提示 Callback 校验失败；
- 向 Bot 发送一条 P2P 文本，Admin 草稿队列出现对应 Tenant/账号/会话的草稿；
- 在已添加 Bot 的群中明确 `@Bot` 发送文本，出现独立的群会话草稿；
- 在同一群由不同用户发送 `@Bot`，以及在线程中发送 `@Bot`，确认会话没有跨用户或跨 thread/root 混合；
- 发送不含 `@Bot` 的普通群消息，确认不会生成自动回复；
- 审批一条测试草稿，确认回复落在原消息目标，且没有重复发送；
- 检查 Admin health、RawEvent/DecisionJob/Outbox backlog 和 delivery exception 队列均无异常。

这些步骤需要真实测试应用和测试租户。没有这些凭证时，只能完成自动化测试和配置审查，不能宣称真实 Feishu E2E 已通过。

## 明确切换为 `BOT_ACTIVE`

只有 draft smoke 和 provider-side 收发验证完成后才允许自动外发：

1. 在 `/admin/accounts` 找到目标 Tenant 的 Feishu 账号。
2. 点击该账号的「切为自动」操作。
3. 确认账号 `automation_default` 变为 `BOT_ACTIVE`，并检查 `audit_logs` 中存在 `SET_AUTOMATION_DEFAULT`。
4. 再发送一条 P2P 和一条群 `@Bot` 测试消息，确认自动回复目标正确。

此操作是逐账号、显式且可审计的；开启 `FEISHU_ENABLED` 本身不会把草稿账号自动提升为 `BOT_ACTIVE`。

## 安全与回调语义

正常消息必须使用加密 envelope，并通过 `X-Lark-*` 签名、时间窗口、Verification Token、App ID 和 AES 解密检查，同时提供非空 `header.event_id` 和有效的 `event.message.create_time`。失效、重放、签名错误或账号不匹配的请求不会创建 `RawEvent`。同一账号的已验证回调按 `header.event_id` 在 RawEvent ingress 去重；该键不是 `message_id`，并且在 `FEISHU_ENABLED=false` 时同样生效。`message.create_time` 是同会话 provider 顺序的事实时间，header `create_time` 只保留为元数据；严格更旧的延迟消息只保存带 `stale_provider_order` disposition 的 NormalizedEvent，不创建 Message、generation、DecisionJob 或 Outbox。已验证的正常事件在功能关闭时仍返回 ACK，但只保存去除敏感 token 的 sanitized `IGNORED_AT_INGRESS` 证据，不会进入决策或发送。

交互卡片动作重复使用同一套签名、时间窗口、Verification Token、App ID、AES 和 body-size
校验，但用 `header.event_id` 写入独立的持久化 callback receipt。重复请求返回第一次已保存的
响应，不重复认领或解决。动作还必须匹配通知 public ID、卡片 revision、work version、nonce、
Tenant、Feishu app 和 operator allowlist。三秒回调路径只执行短 PostgreSQL 事务，不调用 Feishu、
Redis 或 LLM；卡片更新由 Worker 异步完成。

**Secret warning:** App Secret、Verification Token 和 Encrypt Key 都按生产密钥处理。只通过 TLS Admin/Control API 提交；不要写入文档、工单、聊天、截图、shell history、Git、日志或飞书消息。Social Reply 使用 `PLATFORM_SECRET_KEYS` 加密 durable staging 和最终 PostgreSQL credential bundle；丢失该密钥集会使已有账号凭证不可读。

## Provider 与错误处理

- Provisioning 会验证 token、Bot 身份和 Bot 激活状态；未激活返回 `FEISHU_BOT_NOT_ACTIVATED`，需要操作员在飞书侧处理。
- 飞书 API 业务错误保留为 sanitized `FEISHU_API_<code>`；网络/超时按可重试 `PLATFORM_UNAVAILABLE` 处理，重试耗尽后进入 `NEEDS_ACTION/RETRY_EXHAUSTED`。
- Health 状态包括 `READY`、`BOT_NOT_ACTIVE`、`BOT_ID_MISMATCH`、`CREDENTIAL_INVALID` 和 `ERROR`。非 `READY` 时发送暂停为 `FEISHU_ACCOUNT_NOT_READY`，不消耗发送尝试。
- 每个 Outbox 的 UUID 作为 Feishu `uuid` 发送。provider code `99991663` 会触发一次 tenant token 刷新，并使用同一 UUID 重试。
- HTTP 429 / provider rate-limit 可重试；明确的 provider 4xx 拒绝进入 `NEEDS_REVIEW` 并保留 sanitized code。
- connect error/connect timeout 可安全重试；read timeout、未知 post-dispatch 错误、响应格式异常或 provider 5xx 视为 `NEEDS_REVIEW/AMBIGUOUS_SEND`，不会盲目重试造成重复回复。
- `FEISHU_ENABLED=false` 时发送暂停为 `NEEDS_REVIEW/FEISHU_DISABLED`，不消耗尝试；协调重新启用后由 durable sweep 恢复。
- HANDOFF 即使缺少客服群配置也保持成功，同时创建 `BLOCKED_CONFIG` 通知；补齐配置后 Scheduler 可恢复尚未解决的工单。
- 卡片 create 使用稳定 UUID，但 Feishu 去重窗口只有一小时。create 的未知 post-dispatch 结果进入 `NEEDS_REVIEW/AMBIGUOUS_CARD_CREATE`，不会在窗口外盲目重试；已有 provider message ID 的 PATCH update 可以安全恢复。

## Pause and rollback

只暂停人工通知时，在 API、Worker、Scheduler 上同时设置
`FEISHU_HANDOFF_NOTIFICATIONS_ENABLED=false` 并协调重启到同一 digest。现有客户 HANDOFF 和
本地 Admin Inbox 继续工作；卡片创建、更新和动作 mutation 停止，通知意图与 receipt 保留。

暂停整个 Feishu 平台时，在三个角色上同时设置 `FEISHU_ENABLED=false`。该操作暂停
provisioning、正常事件 dispatch、health、客户发送和人工卡片，但保留账号、加密凭证、Callback
身份、RawEvent、Outbox 和通知意图；URL verification 仍可用。不要通过删除凭证实现临时暂停。

代码回滚必须同时考虑数据库：优先保留 additive `b7e4c2d9a615` schema 并只回滚镜像和关闭人工
通知。Downgrade 到 `d3f6a1b8c904` 会删除通知路由、operator、intent、receipt 和 HumanWorkItem
resolution evidence，必须先导出。继续从 `d3f6a1b8c904` downgrade 到 `f8a1c3d5e702` 是不可逆的
人工作业生命周期数据 no-op；再 downgrade 到 `e4b7c2d9a610` 才会移除 Feishu RawEvent 去重索引；
继续 downgrade `e4b7c2d9a610` 时，任何 Feishu `PlatformAccount` 存在都会被拒绝。若必须回到不识别
Feishu 的旧代码，先保持 Feishu 禁用并按 `docs/production-migration.md` 执行已审核的账号移除/重建
或数据库备份恢复方案。镜像回滚不等于数据库回滚，API、Worker、Scheduler 最终仍必须收敛到同一 digest。
