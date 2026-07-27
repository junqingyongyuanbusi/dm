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

### 连接 Facebook Messenger / Instagram 私信

新部署默认关闭 Meta 消息平台。接入前必须在 API、Worker、Scheduler 同时显式设置
`FACEBOOK_MESSENGER_ENABLED=true` 或 `INSTAGRAM_MESSAGING_ENABLED=true`。当前发布范围是
专业账号文本私信，默认 `BOT_DRAFT_ONLY`；评论、附件、模板和营销消息不进入自动回复。

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
  "enable_comments": false,
  "automation_default": "BOT_DRAFT_ONLY"
}
```

Instagram 提供两条不可混用的接入路径：

- **Facebook Login**：从 `/admin/accounts` 的 Facebook Login 卡片选择关联 Page 的 Instagram
  专业账号；保存 Page access token、IG professional account ID 和必填 `page_id`，订阅与发送使用
  Facebook Graph 的 Page 路径。Control API 传 `instagram_login_mode=facebook_login`。
- **Instagram Login**：从独立 Instagram Login 卡片授权；保存 Instagram long-lived token 和 IG
  professional account ID，不允许 `page_id`，订阅与发送使用 Instagram Graph 的 IG 账号路径。
  Control API 传 `instagram_login_mode=instagram_login`。

两条路径都固定 `enable_dm=true`、`enable_comments=false` 和 `BOT_DRAFT_ONLY`。`meta` 与
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
