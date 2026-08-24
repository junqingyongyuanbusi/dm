---
description: 只读审查 dm 当前变更，按仓库契约报告缺陷、风险和验证缺口
argument-hint: "[范围、文件或关注点]"
---

对当前仓库做一次**只读代码审查**。审查范围为 `${ARGUMENTS:-当前工作区与 HEAD 的 diff}`。

开始前：

1. 完整读取根目录 `AGENTS.md`，把它作为项目事实、架构、测试和发布契约。
2. 运行 `git branch --show-current`、`git status --short`，识别基线、任务变更和预先存在的用户改动。
3. 读取变更涉及的实现、调用方、测试、配置和权威文档；不要只看 diff 片段。

审查重点：

- 正确性、回归、并发、幂等、租户隔离和异常路径；
- CSRF、OAuth state、Webhook 验签、凭证、kill switch、发送前复检和 Outbox 不变量；
- API / Worker / Scheduler 职责是否越界；
- PostgreSQL 持久事实与 Redis 临时状态边界是否被破坏；
- 测试是否验证行为而不是锁死私有实现，是否缺少失败、重试、重复投递或恢复场景；
- 是否出现重复实现、死代码、宽泛静默异常、投机抽象、无调用 wrapper 或明显 AI 风格冗余；
- 是否遗漏当前变更适用的轻量本地检查、对应 SHA 的 CI 状态，或用户明确要求的专项验证；不得仅因未在本地重复 Ruff、完整 pytest、Alembic 或 Docker build 就判定为缺陷。

硬约束：

- 不编辑文件，不执行格式化，不提交、不 push、不发布、不部署，不修改 Issue/PR 或远程状态。
- 不运行会写业务数据或调用真实平台 API 的命令。
- 不把测试通过等同于没有问题；也不把风格偏好当成缺陷。
- 发现必须结合仓库证据，给出文件路径和具体理由；不确定项明确标注“需验证”。

按以下结构输出：

```markdown
## Findings
### Critical
- [path:line] 问题、影响、证据、建议
### High
### Medium
### Low

## Verification Gaps
- 尚未运行或证据不足的检查

## Contract Audit
- AGENTS.md 要求：PASS / FAIL / NOT VERIFIED

## Summary
- 是否存在阻塞合并或发布的问题
```

如果没有问题，明确写“未发现阻塞项”，并列出实际审查范围和仍未验证的风险。
