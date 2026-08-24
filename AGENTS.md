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

## 本地验证与远端验证

本地默认采用轻量流程，目标是快速审查、提交和推送，不重复 GitHub CI 的完整门禁。开始前只需确认分支、工作区和最终 diff，并保留无关用户改动：

```bash
git branch --show-current
git status --short
git diff --check
```

默认不在本地启动 PostgreSQL/Redis，不强制运行 focused tests、全仓库 Ruff、Alembic upgrade/check、完整 pytest 或 production image build。代码可以在完成最终 diff 审查后直接提交并推送；不得为了制造干净状态使用 reset、restore、checkout、clean 或 stash。

只有以下情况才增加本地专项验证：

- 用户在当前请求中明确要求运行某项测试或完整验证。
- GitHub CI 失败，需要在本地复现首个可操作错误。
- 改动无法被现有 CI 覆盖，且缺少该检查会让提交内容本身不可解析或不可审查；此时只运行最小检查，例如变更 shell 的 `bash -n <changed-script>` 或变更 Python 文件的定向 Ruff/编译检查。
- 发布脚本要求在发布前执行的配置、digest、迁移兼容或镜像 smoke；这些检查属于发布流程，不是每次本地提交的前置条件。

GitHub Actions 是 Ruff、独立 `_test` 数据库迁移、完整 pytest 和真实 `linux/amd64` production image 的权威自动验证。推送后读取对应 SHA 的 CI 状态；CI 失败时根据日志继续修复，不要求在 push 前本地重复同一套完整检查。

Railway 中的“测试”只允许是部署后的有界 smoke、`/healthz`、日志、deployment status、digest 和经授权测试 tenant/account 的真实行为观察。禁止在 Railway `production` 环境执行 pytest，禁止把 Alembic/测试命令指向业务数据库，也不得为测试调用未经授权的真实平台业务 API。

修改迁移、SQLAlchemy metadata、Docker/runtime assets、路由/安全、发送/队列/幂等逻辑时，仍必须保证对应 CI 或发布脚本门禁覆盖这些风险；不得删除或绕过 `_test` 数据库保护、镜像合同、配置一致性、迁移兼容、kill switch、Outbox、租户隔离或发送前复检。

## 项目验收要求

仓库变更完成以用户当前请求为准。完成实现和最终 diff 审查后可以直接提交并推送；本地完整测试不是 commit/push 的前置条件。Git push 只更新 GitHub，不自动代表 Railway 已部署。

只有用户在当前请求中明确要求部署或让代码在 Railway 生效时，才执行“提交到 `dev` → 推送 `origin/dev` → 等待该 SHA 的 CI 全绿 → 构建并发布 Docker Hub immutable SHA 镜像 → 提升同一 digest 为 `latest` → 通过发布脚本更新 Railway 三角色 → 验证健康与 digest”的完整链路。`railway restart` 只重启现有容器，不会携带未提交、未构建或未发布的代码，不得作为代码发布的替代。

### 提交与 CI

1. 逐项核对用户要求，并审查最终 diff；不得提交无关用户改动。
2. 默认只运行 `git status --short`、`git diff --check` 和提交前的 `git diff --cached --check`；不要求在本地运行 focused、Ruff、完整 pytest、Alembic 或 image build。
3. 在 `dev` 上独立提交当前任务并推送到 `origin/dev`；推送前后核对 `HEAD` 与远端 SHA。
4. 推送后读取该提交的 GitHub Actions 状态并如实报告。CI 失败时继续修复；CI pending 不影响“代码已推送”的事实，但不得声称 CI 已通过。
5. 只有执行 Railway 发布时才必须等待该 SHA 的全部 CI Job 成功；不得在 CI pending、cancelled 或 failed 时发布。
6. 如果 GitHub、Docker Hub 或 Railway 凭证不可用，准确报告对应的 commit、push、CI 或 release 阻塞状态，不得把未完成阶段说成已完成。

### Docker Hub 发布契约

- 只能从干净工作树、已推送且等于 `origin/dev` 的 `dev` commit 发布。
- 对该 commit 只构建一个目标 `linux/amd64` production image。涉及新增 Alembic head 时，发布脚本可额外基于当前运行 predecessor digest 构建一个只叠加新 migration graph 的 migration-compatible rollback image；它不是第二个目标镜像。
- 推送 immutable full-SHA tag：`zhiyangxiaozi/reply-core:<40-character-git-sha>`。
- 不得单独重建 `latest`。必须用 registry manifest tooling 将已推送的 SHA 镜像提升为 `latest`。
- 提升时必须使用 `docker buildx imagetools create --prefer-index=false`，确保 SHA tag 与 `latest` 解析到同一 digest。
- 替换 `latest` 前，将 Railway 当前运行 digest 保留为 `railway-pre-<short-sha>` 审计 tag；若新 release 引入 Alembic head，还必须构建、推送并 smoke-test `railway-compat-pre-<short-sha>`，其内容为 predecessor app 加新 migration graph。数据库迁移后应用回滚只能使用 compatible digest，不能直接使用 raw predecessor digest。Immutable tag 不得删除或覆盖。

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
