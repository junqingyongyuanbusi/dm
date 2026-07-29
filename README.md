# Social Reply（多租户社媒消息中台）

这是一个直连 X、Telegram、Meta 和 WhatsApp 的模块化单体，使用 PostgreSQL 保存会话、消息、决策与 Transactional Outbox，并支持 AI 自动回复和本地人工接管。Chatwoot 是可选 Bridge，不是系统启动依赖。

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

- Web 控制面：`/admin`，PostgreSQL 服务端会话（浏览器仅持有 opaque HTTP-only Cookie）、CSRF 防护；
- Provisioning API：`/api/v1/platform-accounts/*`，使用独立 `CONTROL_API_KEY`，只供服务间调用；
- 数据面：`/webhooks/telegram/*`、`/webhooks/meta/*`、`/webhooks/x/*`；
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

`ADMIN_USERNAME` / `ADMIN_PASSWORD` 是 bootstrap 超级管理员：可查看 `ADMIN_ALLOWED_TENANTS` 中全部数据，并在 `/admin/users` 直接创建绑定到单一 Tenant 的普通用户。普通用户首次登录必须修改初始密码，之后可在“账号”页自行授权和管理本 Tenant 的平台账号，但无权操作租户总开关；系统不发送邀请或邮件。生产建议在 `/admin` 前部署 OIDC/MFA 身份感知代理。完整设计见 `docs/admin-control-plane.md`。

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

### 连接 Facebook Messenger、Facebook 评论与 Instagram 私信

`FACEBOOK_MESSENGER_ENABLED` 与 `INSTAGRAM_MESSAGING_ENABLED` 控制 Meta 消息平台，默认开启；
需要停用时在 API、Worker、Scheduler 同时设为 `false`。Instagram 当前仍仅支持专业账号文本私信；
Facebook Page 可选择开启 Messenger 私信与公开评论回复。附件、模板和营销消息不进入自动回复。

Meta 自动外发默认关闭。要允许某个 Meta 账号使用 `BOT_ACTIVE`，API、Worker、Scheduler 必须同时
设置 `META_AUTO_REPLY_ENABLED=true`。默认 `false`：

- 关闭时：Control API 传 `automation_default=BOT_ACTIVE` 返回 422，后台「切为自动」按钮不渲染，
  直接 POST 也返回 422 `meta_requires_bot_draft_only`。已是 `BOT_ACTIVE` 的历史账号仍可改回草稿。
- 开启时：以上闸门放行；普通 Meta/Instagram 接入仍默认 `BOT_DRAFT_ONLY`，可在
  `/admin/accounts` 逐个账号点「切为自动」。每次变更写入 `audit_logs`
  （`action=SET_AUTOMATION_DEFAULT`）。

Facebook Page 评论自动回复还要求三个角色同时设置 `META_COMMENT_REPLY_ENABLED=true`。两个 Meta
开关都开启时，新授权的 Facebook Page 默认 `enable_comments=true`、`BOT_ACTIVE`，并订阅 `feed`；
评论只会在原评论下发送公开子评论，不生成私信回复。由于自动化模式属于账号级，同一 Page 的
Messenger 私信也会自动回复。Instagram 仍固定评论关闭并默认草稿。

这个开关不改变单会话控制：`/admin/conversations/{id}` 的状态翻转任何时候都可用。

Messenger 推荐从 `/admin/accounts` 使用 Facebook Login OAuth。Control API 等价请求：

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
  "automation_default": "BOT_ACTIVE"
}
```

Facebook 评论授权需要 `pages_read_engagement`、`pages_read_user_content` 和
`pages_manage_engagement`，且权限必须覆盖所选 Page。部署开关后，已有 Page 必须从
`/admin/accounts` 重新执行 Facebook Login 授权；系统使用 `auth_type=rerequest` 请求新增权限。
缺少权限时接入任务返回 `META_COMMENT_PERMISSION_REQUIRED`，健康状态显示 `REAUTH_REQUIRED`。

Instagram 提供两条不可混用的接入路径：

- **Facebook Login**：从 `/admin/accounts` 的 Facebook Login 卡片选择关联 Page 的 Instagram
  专业账号；保存 Page access token、IG professional account ID 和必填 `page_id`，订阅与发送使用
  Facebook Graph 的 Page 路径。Control API 传 `instagram_login_mode=facebook_login`。
- **Instagram Login**：从独立 Instagram Login 卡片授权；保存 Instagram long-lived token 和 IG
  professional account ID，不允许 `page_id`，订阅与发送使用 Instagram Graph 的 IG 账号路径。
  Control API 传 `instagram_login_mode=instagram_login`。

Instagram 两条路径都固定 `enable_dm=true`、`enable_comments=false` 和 `BOT_DRAFT_ONLY`。`meta` 与
`instagram` App family 共用 `/webhooks/meta/{app_public_id}` 路由，因此数据库会拒绝跨 family
重复的 `app_public_id`。

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
→ 验签并提交 RawEvent + versioned dispatch contract
→ PostgreSQL reservation / lease → Redis / Dramatiq dispatch
→ CanonicalEvent + NormalizedEvent 去重
→ Message + DecisionJob（同一事务）
→ Rules / RAG / LLM / Final Guard
→ ReplyDecision + Transactional Outbox（同一事务）
→ 发送前状态/capability/接管复检
→ Telegram / Facebook / Instagram / WhatsApp / X / XChat
```

PostgreSQL 是入站证据、消息、任务、决策和 Outbox 的事实源；Redis 只承载 Dramatiq、kill switch、OAuth 临时状态和可重建缓存。Scheduler 会恢复带版本化 dispatch contract 的新 RawEvent、DecisionJob 和 Outbox；历史缺少安全重建参数的 `PENDING` RawEvent 不会被猜测执行。Worker 提交 Outbox 后仍走低延迟 Fast Path。

## 文档

- [运行架构](docs/architecture.md)
- [配置参考](docs/configuration.md)
- [账号控制面](docs/admin-control-plane.md)
- [生产迁移](docs/production-migration.md)
- [VPS 运维](deploy/vps/README.md)
- [文档地图与历史材料](docs/README.md)

## 测试与 CI

```bash
uv sync --frozen --all-groups
uv run ruff check .
uv run pytest -m "not integration"   # 纯单元

docker compose -f deploy/docker-compose.yml up -d postgres redis
docker compose -f deploy/docker-compose.yml exec -T postgres sh -c \
  'psql -U dev -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = '\''social_reply_test'\''" | grep -q 1 || createdb -U dev social_reply_test'
DATABASE_URL=postgresql+asyncpg://dev:dev@localhost:5432/social_reply_test \
REDIS_URL=redis://localhost:6379/0 uv run pytest -q   # 全量
```

GitHub Actions 在 `main` / `dev` 的 push 和 pull request 上运行两道门禁：`Ruff`，以及使用 pgvector PostgreSQL 17 + Redis 8 的完整 pytest。测试 Job 会先从空库执行 `alembic upgrade head`、`alembic check`，并确认 current revision 等于唯一 head。

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
`BOT_DRAFT_ONLY` 进 `/admin/decisions` 待审。要改行为就改账号配置，代码不按频道写死。

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

## 提示词人设（后台可配）

`/admin/prompt` 编辑 LLM 人设段（语言、语气、身份），保存后**下一条决策立即生效**，无需重启或发版。

人设存 PostgreSQL 而非环境变量：Worker 跑决策、API 跑后台，两个进程必须看到同一份内容；
每次决策直读也免去多 Worker 的缓存失效问题。

**只有人设段可编辑。** 动作语义（何时 auto_reply / handoff / draft / ignore）与安全不变量
（不回显 PII、防提示词注入、`handoff/ignore 时 reply_text 置空`）由代码固定追加，页面上以只读形式
展示。这些内容是结构化输出契约的一部分——删掉任何一行都会静默废掉防注入，或让 `json_schema`
校验开始失败（决策全部降级转人工）。

- 作用域按 `(tenant_id, brand_id)`；未配置的租户回落到代码内置默认人设。
- 每次保存 `revision` 自增，并写进 `reply_decisions.prompt_version`（形如 `v0-stub#r7`），
  可回溯某条回复出自哪一版人设。变更记入 `audit_logs`（`action=SET_REPLY_PERSONA`）。
- **试运行**:页面内可用当前人设跑一次真实 LLM 调用，只回显 action/回复/置信度，
  不写 `reply_decisions`、不建 outbox、不发送。发给模型前同样做 PII 脱敏。
- 知识库精确命中并原文直答时不经过 LLM，因此也不受人设影响；人设只作用于需要模型生成的回复。

## 回复模板导入（知识库）

CSV 格式（UTF-8，表头必需 `question,reply`，可选 `brand_id,platform,category`）：

| 列 | 必需 | 说明 |
| --- | --- | --- |
| question | 是 | 模板触发问题/关键词 |
| reply | 是 | 标准回复文本 |
| brand_id | 否 | 品牌，缺省用 `--brand`（默认 default） |
| platform | 否 | 平台，留空表示全平台 |
| category | 否 | 分类标签 |

```bash
uv run python -m apps.cli.import_knowledge 模板.csv --brand default
```

- 幂等：按内容 sha256（content_hash）去重，重复导入/模板未变的行自动跳过，不重复扣 embedding 费；修改模板文本后重导会追加新记录。
- 未配置 `OPENAI_API_KEY` 时（非测试环境）导入会直接报错，防止误导入不可用向量；仅试跑请加 `--allow-fake`（伪向量按 version=fake-sha256 与真实向量隔离，正式检索不可用）。
- Excel 用户：请在 Excel 中「另存为 → CSV UTF-8（逗号分隔）」后再导入。
