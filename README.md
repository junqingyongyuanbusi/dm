# Social Reply（多租户社媒消息中台）

这是一个直连 X、Telegram、Facebook、Instagram、WhatsApp、Feishu（飞书）和 Email 的模块化单体，使用 PostgreSQL 保存会话、消息、决策与 Transactional Outbox，并支持 AI 自动回复和本地人工接管。Chatwoot 是可选 Bridge，不是系统启动依赖。当前共 7 个直连账号平台；Email 已实现协议与控制面契约，但仓库测试不代表已使用真实邮箱凭证完成联网验证。

## 本地运行

```bash
docker compose -f deploy/docker-compose.yml up -d
uv sync --frozen --all-groups
cp .env.example .env   # 仅本地开发模板，已启用 TESTING/stub
uv run alembic upgrade head

# 本地 smoke 模式：TESTING=true 会内联 actor，并使用 stub LLM；知识检索默认关闭。
uv run uvicorn apps.api.main:app --port 8000
```

通过 `http://localhost:8000/admin` 连接平台账号。新账号默认 `BOT_DRAFT_ONLY`，平台事件经统一决策与 Outbox 链路处理。根模板只用于单进程 smoke/debug；验证真实 Redis/Dramatiq 三角色拓扑时，使用生产式配置（`TESTING=false`、真实强密钥和 OpenAI 凭证），再分别启动 `uv run dramatiq apps.worker.main` 与 `uv run python -m apps.scheduler.main`。

## 可选 Chatwoot Bridge

需要保留 Chatwoot webhook、私有备注和 Messages API 投递时：

1. 在 Chatwoot 建立 API/AgentBot inbox，获取 `api_access_token` 与 account id。
2. 为 API、Worker 和 Scheduler 同时设置：

   ```env
   CHATWOOT_ENABLED=true
   CHATWOOT_BASE_URL=https://chatwoot.example.com
   CHATWOOT_API_TOKEN=<api_access_token>
   CHATWOOT_WEBHOOK_SECRET=<强随机 webhook secret>
   ```

3. 确保 `platform_accounts.chatwoot_inbox_id` 与 `conversation_mappings` 已建立。
4. 将 Chatwoot webhook 指向 `${PUBLIC_BASE_URL}/webhooks/chatwoot`。

关闭开关后，Chatwoot 路由和补拉任务不会注册；历史 Chatwoot Outbox 会进入 `NEEDS_REVIEW/CHATWOOT_DISABLED`，不会使用占位凭证发送，并在重新启用后自动回到投递队列。

## 平台账号管理控制面（推荐）

生产账号通过 Web 管理后台连接，不再要求运营人员运行账号创建 CLI：

```text
${PUBLIC_BASE_URL}/admin
```

控制面与 webhook 数据面分离：

- Web 控制面：`/admin`，使用分组导航：运营（工作队列、对话）、内容与策略（知识库、品牌语气）、集成（平台账号、Feishu 人工通知）和系统（健康、安全控制、用户）；PostgreSQL 服务端会话，浏览器仅持有 opaque HTTP-only Cookie，并使用 CSRF 防护；
- 当前页面路径以 `/admin/content/*`、`/admin/integrations/*`、`/admin/system/*` 为主，原 `/admin/accounts`、`/admin/knowledge`、`/admin/prompt`、`/admin/health`、`/admin/users` 和 `/admin/feishu-handoff` 继续作为兼容路由；
- OAuth callback 路径保持 `/admin/oauth/*/callback`，不会随浏览器信息架构移动；
- Provisioning API：`/api/v1/platform-accounts/*`，使用独立 `CONTROL_API_KEY`，只供服务间调用；
- 数据面：`/webhooks/telegram/*`、`/webhooks/meta/*`、`/webhooks/x/*`、`/webhooks/feishu/*`；
- Durable provisioning：提交后返回 `202 + job_id`，Worker 执行平台验证/落库/Webhook 配置，Scheduler 恢复失败或中断任务；
- Secret isolation：OAuth 临时状态加密存入 Redis；平台凭证由 `PLATFORM_SECRET_KEYS` 加密后暂存于 durable ProvisioningJob，并最终写入 PostgreSQL encrypted bundle；
- 安全默认：新账号默认为 `BOT_DRAFT_ONLY`。直连平台草稿只保存在 `reply_decisions`，绝不会作为客户消息发送。

先配置：

```env
CONTROL_API_KEY=<服务间强随机值>
ADMIN_SESSION_SECRET=<至少 32 字节强随机值>
ADMIN_USERNAME=admin
ADMIN_PASSWORD=<强密码>
ADMIN_ALLOWED_TENANTS=default,tenant-a
PUBLIC_BASE_URL=https://reply.example.com
PLATFORM_SECRET_KEYS=<Fernet key；轮换时逗号分隔>
```

`ADMIN_USERNAME` / `ADMIN_PASSWORD` 是 bootstrap 超级管理员：可查看 `ADMIN_ALLOWED_TENANTS` 中全部数据，并在 `/admin/system/users`（兼容 `/admin/users`）直接创建绑定到单一 Tenant 的普通用户。普通用户首次登录必须修改初始密码，之后可在“平台账号”页自行授权和管理本 Tenant 的平台账号，但无权操作 `/admin/system/safety` 的租户总开关；系统不发送邀请或邮件。生产建议在 `/admin` 前部署 OIDC/MFA 身份感知代理。完整设计见 `docs/admin-control-plane.md`。

Provisioning API 请求使用：

```http
Authorization: Bearer <CONTROL_API_KEY>
Content-Type: application/json
```

### 连接 Telegram

```http
POST /api/v1/platform-accounts/telegram

{
  "token": "<BOT_TOKEN>",
  "tenant_id": "default",
  "brand_id": "default",
  "automation_default": "BOT_DRAFT_ONLY",
  "drop_pending_updates": false
}
```

API 立即返回 `job_id`；Worker 验证 `getMe`、幂等创建或更新账号、生成账号级 webhook secret，并调用 Telegram `setWebhook`。通过 `GET /api/v1/platform-accounts/jobs/<job_id>` 查看结果。

### 连接 Facebook Messenger、Facebook 评论与 Instagram 私信/评论

`FACEBOOK_MESSENGER_ENABLED` 与 `INSTAGRAM_MESSAGING_ENABLED` 控制 Meta 消息平台，默认开启；
需要停用时在 API、Worker、Scheduler 同时设为 `false`。Facebook Page 与 Instagram 专业账号均可
开启文本私信和公开评论回复；附件、模板和营销消息不进入自动回复。

Meta 自动外发默认关闭。所有新接入的 Meta 账号始终从 `BOT_DRAFT_ONLY` 开始，评论 capability
不会改变自动化模式。要允许管理员之后把某个 Meta 账号切到 `BOT_ACTIVE`，API、Worker、
Scheduler 必须同时设置 `META_AUTO_REPLY_ENABLED=true`。默认 `false`：

- Meta Control API 接入始终拒绝 `automation_default=BOT_ACTIVE`；新账号必须先以
  `BOT_DRAFT_ONLY` 完成接入。
- 关闭时：后台「切为自动」按钮不渲染，直接 POST 也返回 422
  `meta_requires_bot_draft_only`。已是 `BOT_ACTIVE` 的历史账号仍可改回草稿。
- 开启时：管理员可在接入完成后到 `/admin/integrations/accounts` 逐个账号点「切为自动」。每次变更写入
  `audit_logs`（`action=SET_AUTOMATION_DEFAULT`）。

Facebook 与 Instagram 评论自动回复还要求三个角色同时设置
`META_COMMENT_REPLY_ENABLED=true`。两个 Meta 开关都开启时，新授权账号默认
`enable_comments=true`、`BOT_DRAFT_ONLY`；评论只会在原评论下发送公开子评论，不生成私信回复。
由于自动化模式属于账号级，管理员后续显式切到 `BOT_ACTIVE` 时，同一账号的
Messenger/Instagram 私信也会开始自动回复。

这个开关不改变单会话控制：`/admin/conversations/{id}` 的状态翻转任何时候都可用。

Messenger 推荐从 `/admin/integrations/accounts` 使用 Facebook Login OAuth。Control API 等价请求：

```http
POST /api/v1/platform-accounts/meta

{
  "platform": "facebook",
  "external_account_id": "<PAGE_ID>",
  "access_token": "<PAGE_ACCESS_TOKEN>",
  "app_secret": "<META_APP_SECRET>",
  "app_id": "<META_APP_ID>",
  "verify_token": "<WEBHOOK_VERIFY_TOKEN>",
  "brand_id": "default",
  "enable_dm": true,
  "enable_comments": true,
  "automation_default": "BOT_DRAFT_ONLY"
}
```

Facebook 评论授权需要 `pages_read_engagement`、`pages_read_user_content` 和
`pages_manage_engagement`，且权限必须覆盖所选 Page。部署开关后，已有 Page 必须从
`/admin/integrations/accounts` 重新执行 Facebook Login 授权；系统使用 `auth_type=rerequest` 请求新增权限。
缺少权限时接入任务返回 `META_COMMENT_PERMISSION_REQUIRED`，健康状态显示 `REAUTH_REQUIRED`。

Instagram 提供两条不可混用的接入路径：

- **Facebook Login**：从 `/admin/integrations/accounts` 的 Facebook Login 卡片选择关联 Page 的 Instagram
  专业账号；保存 Page access token、IG professional account ID 和必填 `page_id`，订阅与发送使用
  Facebook Graph 路径。评论需要 `pages_read_engagement`、`instagram_manage_comments`；Page
  账号级只订阅 `messages`，评论通过 App 级 `instagram/comments` webhook 投递。Control API 传
  `instagram_login_mode=facebook_login`。
- **Instagram Login**：从独立 Instagram Login 卡片授权；保存 Instagram long-lived token 和 IG
  professional account ID，不允许 `page_id`，订阅与发送使用 Instagram Graph 的 IG 账号路径。
  评论需要 `instagram_business_manage_comments`，App 级与账号级都订阅 `comments`。Control API
  传 `instagram_login_mode=instagram_login`。

评论开关关闭时，两条路径仍默认 `enable_comments=false`、`BOT_DRAFT_ONLY`；两个 Meta 开关都
开启时，评论 capability 可默认启用，但新账号仍是 `BOT_DRAFT_ONLY`。管理员必须在接入完成后逐个
账号显式切到 `BOT_ACTIVE`，才会自动外发评论或私信。已有 Instagram token 必须从
`/admin/integrations/accounts` 按原登录路径重新授权。`meta` 与 `instagram` App family 共用
`/webhooks/meta/{app_public_id}` 路由，因此数据库会拒绝跨 family 重复的 `app_public_id`。

Meta 只在「App 级 Webhooks 产品」与「账号级订阅」都列出某个字段时才投递事件。接入与健康巡检
会同时完成两级订阅，无需在 App Dashboard 手工填回调 URL。App 级订阅以并集方式写入，不会覆盖
同一个 App 上其他账号依赖的字段。健康状态为 `APP_SUBSCRIPTION_MISSING` 时说明 App 级订阅仍未
生效，此时平台不会投递任何消息。

同一个 Meta App 下增加账号时，可传首次响应的 `app_public_id` 复用 webhook 路由。系统验证
账号 token、使用 `appsecret_proof` 调用 Graph API并自动安装 `messages` subscription。本地账号
先进入 `PROVISIONING` 以接住可能并发到达的私信 occurrence，但所有发送在 `READY` 前暂停；订阅
失败会将账号设为 `DISABLED`。Scheduler 会核对 token 和 subscription，账号页显示 `READY`、`ERROR`
或 `REAUTH_REQUIRED`。Business Verification、Advanced Access/App Review、App Live 状态、隐私
政策和数据删除流程仍需在 Meta 后台完成。普通私信发送受 24 小时消息窗口约束。

### 连接 Feishu（飞书）

Feishu 使用企业自建应用 Bot，不使用自定义群机器人 webhook。新部署先用
`FEISHU_ENABLED=false` 将同一镜像部署到 API、Worker、Scheduler，确认数据库与三个角色就绪后，
再协调重启并同时设为 `true`。然后在 `/admin/integrations/accounts` 提交 App ID、App Secret、Verification
Token 和 Encrypt Key；Control API 等价入口为
`POST /api/v1/platform-accounts/feishu`。凭证按 Tenant 暂存和落库时均由
`PLATFORM_SECRET_KEYS` 加密。

接入任务验证应用与 Bot，但不会通过 API 修改飞书开放平台的事件回调。管理员必须复制任务返回的
`${PUBLIC_BASE_URL}/webhooks/feishu/{public_id}`，在飞书开放平台手工订阅
`im.message.receive_v1`，完成 URL verification，并发布应用。账号固定从 `BOT_DRAFT_ONLY`
开始；草稿 smoke 通过后，再在 `/admin/integrations/accounts` 明确点击「切为自动」进入 `BOT_ACTIVE`。
私信和群内明确 `@Bot` 的文本消息受支持；普通群消息与 `@所有人` 不在范围内。

人工接管卡片使用独立的 `FEISHU_HANDOFF_NOTIFICATIONS_ENABLED=false` 暗发布开关。在
`/admin/integrations/feishu/handoff` 选择客服群、维护 app-scoped operator `open_id` allowlist，并把页面显示的
Card Action Callback 配置为 `card.action.trigger` 回调；测试卡片到达后，API、Worker、Scheduler
必须同时启用该开关。新的 HANDOFF 会在同一事务中留下持久化通知意图，客服可在飞书卡片认领，
并用“已回复，恢复 Bot”解决工单。该动作是客服声明；需要可验证发送证据时，应通过本地 Inbox
手动回复。完整操作步骤见 [Feishu operator runbook](docs/feishu-integration.md)。

### 连接 Email

Email 已支持管理员接入、Scheduler 只读 IMAP 轮询、统一草稿/人工审核和 SMTP 回复。新部署的
`EMAIL_ENABLED=false`、`EMAIL_AUTO_REPLY_ENABLED=false`；API、Worker、Scheduler 必须运行
同一镜像并使用完全一致的七个 Email 配置值。新账号强制从 `BOT_DRAFT_ONLY` 开始，只有总 gate、
自动回复 gate 和账号 `BOT_ACTIVE` 三者同时满足时才允许 Bot 自动外发。

IMAP 使用 readonly `SELECT` 与 `BODY.PEEK[]`；SMTP 仅允许 SSL 或严格 STARTTLS，不会降级到明文。
SMTP 端口留空时按加密方式使用 SSL 465 或 STARTTLS 587，显式合法端口保持不变。Admin 显示的
“接入探测”及探测时间仅表示最近一次凭证接入验证，不是持续监控。Endpoint 必须命中 host
allowlist，且 DNS 解析结果全部为公共目标。会话按 thread 建立并包含 sender
以避免串人；24 小时自动回复限额按 account+sender 跨 thread 统计。轮询 RawEvent 只保存 UID、
UIDVALIDITY、size 和可选 SHA-256，不保存 RFC822 正文。

当前迁移唯一 head 为 `e9a1c4f7b620`。仓库尚不声称已用真实企业邮箱完成 live E2E；管理员提供
目标凭证后，必须先做 Phase 0 TLS/login/readonly 检查，再执行 draft-only real smoke。完整步骤见
[Email operator runbook](docs/email-integration.md)。

### 连接 X

```http
POST /api/v1/platform-accounts/x

{
  "consumer_key": "<CONSUMER_KEY>",
  "consumer_secret": "<CONSUMER_SECRET>",
  "access_token": "<ACCESS_TOKEN>",
  "access_token_secret": "<ACCESS_TOKEN_SECRET>",
  "brand_id": "default",
  "automation_default": "BOT_DRAFT_ONLY"
}
```

X 使用部署级 Consumer App 和 Tenant 级共享 `webhook_url`，再按 `for_user_id` 路由到账号。旧 `environment` 字段仅作请求兼容，缺省为 `oauth`，当前 v2 运行链路不读取 Account Activity environment 名称。`X_LEGACY_DM_ENABLED=true` 时 Worker 额外读取 `/2/dm_events` 验证 Direct Messages 权限；关闭时跳过该探测并暂停 `x_dm` 发送。Legacy DM 或 XChat 任一开启时，X App 都必须授予 Direct Messages 权限。`X_ACTIVITY_ENABLED` 控制 CRC/webhook 与健康巡检。`XCHAT_ENABLED` 控制实验性的加密消息补拉、subscription 和发送，新部署建议保持 `false`，仅对少量已完成 PIN 密钥登记的账号开启。

功能开关暂停对应的实时入口、轮询、自动订阅和发送，不清除 token、游标或 XChat 私钥；重新开启后对应 Outbox 自动恢复。Legacy DM 与 XChat 已使用 PostgreSQL checkpoint、lease 和 resumable gap/backfill。X post reply 不受 Legacy DM 开关影响。

### 连接 WhatsApp Cloud API

新部署默认关闭 WhatsApp；接入前必须在三个应用角色同时设置 `WHATSAPP_ENABLED=true`。

在 `/admin` 中填写 `phone_number_id`、Meta App ID、App Secret 和 Access Token；或调用 `POST /api/v1/platform-accounts/whatsapp`。WhatsApp 与 Facebook/Instagram 共用 Meta App 级 webhook，系统根据 `phone_number_id` 动态路由到隔离账号。当前自动发送仅允许客户服务窗口内的 session text；窗口外模板消息需要在 Meta 审核后扩展 capability。

### 统一回复链路

```text
平台 Webhook
→ 验签、解密/识别账号并提交 RawEvent + versioned dispatch contract
→ PostgreSQL reservation / lease → Redis / Dramatiq durable dispatch
→ CanonicalEvent + NormalizedEvent 去重与会话隔离
→ Message + DecisionJob（同一事务）
→ Rules / RAG / LLM / Final Guard
→ ReplyDecision + Transactional Outbox（同一事务）
→ 发送前状态/capability/接管/平台健康复检
→ Telegram / Facebook / Instagram / WhatsApp / Feishu / Email / X / XChat
```

PostgreSQL 是入站证据、消息、任务、决策和 Outbox 的事实源；Redis 只承载 Dramatiq、kill switch、OAuth 临时状态和可重建缓存。Scheduler 会恢复带版本化 dispatch contract 的新 RawEvent、DecisionJob 和 Outbox；历史缺少安全重建参数的 `PENDING` RawEvent 不会被猜测执行。Worker 提交 Outbox 后仍走低延迟 Fast Path。

账号的 `PlatformAccount.automation_default` 是跨多个会话使用的账号级自动化策略；单个 `HumanWorkItem` 只影响自己的会话。转人工先进入 `WAITING/HANDOFF_PENDING`，认领会在同一锁定事务中进入 `CLAIMED/HUMAN_ACTIVE` 并取消该会话尚未发送的决策型 Bot Outbox。解决工作项会在一次操作中恢复账号当前策略（Meta 自动外发门禁不允许时安全回落到 `BOT_DRAFT_ONLY`），无需再点击恢复。等待或人工处理中收到的消息仍会持久化为 `ignore` 决策且不会在解决后补发；只有解决后的下一条新消息按恢复后的账号策略处理。其他会话不受影响。

## 文档

- [运行架构](docs/architecture.md)
- [配置参考](docs/configuration.md)
- [账号控制面](docs/admin-control-plane.md)
- [Feishu operator runbook](docs/feishu-integration.md)
- [Email operator runbook](docs/email-integration.md)
- [生产迁移](docs/production-migration.md)
- [VPS 运维](deploy/vps/README.md)
- [文档地图与历史材料](docs/README.md)

## 测试与 CI

```bash
uv sync --frozen --all-groups
uv run ruff check .
uv run pytest -m "not integration"   # 本地单元门禁，覆盖 7 个直连账号平台

docker compose -f deploy/docker-compose.yml up -d postgres redis
docker compose -f deploy/docker-compose.yml exec -T postgres sh -c \
  'psql -U dev -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = '\''social_reply_test'\''" | grep -q 1 || createdb -U dev social_reply_test'
DATABASE_URL=postgresql+asyncpg://dev:dev@localhost:5432/social_reply_test \
REDIS_URL=redis://localhost:6379/0 uv run pytest -q   # 7 个直连账号平台的全量门禁
```

GitHub Actions 在 `main` / `dev` 的 push 和 pull request 上运行三道门禁：`Ruff`、使用 pgvector PostgreSQL 17 + Redis 8 的完整 pytest，以及实际 `linux/amd64` 生产 Dockerfile 构建与镜像入口契约检查。测试 Job 会先从空库执行 `alembic upgrade head`、`alembic check`，并确认 current revision 等于唯一 head `e9a1c4f7b620`。平台专用测试文件的精确收集数以 `pytest --collect-only` 为准；跨平台断言会提供额外覆盖，但测试 stub/fake 不代表已使用生产凭证完成真实 Feishu 或 Email E2E。

## X 贴文评论自动回复

`X_PUBLIC_REPLY_ENABLED` 默认 `false`。开启后 Scheduler 会为具备 `mentions` 能力的 X 账号订阅
XAA 的 `post.mention.create`。

**X 上没有独立的「评论」实体。** 回复一条帖子就是发一条带 `replied_to` 引用的新帖，并自动带上
原作者的 @handle，因此贴文评论与 @提及是同一个信号：

```
refs=[replied_to → 你的帖子]  in_reply_to_user_id=你  mentions=[你]
text: "@yourhandle 联系方式"
```

XAA 的完整事件枚举里没有任何回复/评论专用事件（只有 `post.create`、`post.delete`、
`post.mention.create`、`like.create` 等），`post.mention.create` 是感知评论的唯一 webhook 通路。

### 会话键

`x_reply:{account}:{conversation_id}:{author_id}` —— 必须带作者。一条帖子下所有评论共享同一个
`conversation_id`，只按 thread 建键会让第一个评论者占坑（`Conversation` 只有一个 `contact_id`），
后续所有人的留言都挂到他名下，LLM 还会把陌生人的评论当上下文读。带上作者后：

- 同一人在同一帖下的多次评论 → 一个会话，有上下文
- 不同人 → 互相隔离，各自绑定自己的联系人

这也让 X 的「每次互动最多 1 条回复」正好等价于「每个会话最多 1 条外发」。

### 政策约束

| 场景 | 允许 | 说明 |
| --- | --- | --- |
| 回复"评论过你帖子"的人 | 有条件 | 用户先互动，**每次互动最多 1 条** |
| 响应求助类 @mention | 是 | 用户主动发起 |
| 按关键词自动回复任何人 | 否 | 未经邀约的骚扰 |
| **AI 生成并发布回复** | **需 X 事先批准** | 未获批部署即属违规 |

评论与私信一致，是否自动外发由账号的 `automation_default` 决定：`BOT_ACTIVE` 直接回复，
`BOT_DRAFT_ONLY` 进 `/admin/inbox?queue=drafts` 待审。要改行为就改账号配置，代码不按频道写死。

**开 `BOT_ACTIVE` 前请确认**：已取得 X 对 AI 生成回复的批准、账号资料已标注为自动账号。
另外建议同时打开 `REQUIRE_KNOWLEDGE`——知识库未命中时模型的自由发挥会直接成为公开推文。

### 其他行为

- 回复开头的 `@handle` 前缀会被剥掉再进管线（`"@you 联系方式"` → `"联系方式"`），避免污染知识检索；
  原文保留在 `raw_payload`。对方只 @ 了一下没写正文时保留原文。
- 本账号自己发的帖记 `IGNORED_SELF_MENTION`，避免自接自答。
- 未开开关时记 `IGNORED_X_PUBLIC_REPLY_DISABLED`，不建会话。
- webhook 是 App 级共享的，其他 `post.*` 事件记 `IGNORED_X_ACTIVITY_EVENT` 后丢弃。
- 发送侧复用既有 `x_post_reply`（`POST /2/tweets` + `reply.in_reply_to_tweet_id`），Guard 按 280 字限长。
- 嵌套回复（别人回复评论者、没 @ 你）不会触发事件。这与 X「仅在用户与你互动时回复」的要求一致。

## 提示词品牌表达偏好（后台可配）

`/admin/content/brand-voice` 不再接受自由文本系统指令，只允许选择 `tone`、`length`、`empathy`、`emoji` 四个有限枚举。保存后 API 同时写入规范 JSON `voice_preferences` 和代码编译的兼容 `persona` 文本；新 Worker 只读取 JSON 并编译固定英文条款，旧 Worker 在混合版本窗口也只能看到代码生成文本。

**WikiFX 身份、同语言回复策略、领域事实边界、动作含义、风险/可见性规则和安全策略不可编辑。** 这些规则与严格六字段输出 schema 由代码固定追加。数据库中的旧 `persona` 任意文本永远不会被新代码执行；缺失或畸形 JSON 会安全回落到代码编译的默认偏好。

- 作用域按 `(tenant_id, brand_id)`；未配置的租户使用规范默认值 `professional/concise/standard/never`。
- 每次保存 `revision` 自增，并写进 `reply_decisions.prompt_version`（形如 `v1-wikifx-multilingual#r7`）；审计 `SET_REPLY_PERSONA` 只记录结构化枚举，不记录任意文本或字符数。
- **试运行**使用当前保存并编译的偏好，只回显结果，不写 `reply_decisions`、不建 Outbox、不发送；客户 PII 仍先脱敏。
- `PERSONA_MAX_CHARS=4000` 仅保留为代码编译输出不变量，不是后台输入额度。
- 检索知识作为不可信 JSON 数据传给模型。只有已发布模板参与检索；官方联系方式仅在命中的已分类模板被确定性原文发送且回复与批准模板完全一致时获得 PII 例外。模型生成、复制或修改的联系方式一律转人工。

## 回复模板导入（知识库）

CSV 格式（UTF-8，表头必需 `question,reply`，可选 `brand_id,platform,category,is_official_contact`）。所有新建/导入行均为草稿，明确发布前不会参与检索：

| 列 | 必需 | 说明 |
| --- | --- | --- |
| question | 是 | 模板触发问题/关键词 |
| reply | 是 | 标准回复文本 |
| brand_id | 否 | 品牌，缺省用 `--brand`（默认 default） |
| platform | 否 | 平台，留空表示全平台 |
| category | 否 | 分类标签 |
| is_official_contact | 否 | 仅接受 true/false/1/0/yes/no（不区分大小写）；空白为 false |

```bash
uv run python -m apps.cli.import_knowledge 模板.csv --brand default
```

- 幂等：按内容 sha256（content_hash）去重，重复导入/模板未变的行自动跳过，不重复扣 embedding 费；修改模板文本后重导会追加新草稿。
- 官方联系方式分类只能在草稿状态显式更改并审计为 `SET_KNOWLEDGE_OFFICIAL_CONTACT`；先复核并分类，再发布。发布和下架分别审计为 `PUBLISH_KNOWLEDGE` / `UNPUBLISH_KNOWLEDGE`。历史状态不会由迁移自动改变。
- 未配置 `OPENAI_API_KEY` 时（非测试环境）导入会直接报错，防止误导入不可用向量；仅试跑请加 `--allow-fake`（伪向量按 version=fake-sha256 与真实向量隔离，正式检索不可用）。
- Excel 用户：请在 Excel 中「另存为 → CSV UTF-8（逗号分隔）」后再导入。
