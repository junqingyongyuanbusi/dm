---
description: 生成 dm 当前任务的可恢复交接，记录状态、决策、证据、风险和下一步
argument-hint: "[任务名称或交接对象]"
---

为 `${ARGUMENTS:-当前任务}` 生成一份可供下一位开发者或下一次 Pi 会话继续工作的交接说明。

只读收集证据：

1. 完整读取根目录 `AGENTS.md`。
2. 运行 `git branch --show-current`、`git rev-parse HEAD`、`git status --short`。
3. 查看当前任务相关 diff、最近提交和已有验证输出；区分已提交、已推送、CI、已发布和已部署等不同状态。
4. 明确列出所有预先存在、不得覆盖的用户改动。

硬约束：

- 不编辑文件，不提交、不 push、不发布、不部署，不改变远程状态。
- 不把计划、尝试或部分结果写成完成。
- 未运行或无法证明的检查写 `NOT VERIFIED`；失败写 `FAIL`，并保留首个可操作错误。
- 不复制 secret、token、完整连接串、Cookie、OAuth code、用户 PII 或原始 Session transcript。
- 重要决定必须写出原因和依据；不要只给结论。

按以下结构输出：

```markdown
# Handoff: <任务>

## Goal and Acceptance Criteria
- 用户目标
- 完成条件
- 非目标

## Repository State
- Branch
- HEAD
- Working tree
- 必须保留的既有改动
- 与 origin/dev 的 ahead/behind 状态

## Decisions
- 决定、理由、权威文件或代码依据

## Changes
- 已修改文件及目的
- 已提交 / 未提交
- Commit / Push / CI / Release / Deploy 各自状态

## Verification
- `exact command` — PASS / FAIL / NOT VERIFIED
- 可观察证据

## Gaps and Risks
- 未完成工作
- 已知风险
- 环境或外部阻塞

## Next Steps
1. 下一步具体动作
2. 对应验证
3. 停止条件

## Rollback
- 安全回滚路径
- 若涉及迁移，引用 `docs/production-migration.md` 的适用要求
```

交接必须让接手者无需猜测当前状态，也不能要求其恢复、删除或提交无关用户改动。
