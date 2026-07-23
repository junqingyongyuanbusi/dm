# Social Reply（多租户社媒消息中台）

这是一个直连 X、Telegram、Meta 和 WhatsApp 的模块化单体，使用 PostgreSQL 保存会话、消息、决策与 Transactional Outbox，并支持 AI 自动回复和本地人工接管。Chatwoot 是可选 Bridge，不是系统启动依赖。

## 本地运行

```bash
docker compose -f deploy/docker-compose.yml up -d
uv sync
cp .env.example .env
# 直连模式默认关闭 Chatwoot，无需配置 Chatwoot secret/token。
# CHATWOOT_ENABLED=false
uv run alembic upgrade head

# 终端 1：API
uv run uvicorn apps.api.main:app --port 8000
# 终端 2：worker
uv run dramatiq apps.worker.main
# 终端 3：scheduler
uv run python -m apps.scheduler.main
```

通过 `http://localhost:8000/admin` 连接平台账号。新账号默认 `BOT_DRAFT_ONLY`，平台事件经统一决策与 Outbox 链路处理。

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
4. 将 Chatwoot webhook 指向 `https://<PUBLIC_BASE_URL>/webhooks/chatwoot`。

关闭开关后，Chatwoot 路由和补拉任务不会注册；历史 Chatwoot Outbox 会进入 `NEEDS_REVIEW/CHATWOOT_DISABLED`，不会使用占位凭证发送，并在重新启用后自动回到投递队列。

## 平台账号管理控制面（推荐）

生产账号通过 Web 管理后台连接，不再要求运营人员运行账号创建 CLI：

```text
https://<PUBLIC_BASE_URL>/admin
```

控制面与 webhook 数据面分离：

- Web 控制面：`/admin`，PostgreSQL 服务端会话（浏览器仅持有 opaque HTTP-only Cookie）、CSRF 防护；
- Provisioning API：`/api/v1/platform-accounts/*`，使用独立 `CONTROL_API_KEY`，只供服务间调用；
- 数据面：`/webhooks/telegram/*`、`/webhooks/meta/*`、`/webhooks/x/*`；
- Durable provisioning：提交后返回 `202 + job_id`，Worker 执行平台验证/落库/Webhook 配置，Scheduler 恢复失败或中断任务；
- Secret isolation：Token 只进入 SecretStore，PostgreSQL 的 `provisioning_jobs` 只保存引用和脱敏结果；
- 安全默认：新账号默认为 `BOT_DRAFT_ONLY`。直连平台草稿只保存在 `reply_decisions`，绝不会作为客户消息发送。

先配置：

```env
CONTROL_API_KEY=<服务间强随机值>
ADMIN_SESSION_SECRET=<至少 32 字节强随机值>
ADMIN_USERNAME=admin
ADMIN_PASSWORD=<强密码>
ADMIN_ALLOWED_TENANTS=default,tenant-a
PUBLIC_BASE_URL=https://reply.example.com
ACCOUNT_SECRETS_ROOT=/secure/social-reply/accounts
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

### 连接 Facebook / Instagram

```http
POST /api/v1/platform-accounts/meta

{
  "platform": "instagram",
  "external_account_id": "<IG_ACCOUNT_ID>",
  "access_token": "<LONG_LIVED_ACCESS_TOKEN>",
  "app_secret": "<META_APP_SECRET>",
  "app_id": "<META_APP_ID>",
  "brand_id": "default",
  "enable_dm": true,
  "enable_comments": true,
  "automation_default": "BOT_DRAFT_ONLY"
}
```

同一个 Meta App 下继续增加账号时，可传首次响应的 `app_public_id` 复用 webhook 路由。响应会返回 `webhook_url`；`verify_token` 只存入 Secret Store，不进入 ProvisioningJob、API 或管理页。首次接入请在表单中自行指定并同时配置到 Meta Dashboard。Meta Dashboard 中的字段订阅、Business Verification、Advanced Access/App Review 仍需人工完成。普通自动回复受 24 小时窗口约束，评论私密回复还有次数与期限限制。

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

功能开关暂停对应的实时入口、自动订阅和发送，不清除 token、游标或 XChat 私钥；重新开启后对应 Outbox 自动恢复。在 durable checkpoint/backfill 完成前，已有且已验证能力的账号仍保留低频 reconciliation，避免长时间停用放大平台历史窗口缺口。X post reply 不受 Legacy DM 开关影响。

### 连接 WhatsApp Cloud API

在 `/admin` 中填写 `phone_number_id`、Meta App ID、App Secret 和 Access Token；或调用 `POST /api/v1/platform-accounts/whatsapp`。WhatsApp 与 Facebook/Instagram 共用 Meta App 级 webhook，系统根据 `phone_number_id` 动态路由到隔离账号。当前自动发送仅允许客户服务窗口内的 session text；窗口外模板消息需要在 Meta 审核后扩展 capability。

### 统一回复链路

```text
平台 Webhook
→ 账号级签名校验
→ CanonicalEvent
→ ingest_canonical_event
→ 统一规则 / RAG / LLM / Final Guard
→ Transactional Outbox
→ 账号级 Sender
→ Telegram / Facebook / Instagram / X API
```

## 测试

```bash
uv run pytest -m "not integration"   # 纯单元
docker compose -f deploy/docker-compose.yml up -d
uv run pytest                        # 全量
```

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
