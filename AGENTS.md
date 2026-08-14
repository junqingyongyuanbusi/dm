# 仓库工作契约

## 项目事实

- 只在 `dev` 分支工作，除非用户明确指定其他分支；不得隐式修改或部署 `main`。
- 保留与当前任务无关的用户改动。禁止用 reset、checkout、restore、clean 或批量暂存覆盖、恢复、删除这些改动。
- 本项目是 Python 3.13 的 FastAPI 模块化单体。API、Worker、Scheduler 使用同一个生产镜像，由必填的 `SERVICE_ROLE=api|worker|scheduler` 选择进程角色；缺少或未知角色必须在启动前失败。
- PostgreSQL 是账号、凭证信封、入站证据、会话、任务、决策、Outbox、审计和恢复状态的持久事实源。Redis 只承载 Dramatiq、kill switch、OAuth 临时状态及可重建缓存，不得成为业务事实源。
- API 拥有 HTTP 路由、Admin、OAuth callback、Provisioning API、Webhook ingress 和数据库准备；Worker 拥有异步处理与发送；Scheduler 拥有周期恢复、轮询和巡检。Worker 与 Scheduler 不运行 HTTP 服务，也不执行迁移。
- API、Worker、Scheduler 必须共享 PostgreSQL、Redis、`PLATFORM_SECRET_KEYS`、安全配置和功能开关，并与 PostgreSQL、Redis 部署在同一基础设施区域。PostgreSQL 与 Redis 只能使用私有网络端点；只有 API 可以接收公网 ingress，Worker 与 Scheduler 不得暴露公网服务。
- 新平台账号默认 `BOT_DRAFT_ONLY`。不得为了通过测试或加速上线而放宽 CSRF、OAuth state、Webhook 验签、租户隔离、kill switch、发送前复检、幂等或 Outbox 约束。
- `.env.example` 仅用于本地单进程 smoke。生产配置的事实源是 Railway 服务变量，并由 `scripts/validate_railway_config.py` 在每次发布前验证；不得把生产 secret 写进仓库、日志、测试快照或临时文件。
- 当前架构和配置权威文档是 `docs/architecture.md` 与 `docs/configuration.md`；数据库和协调发布要求见 `docs/production-migration.md`。`docs/superpowers/plans/` 只保存历史计划，不是运行时契约。
- 生产目标固定为：
  - Docker Hub：`zhiyangxiaozi/reply-core`
  - Railway 项目：`reply-core`
  - Railway 环境：`production`
  - Railway 服务：`api`、`worker`、`scheduler`
  - 公网地址：`https://relay.nexory.top`

## 项目结构

- `apps/api/main.py`：FastAPI 应用入口。
- `apps/worker/main.py`：Dramatiq Worker 入口。
- `apps/scheduler/main.py`：Scheduler 入口。
- `apps/cli/`：人工执行的维护和导入命令，不属于容器默认启动路径。
- `src/social_reply/application/`：用例编排，包括账号管理、事件摄取、人工接管、知识库、投递与决策。
- `src/social_reply/domain/`：领域模型、状态和不变量；不得依赖 FastAPI、Dramatiq 或具体平台 SDK。
- `src/social_reply/connectors/`：Chatwoot、Email、Feishu、Meta、Telegram、WhatsApp、X、XChat 等外部边界。
- `src/social_reply/infrastructure/`：PostgreSQL、Redis、队列、锁和持久化实现。
- `src/social_reply/shared/`：跨模块配置和共享基础设施。
- `migrations/`：Alembic 迁移。迁移图必须保持唯一 head；禁止改写已发布迁移来伪造兼容。
- `scripts/prepare_database.py`：API 启动时在 PostgreSQL advisory lock 内执行数据库准备。
- `scripts/assert_database_ready.py`：Worker/Scheduler 启动前验证 schema 与加密凭证可读性。
- `scripts/validate_railway_config.py`：发布前验证三角色真实 Pydantic Settings 和跨角色一致性。
- `scripts/publish_railway_release.sh`：唯一默认生产发布入口。
- `tests/unit/`：无需真实外部服务的单元和契约测试。
- `tests/integration/`：依赖本地 PostgreSQL/Redis 或跨模块边界的测试。
- `deploy/docker-compose.yml`：仅用于本地 PostgreSQL/Redis 开发与测试，不是生产编排文件。
- `Dockerfile`、`entrypoint.sh`：三角色共享的 production image 与 fail-closed 启动契约。
- `.github/workflows/ci.yml`：Ruff、迁移/全量 pytest、真实 `linux/amd64` production image 三道 CI 门禁。
- `.pi/`：项目级 Pi 工程目录。Pi 识别的 Prompts、Skills、Extensions、Themes、Settings 和 System Prompt 文件受 Project Trust 加载门控制；任意普通文件并不会因此获得保护。根目录 `AGENTS.md` 仍是项目事实与硬约束的唯一契约。不得把 personal model、凭据、本机绝对路径、Session transcript 或未经审查的 Extension/Package 写入该目录。

## 实际安装命令

依赖安装必须使用锁文件。不要创建或维护并行的 `requirements.txt`，也不要用非锁定的 `pip install` 替代仓库流程。

```bash
uv sync --frozen --all-groups
```

首次本地 smoke 可准备本地配置和基础设施：

```bash
[ -f .env ] || cp .env.example .env
docker compose -f deploy/docker-compose.yml up -d postgres redis
uv run alembic upgrade head
uv run uvicorn apps.api.main:app --port 8000
```

`.env.example` 使用 `TESTING=true`、StubBroker 和 stub LLM，只证明单进程 smoke；它不证明生产 Redis/Dramatiq 三角色拓扑、平台凭证或真实外部 API 可用。

只需要停止本地基础设施时：

```bash
docker compose -f deploy/docker-compose.yml down
```

除非用户明确要求清空本地数据，不得添加 `-v`，不得删除 PostgreSQL volume。

## 实际测试命令

开始前先确认分支和工作区，识别并保留无关用户改动：

```bash
git branch --show-current
git status --short
```

先运行与修改直接相关的 focused tests，例如：

```bash
uv run pytest -q tests/unit/test_config.py
uv run pytest -q tests/integration/test_admin_console.py
```

然后运行仓库级 Ruff：

```bash
uv run ruff check .
```

完整门禁使用独立的 `_test` 数据库。Pytest 会拒绝非 `_test` 数据库；不得绕过这项保护，也不得指向开发库或生产库。

```bash
docker compose -f deploy/docker-compose.yml up -d postgres redis

docker compose -f deploy/docker-compose.yml exec -T postgres sh -c \
  'psql -U dev -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = '\''social_reply_test'\''" | grep -q 1 || createdb -U dev social_reply_test'

export TESTING=true
export DATABASE_URL=postgresql+asyncpg://dev:dev@localhost:5432/social_reply_test
export REDIS_URL=redis://localhost:6379/0
export PLATFORM_SECRET_KEYS=Wm5wbamjBFvTmkGIU2NskIKCrJfsb4AdUBDZR-m1-CM=

uv run alembic upgrade head
uv run alembic check
head_revision="$(uv run alembic heads)"
current_revision="$(uv run alembic current)"
test -n "$head_revision"
test "$head_revision" = "$current_revision"

env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
    -u http_proxy -u https_proxy -u all_proxy \
    uv run pytest -q

git diff --check
```

按变更类型增加验证：

- 修改 shell 脚本：运行 `bash -n <changed-script>`。
- 修改迁移或 SQLAlchemy metadata：必须运行上述 Alembic upgrade/check/head-current 全套检查；必要时从空测试库验证。发布前必须阅读 `docs/production-migration.md` 中适用章节，先完成并记录其要求的 PostgreSQL 备份及恢复验证、数据库存、旧副本清退、流量/Worker 暂停和分阶段开关；任一前置条件未满足都不得发布。
- 修改 Dockerfile、`.dockerignore`、`entrypoint.sh` 或 production runtime assets：必须执行真实 `linux/amd64` build，并复现 `.github/workflows/ci.yml` 的 image contract checks。
- 修改路由、Webhook、OAuth、Admin 或 Provisioning API：增加 focused route/security tests，并确认稳定协议路径与 HTTP methods 未意外变化。
- 修改发送、队列、恢复或幂等逻辑：必须覆盖成功、重试、重复投递、并发/lease、fail-closed 和恢复路径。
- 纯文档变更仍需运行 Ruff、完整 pytest、适用的迁移检查和 `git diff --check`；不得假设文档删除不会破坏测试或脚本引用。

本地 production image 构建命令：

```bash
full_sha="$(git rev-parse HEAD)"
image="reply-core-local:${full_sha}"
docker buildx build \
  --platform linux/amd64 \
  --provenance=false \
  --build-arg "RELEASE_SHA=${full_sha}" \
  --build-arg "BUILD_DATE=1970-01-01T00:00:00Z" \
  --build-arg "SOURCE_URL=https://github.com/junqingyongyuanbusi/dm" \
  --tag "$image" \
  --load \
  .
```

CI 中的 `Production image` Job 是镜像验收的完整权威实现；修改镜像契约时必须同步更新 CI 检查。

## 项目验收要求

仓库变更不能在“代码写完”“本地测试通过”或“Git push 完成”时宣告完成。除非用户明确要求不部署，每个推送到 `dev` 的完成变更都必须发布并部署对应镜像。

任何要求让本地代码在 Railway 生效的交付，默认包含“提交到 `dev` → 推送 `origin/dev` → 等待 CI 全绿 → 构建并发布 Docker Hub immutable SHA 镜像 → 提升同一 digest 为 `latest` → 通过发布脚本更新 Railway 三角色 → 验证健康与 digest”的完整链路。`railway restart` 只重启现有容器，不会携带未提交、未构建或未发布的代码，不得作为代码发布的替代。

### 提交与 CI

1. 逐项核对用户要求，并审查最终 diff；不得提交无关用户改动。
2. 运行 focused tests、仓库 Ruff、完整 pytest、适用的迁移/metadata/image 检查和 `git diff --check`。
3. 在 `dev` 上独立提交当前任务。提交前用 `git diff --cached --check` 和 `git status --short` 确认暂存内容。
4. 推送到 `origin/dev`。
5. 等待该提交的全部 GitHub Actions Job 成功；不得在 CI pending、cancelled 或 failed 时发布。
6. 如果 GitHub、Docker Hub 或 Railway 凭证不可用，报告 `implementation complete, release blocked`；不得声称任务完整或已经部署。

### Docker Hub 发布契约

- 只能从干净工作树、已推送且等于 `origin/dev` 的 `dev` commit 发布。
- 对该 commit 只构建一个 `linux/amd64` 镜像。
- 推送 immutable full-SHA tag：`zhiyangxiaozi/reply-core:<40-character-git-sha>`。
- 不得单独重建 `latest`。必须用 registry manifest tooling 将已推送的 SHA 镜像提升为 `latest`。
- 提升时必须使用 `docker buildx imagetools create --prefer-index=false`，确保 SHA tag 与 `latest` 解析到同一 digest。
- 替换 `latest` 前，将 Railway 当前运行 digest 保留为 `railway-pre-<short-sha>` rollback tag；immutable SHA tag 不得删除或覆盖。

### Railway 发布契约

- `api`、`worker`、`scheduler` 的 source image 必须保持 `zhiyangxiaozi/reply-core:latest`。
- 标准发布必须通过发布脚本执行 `railway redeploy --from-source`，不能依赖普通 redeploy 或镜像自动更新。`docs/production-migration.md` 中经审查的 migration-specific staged rollout 优先于这条通用规则；只允许按其明确步骤执行 API-only 或变量切换阶段，并在阶段结束后重新验证健康、digest 和兼容窗口。
- 先部署 API，因为 API 拥有数据库准备和迁移。API 必须达到 `SUCCESS`、运行预期 digest，且 `/healthz` 成功后才能继续。
- 再部署 Worker 和 Scheduler；两者必须达到 `SUCCESS` 并运行同一预期 digest。
- 最终验证必须证明 API、Worker、Scheduler 运行完全相同且等于 Docker Hub `latest` 的 digest，并确认 API、Worker、Scheduler、PostgreSQL、Redis 区域一致。
- 保留前一 digest 和 deployment IDs。镜像回滚不等于数据库回滚；涉及迁移时遵循 `docs/production-migration.md`。
- 需要 staged/coordinated rollout 的变更必须同步更新 `docs/production-migration.md`，不能盲目执行通用发布。

### 唯一默认发布命令

```bash
scripts/publish_railway_release.sh
```

发布必须从干净 clone 或干净 worktree 执行。不得为了满足 clean-worktree 条件而删除、stash、reset 或提交无关用户改动。除非正在修复发布脚本本身，不得用临时手工命令替代；任何手工 fallback 都必须保持上述全部不变量并明确报告。
