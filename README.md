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

## 测试

```bash
uv run pytest -m "not integration"   # 纯单元
docker compose -f deploy/docker-compose.yml up -d
uv run pytest                        # 全量
```
