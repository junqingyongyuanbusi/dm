---
description: 按 dm 的 AGENTS.md 选择并执行 focused、仓库级和变更类型验证
argument-hint: "[变更范围或验收标准]"
---

验证当前仓库变更，关注 `${ARGUMENTS:-当前工作区全部任务变更}`。目标是产生可执行、可观察、可审计的证据，而不是只给命令清单。

开始前：

1. 完整读取根目录 `AGENTS.md`。
2. 运行 `git branch --show-current`、`git status --short`、`git diff --check`。
3. 区分任务变更与预先存在的用户改动；不得 reset、restore、checkout、clean、stash 或批量暂存来制造干净状态。
4. 根据实际 diff 判断变更类型，读取相应实现、测试、CI 和权威文档。

执行策略：

1. 先运行最小相关 focused tests。
2. 再运行 `uv run ruff check .`。
3. 按 `AGENTS.md` 使用独立的 `social_reply_test` 数据库执行 Alembic 与完整 pytest 门禁；不得指向开发库或生产库。
4. 修改 shell、迁移/metadata、路由/安全、队列/幂等、Docker/runtime assets 时，增加契约规定的专项检查。
5. 如果需要本地 PostgreSQL/Redis，先记录 `docker compose -f deploy/docker-compose.yml ps` 的初始状态，再用该 Compose 文件启动缺失服务。结束时只停止本次验证新启动的服务；原先已运行的服务必须保持运行。不得执行 `down -v` 或删除现有 volume。
6. 测试失败时先分类为代码、测试、依赖服务、环境变量、代理、权限、网络、缓存或旧产物问题；不要通过弱化断言、关闭安全校验、换模型或改 Prompt 掩盖环境故障。

权限边界：

- 可以运行仓库文档规定的本地只读检查、测试、构建和临时本地测试基础设施。
- 不自动 commit、push、publish、deploy、修改 Railway/GitHub/Docker Hub 或调用真实平台业务 API；除非用户在当前请求中明确授权，并且仍需遵守 `AGENTS.md` 发布契约。
- 不安装或升级依赖；如环境缺依赖，只报告需要执行的锁定安装命令。
- 不为了让测试通过而修改代码；若验证暴露真实缺陷，停止并报告最小可操作失败，除非用户同时要求修复。

按以下结构输出：

```markdown
## Scope
- 变更类型、文件和验收标准

## Automated Verification
- `exact command` — PASS / FAIL / NOT RUN
- 首个可操作错误或关键输出

## Observable Evidence
- Diff、schema、镜像、日志或行为证据

## Acceptance Criteria
- 每项要求 — PASS / FAIL / NOT VERIFIED

## Gaps and Risks
- 未验证项及原因

## Cleanup
- 启动的本地服务和清理状态

## Verdict
- verified / failed / blocked
```

只有所有适用门禁通过时才能写 `verified`；环境阻塞与代码失败必须区分。
