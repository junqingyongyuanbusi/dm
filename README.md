# Reply Core（社媒自动回复核心）

架构见 `PLAN.md`。当前进度：Phase 1 / Plan 1 —— Chatwoot 事件入站链路。

## 本地运行

```bash
docker compose -f deploy/docker-compose.yml up -d
uv sync
cp .env.example .env
uv run alembic upgrade head

# 准备一个测试账号（chatwoot_inbox_id=101）
docker compose -f deploy/docker-compose.yml exec postgres psql -U dev -d social_reply -c \
  "INSERT INTO platform_accounts (id, tenant_id, brand_id, platform, name, chatwoot_inbox_id, automation_default, status, created_at)
   VALUES (gen_random_uuid(), 'default', 'b1', 'telegram', 'tg-main', 101, 'BOT_DRAFT_ONLY', 'CONNECTED', now());"

# 终端 1：API
uv run uvicorn apps.api.main:app --port 8000
# 终端 2：worker
uv run dramatiq apps.worker.main
# 终端 3：冒烟
uv run python scripts/send_test_webhook.py
```

验收：`raw_events.processing_status='PROCESSED'`，`conversations`/`messages`/`automation_states`（BOT_DRAFT_ONLY）各一行。

## Plan 2b：真实 Chatwoot 发送（需自托管 Chatwoot 凭证）

1. 在 Chatwoot 建一个 API/AgentBot inbox，取 `api_access_token` 与 account id。
2. `.env` 设：`CHATWOOT_BASE_URL` / `CHATWOOT_API_TOKEN` / `TESTING=false` / `CHATWOOT_WEBHOOK_SECRET=<真实密钥>`。
3. 确保 `platform_accounts.chatwoot_inbox_id` 与 `conversation_mappings` 已建立（首条入站消息会自动建 mapping）。
4. 起三个常驻进程：

   ```bash
   uv run uvicorn apps.api.main:app --port 8000   # API（webhook 入口）
   uv run dramatiq apps.worker.main               # worker（入站处理 + outbox 投递）
   uv run python -m apps.scheduler.main           # scheduler（outbox 补扫，30s 一轮）
   ```

5. Chatwoot webhook 指向 `http(s)://<host>:8000/webhooks/chatwoot`。用户发消息 → 决策 → BOT_ACTIVE 下自动回复经 Chatwoot Messages API 发出。

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
- `TESTING=true` 或未配置 `OPENAI_API_KEY` 时自动使用确定性伪向量（FakeEmbeddingClient）并打印提示，方便无凭证试导入。
- Excel 用户：请在 Excel 中「另存为 → CSV UTF-8（逗号分隔）」后再导入。
