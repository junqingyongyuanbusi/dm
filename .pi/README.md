# 项目级 Pi 工程目录

本目录提供当前仓库共享、版本化的 Pi 工作流入口。仓库事实、架构边界、真实命令和发布契约仍以根目录 `AGENTS.md` 为准；`.pi/` 不能复制或覆盖这份契约。

## 当前资源

| 命令 | 文件 | 用途 | 默认副作用 |
| --- | --- | --- | --- |
| `/dm-review` | `prompts/dm-review.md` | 只读审查当前 diff 或指定范围 | 不编辑、不提交、不发布 |
| `/dm-verify` | `prompts/dm-verify.md` | 轻量检查当前 diff、读取对应 CI，或按用户要求执行专项验证 | 默认只读本地/远端证据；不自动启动测试基础设施或修改远程状态 |
| `/dm-handoff` | `prompts/dm-handoff.md` | 生成可恢复、可审计的任务交接 | 不编辑、不提交、不发布 |

Pi 从仓库根目录启动时会自动发现 `.pi/prompts/*.md`，文件名就是斜杠命令名。Prompt 自动发现不递归，因此共享命令保持在 `prompts/` 顶层。

## 使用

首次加载这些项目资源前，先审查本目录，再在仓库根目录启动 Pi 并确认 Project Trust：

```bash
cd /path/to/dm
pi --approve
```

交互会话中修改 Prompt 后使用：

```text
/reload
```

需要明确忽略项目 Pi 资源时：

```bash
pi --no-approve
```

`--approve` 只表示本次运行允许加载已审查的项目资源，不是文件系统、网络、Shell 或发布操作的 Sandbox。Pi Project Trust 是上下文与代码加载边界，不是操作系统隔离。

## 设计边界

当前有意不创建以下资源：

- `.pi/settings.json`：顶层 Prompt 可自动发现，暂不需要额外路径、Package 或项目级模型配置。
- `.pi/skills/`：尚无需要脚本、参考资料和按需加载的稳定多步骤能力。
- `.pi/extensions/`：尚无必须用运行时代码实现的工具或强制策略；Extension 具有当前用户权限。
- `.pi/agents/`：不是 Pi Core 的通用项目资源，只有特定 Subagent Extension 才会解释其格式。
- `.pi/SYSTEM.md` / `.pi/APPEND_SYSTEM.md`：仓库契约已经由 `AGENTS.md` 提供，不替换或扩张默认 System Prompt。

只有出现真实、重复且可验证的需求时才升级能力：

```text
短的显式重复任务      → Prompt
多步骤流程与参考资料    → Skill
工具、拦截或运行时状态  → Extension
跨两个以上项目稳定复用  → Pi Package
项目事实与硬约束        → AGENTS.md
```

## 版本控制

应提交：

- `.pi/README.md`
- `.pi/prompts/*.md`
- 未来经过审查的 `.pi/settings.json`、Skills 或 Extensions 源码

不得提交：

- `.pi/npm/`、`.pi/git/`
- 项目内 Session、日志或 JSONL transcript
- Extension 的 `node_modules/`
- `auth.json`、`trust.json` 或任何凭据

相关忽略规则位于仓库根目录 `.gitignore`。
