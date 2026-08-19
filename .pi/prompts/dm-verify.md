---
description: 按 dm 的 AGENTS.md 执行轻量本地检查、专项验证或读取 CI/Railway 证据
argument-hint: "[变更范围或验收标准]"
---

验证当前仓库变更，关注 `${ARGUMENTS:-当前工作区全部任务变更}`。目标是产生可执行、可观察、可审计的证据，而不是只给命令清单。

开始前：

1. 完整读取根目录 `AGENTS.md`。
2. 运行 `git branch --show-current`、`git status --short`、`git diff --check`。
3. 区分任务变更与预先存在的用户改动；不得 reset、restore、checkout、clean、stash 或批量暂存来制造干净状态。
4. 根据实际 diff 判断变更类型，读取相应实现、测试、CI 和权威文档。

执行策略：

1. 默认只审查最终 diff，并执行 `git status --short`、`git diff --check`；不自动运行 focused tests、全仓 Ruff、Alembic、完整 pytest、Docker build 或本地 PostgreSQL/Redis。
2. 用户明确指定测试、完整验证或某个验收标准时，只运行该范围所需的最小命令。
3. GitHub CI 是 Ruff、独立 `_test` 数据库迁移、完整 pytest 和 production image 的权威验证；提交已推送时，优先读取对应 SHA 的 CI 状态，不在本地重复整套门禁。
4. CI 失败时，先读取首个可操作错误；只有需要复现时才在本地启动必要服务或运行对应 focused command。
5. Railway 验证仅限部署后的有界 smoke、health、日志、deployment status、digest 和经授权测试 tenant/account 的行为观察。禁止在 Railway `production` 环境运行 pytest，禁止将 Alembic/测试连接指向业务数据库，禁止调用未经授权的真实平台业务 API。
6. 验证失败时区分代码、测试、依赖服务、环境变量、代理、权限、网络、缓存或旧产物问题；不要通过弱化断言、关闭安全校验、换模型或改 Prompt 掩盖故障。
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
