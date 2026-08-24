# 仓库工作契约

## 项目事实

- 只在 `dev` 分支工作，除非用户明确指定其他分支；不得隐式修改或部署 `main`。
- 保留与当前任务无关的用户改动。禁止用 reset、checkout、restore、clean 或批量暂存覆盖、恢复、删除这些改动。
- 本项目是 Python 3.13 的 FastAPI 模块化单体。API、Worker、Scheduler 从同一个 GitHub `dev` commit 的根目录 `Dockerfile` 分别构建，由必填的 `SERVICE_ROLE=api|worker|scheduler` 选择进程角色；缺少或未知角色必须在启动前失败。
- PostgreSQL 是账号、凭证信封、入站证据、会话、任务、决策、Outbox、审计和恢复状态的持久事实源。Redis 只承载 Dramatiq、kill switch、OAuth 临时状态及可重建缓存，不得成为业务事实源。
- API 拥有 HTTP 路由、Admin、OAuth callback、Provisioning API、Webhook ingress 和数据库准备；Worker 拥有异步处理与发送；Scheduler 拥有周期恢复、轮询和巡检。Worker 与 Scheduler 不运行 HTTP 服务，也不执行迁移。
- API、Worker、Scheduler 必须共享 PostgreSQL、Redis、`PLATFORM_SECRET_KEYS`、安全配置和功能开关，并与 PostgreSQL、Redis 部署在同一基础设施区域。PostgreSQL 与 Redis 只能使用私有网络端点；只有 API 可以接收公网 ingress，Worker 与 Scheduler 不得暴露公网服务。
- 新平台账号默认 `BOT_DRAFT_ONLY`。不得为了通过测试或加速上线而放宽 CSRF、OAuth state、Webhook 验签、租户隔离、kill switch、发送前复检、幂等或 Outbox 约束。
- `.env.example` 仅用于本地单进程 smoke。生产配置的事实源是 Railway 服务变量，并由 `scripts/validate_railway_config.py` 在每次发布前验证；不得把生产 secret 写进仓库、日志、测试快照或临时文件。
- 当前架构和配置权威文档是 `docs/architecture.md` 与 `docs/configuration.md`；数据库和协调发布要求见 `docs/production-migration.md`。`docs/superpowers/plans/` 只保存历史计划，不是运行时契约。
- 生产目标固定为：
  - GitHub 源码：`junqingyongyuanbusi/dm` 的 `dev` 分支；Railway 原生 GitHub autodeploy 必须关闭
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
- `scripts/publish_railway_release.sh`：Production Deploy workflow 内部的 commit-pinned 串行发布实现，不是本地 operator 入口。
- `tests/unit/`：无需真实外部服务的单元和契约测试。
- `tests/integration/`：依赖本地 PostgreSQL/Redis 或跨模块边界的测试。
- `deploy/docker-compose.yml`：仅用于本地 PostgreSQL/Redis 开发与测试，不是生产编排文件。
- `Dockerfile`、`entrypoint.sh`：三角色共享的 production build/runtime 与 fail-closed 启动契约。
- `.github/workflows/ci.yml`：Ruff、迁移/全量 pytest、真实 `linux/amd64` production image 三道 CI 门禁；`.github/workflows/deploy-production.yml`：CI 后固定 SHA 的 production 串行源码发布入口。
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

只有用户在当前请求中明确要求部署或让代码在 Railway 生效时，才执行“提交到 `dev` → 推送 `origin/dev` → 等待该 SHA 的 CI 全绿 → 由独立 Production Deploy workflow 固定该 SHA → API → `/healthz` → Worker → Scheduler → 验证三角色 commit provenance”的完整链路。`railway restart` 只重启现有容器，不会携带未部署的代码，不得作为发布替代。

### 提交与 CI

1. 逐项核对用户要求，并审查最终 diff；不得提交无关用户改动。
2. 默认只运行 `git status --short`、`git diff --check` 和提交前的 `git diff --cached --check`；不要求在本地运行 focused、Ruff、完整 pytest、Alembic 或 image build。
3. 在 `dev` 上独立提交当前任务并推送到 `origin/dev`；推送前后核对 `HEAD` 与远端 SHA。
4. `.github/workflows/ci.yml` 继续使用 `cancel-in-progress: true` 处理快速 CI；生产发布必须由独立 `.github/workflows/deploy-production.yml` 承担，并使用 `concurrency: production-source-release`、`cancel-in-progress: false`，不得在 API 后被新 push 取消。
5. Production Deploy 必须等待触发 SHA 的 CI 全绿，并通过 Railway `serviceInstanceDeployV2(commitSha: ...)` 部署精确 SHA；禁止用会重新解析移动分支头的普通 redeploy 代替。
6. 如果 GitHub 或 Railway 凭证不可用，准确报告 commit、push、CI 或 release 阻塞状态，不得把未完成阶段说成已完成。

### GitHub 源码发布契约

- `api`、`worker`、`scheduler` 必须连接 `junqingyongyuanbusi/dm`，Railway 原生 GitHub autodeploy 与 deployment triggers 必须关闭；唯一自动入口是 GitHub Actions Production Deploy。
- 普通 `dev` push 只在 CI 成功后发布。排队期间若该 SHA 尚未开始生产变更且已不是最新 `dev` 头，可跳过；一旦开始 API 部署，整次 API → Worker → Scheduler rollout 必须完整收尾。
- 先部署 API，因为 API 拥有数据库准备。API 必须达到 `SUCCESS`、deployment metadata 的 `commitHash` 等于目标 SHA，且 `/healthz` 成功后才能继续。
- 再部署 Worker 和 Scheduler；两者必须达到 `SUCCESS` 且 `commitHash` 等于同一目标 SHA。失败后重跑必须允许每个角色处于 predecessor 或 target，并从首个未完成角色继续。
- 发布前后必须验证三角色真实 Settings、`SERVICE_ROLE`、共享变量、域名、replicas/restart policy 和区域指纹不变，并确认 API、Worker、Scheduler、PostgreSQL、Redis 同区。
- 自动源码发布若发现 `migrations/` 或 `alembic.ini` 相对当前 production predecessor 有变化，必须在任何 production mutation 前 fail closed。含 Alembic 图变化的发布不是普通自动发布，必须遵循 `docs/production-migration.md` 的 migration-aware 路径；数据库升级后不得直接部署不认识新 head 的旧源码 commit。
- 保留切换前 Docker digest、predecessor Git SHA 和 deployment IDs。Docker Hub immutable tags 与 `scripts/publish_railway_docker_release.sh` 只作为 migration-aware/紧急回退资产，不是普通代码发布入口。
- 需要 staged/coordinated rollout 的变更必须同步更新 `docs/production-migration.md`，不能盲目执行通用发布。

### 唯一默认发布入口

普通发布入口是向 `origin/dev` 推送经过审查的 commit；CI 成功后，受 `production-source-release` concurrency 保护的 `.github/workflows/deploy-production.yml` 自动执行。需要回退普通代码时，在 `dev` 上提交经过审查的 revert，再走同一 push/CI/发布入口；不得在本地伪造 Actions 标志直接执行内部脚本。

`scripts/publish_railway_release.sh` 是 workflow 内部实现，不是本地 operator 命令。Docker migration-aware 路径使用 `scripts/publish_railway_docker_release.sh`，且只能按 `docs/production-migration.md` 中针对具体迁移审查过的 staged source-transition 方案执行。

提交与发布不得为了制造干净状态删除、stash、reset 或覆盖无关用户改动。任何 fallback 都必须保持上述全部不变量并明确报告。
