# 企业邮箱（Email）渠道接入与运维

> 状态：Email 渠道已实现，包括账号接入、只读 IMAP 轮询、统一决策/人工草稿、SMTP
> 回复、Outbox 恢复和管理员控制面。当前 Alembic 唯一 head 为 `e9a1c4f7b620`。
>
> 仓库测试使用 stub/fake 协议客户端，不代表已使用真实企业邮箱凭证联网验证。管理员提供
> 目标邮箱凭证后，仍必须先完成本文 Phase 0，再执行接入后的 real smoke。

Email 是平台无关渠道：可接入满足本文 TLS、host allowlist 和 DNS 公共目标约束的
IMAP/SMTP 企业邮箱。本文以飞书国际版（Lark）的常见服务器参数为示例；收信由 Scheduler
轮询 IMAP，发信由 Worker 通过 SMTP 完成，不使用 Lark OpenAPI 邮件发送接口。

## 1. 已实现契约

### 1.1 会话、线程与频控

- 会话按邮件 thread 建立。线程根优先取 `References`，其次取 `In-Reply-To`，最后取当前
  `Message-ID`；`conversation_key` 同时包含 account、规范化 sender 和 thread root。
  sender 必须参与会话键，避免不同发件人在相同或碰撞的 thread 标识下串人、共享上下文。
- 自动回复频控不按 thread 隔离，而是按 **account + sender** 统计过去 24 小时已成功发送的
  `DECISION/BOT` Email 回复。因此同一发件人在多个 thread 中共享
  `EMAIL_PER_SENDER_DAILY_REPLY_LIMIT`；默认上限为 5。人工回复和草稿批准不计入该 Bot
  频控。
- 回复目标绑定原始入站 Message、Conversation contact 和账号，发送前再次校验，不能把收件人
  改成任意地址。

### 1.2 IMAP 读取边界

- IMAP 仅支持 TLS 直连（`IMAP4_SSL`），使用系统默认 CA、主机名校验和证书验证。
- 登录后执行 readonly `SELECT`；邮件正文抓取使用 UID `FETCH ... BODY.PEEK[]`，不会通过读取
  操作设置 `\\Seen`。系统不移动或删除邮件。
- 首次接入以当前最大 UID 为锚，只处理之后的新邮件，不回放已有收件箱。UIDVALIDITY 变化时记录
  `EMAIL_UIDVALIDITY_CHANGED` gap，并在新 UID epoch 的当前最大 UID 重新锚定；历史间隙不会自动
  回放，以避免自动回复风暴。
- `RawEvent` 只保存最小轮询证据：UID、UIDVALIDITY、消息字节数，以及在实际抓取时计算的
  SHA-256；不保存 RFC822 正文、主题、发件人或附件内容。正文只在内存中规范化，业务 Message
  按既有数据模型持久化。

### 1.3 SMTP 与网络安全

- SMTP 只接受 `ssl` 或 `starttls`。未填写 SMTP 端口时，`ssl` 默认使用 465，
  `starttls` 默认使用 587；显式填写的合法端口会保留。`ssl` 使用 `SMTP_SSL`；`starttls`
  必须先 EHLO、使用验证证书和主机名的默认 TLS context 成功升级、再次 EHLO，然后才允许
  登录。STARTTLS 不可用、升级失败或证书验证失败时直接失败，绝不降级为明文认证或发送。
- IMAP 与 SMTP host 必须先命中 `EMAIL_ALLOWED_HOSTS`。每次新建连接前还会重新解析 DNS，且
  所有返回地址都必须是公共全局地址；IP literal、localhost、私网、loopback、link-local、
  reserved、multicast、unspecified 或公私混合 DNS 结果均 fail closed。
- 标准库高层 IMAP/SMTP 客户端不能把已检查地址可靠 pin 到随后 socket；实现是在每次连接前
  立即做 allowlist 和 DNS 公共目标校验，以缩小但不能完全消除 resolver-to-connect race。

### 1.4 自动化门禁

- `EMAIL_ENABLED=false` 和 `EMAIL_AUTO_REPLY_ENABLED=false` 都是代码默认值，两个环境模板也
  保持相同默认。
- `EMAIL_ENABLED` 是渠道总 gate：关闭时不允许接入、不执行 Email 轮询，Email Outbox 在发送前
  进入可恢复的 `NEEDS_REVIEW/EMAIL_DISABLED`。
- 新接入 Email 账号强制 `BOT_DRAFT_ONLY`；API 请求和 Worker provisioning 都拒绝
  `BOT_ACTIVE`。Admin 中的“接入探测”只表示最近一次凭证接入验证通过或错误，不是持续监控；
  接入成功不会自动开启外发。
- 自动外发是第二道 gate：代码仅在三个角色一致设置 `EMAIL_ENABLED=true`、
  `EMAIL_AUTO_REPLY_ENABLED=true`，且管理员明确把账号切为 `BOT_ACTIVE` 时允许 Bot 决策发送。
  **当前 rollout 明确禁止开启该 gate**：现有入站数据没有可信 envelope sender，也没有经过
  SPF、DKIM、DMARC 验证的认证结果，不能把可伪造的 `From` 头当作自动回复授权证据。当前
  Railway 目标值必须保持 `EMAIL_AUTO_REPLY_ENABLED=false`，草稿留在
  `/admin/inbox?queue=drafts` 等待人工审核。
- 当前没有独立的 Email 持续监控或 health reconciler。持久化的
  `email_health_status=READY` 和探测时间来自 provisioning 时的一次 IMAP/SMTP 凭证接入验证；
  不要把它描述成 Scheduler 周期健康巡检。

## 2. Lark 侧管理员准备

以下是接入操作清单，需由目标组织管理员在其实际 Lark 控制台确认；仓库本身没有真实租户或
邮箱凭证可验证这些 provider-side 步骤。

### 2.1 开启第三方客户端权限

管理后台通常位于：

```text
Product Settings > Mail > Email Management Tools > User Function Permissions
  > Third-party email client > Edit > Permissions: On
```

为实际接入成员或公共邮箱开启 IMAP/SMTP，并生成专用密码。公共邮箱应在管理员后台开启其
IMAP/SMTP Service。控制台名称可能随 Lark 版本变化，以目标租户当前页面为准。

### 2.2 常见 Lark 国际版参数

| 协议 | 地址 | 端口 | 加密 |
| --- | --- | --- | --- |
| IMAP | `imap.larksuite.com` | 993 | SSL |
| SMTP | `smtp.larksuite.com` | 465 | SSL |
| SMTP | `smtp.larksuite.com` | 587 | STARTTLS |

默认 `EMAIL_ALLOWED_HOSTS=imap.larksuite.com,smtp.larksuite.com`。接入其他供应商前，管理员
必须先审查其 host，并在 API、Worker、Scheduler 上部署完全一致的 allowlist；不要为方便测试
加入通配符、IP 地址、localhost 或私网地址。

## 3. Phase 0 与 real smoke

当前仓库不声称已完成真实邮箱联网验证。管理员提供目标邮箱地址、专用密码和服务器参数后，先在
受控环境做 Phase 0，仅验证 TLS、登录和 readonly mailbox 访问，不发送客户邮件：

```bash
python3 - <<'EOF'
import imaplib
import smtplib
import ssl

imap = imaplib.IMAP4_SSL(
    "imap.larksuite.com",
    993,
    ssl_context=ssl.create_default_context(),
    timeout=10,
)
print(imap.login("support@corp.com", "<专用密码>"))
print(imap.select("INBOX", readonly=True))
imap.logout()

smtp = smtplib.SMTP_SSL(
    "smtp.larksuite.com",
    465,
    timeout=10,
    context=ssl.create_default_context(),
)
print(smtp.login("support@corp.com", "<专用密码>"))
smtp.quit()
EOF
```

若使用 587，Phase 0 必须显式 `EHLO -> STARTTLS(ssl.create_default_context()) -> EHLO -> login`；
STARTTLS 失败时停止，不得改用明文继续。

Phase 0 通过后，再执行系统 real smoke：

1. 保持 `EMAIL_AUTO_REPLY_ENABLED=false`，用测试发件人向目标邮箱发送一封新邮件；
2. 确认 Scheduler 轮询后生成 Email Conversation、Message 和草稿，且邮箱未被标记已读；
3. 在 Admin 审批草稿，确认测试收件人收到带正确 `Re:`、`In-Reply-To`、`References`、
   `Auto-Submitted: auto-replied` 和 `X-Auto-Response-Suppress` 的纯文本回复；
4. 用同一 sender 在不同 thread 验证会话隔离和 account+sender 跨 thread 频控；
5. 检查对应 `RawEvent.payload` 只有 UID/UIDVALIDITY/size/可选 SHA-256，没有正文；
6. 上述 smoke 只验证人工审核链路，不授权自动回复。继续保持
   `EMAIL_AUTO_REPLY_ENABLED=false`；任何未来自动回复试点必须先接入并真实验证可信 envelope
   sender 与 SPF/DKIM/DMARC 认证证据，再通过一次独立的 Email 安全评审，之后才能另行批准。

## 4. 接入与 Railway rollout

Email schema 由 revision `e9a1c4f7b620` 提供；它在 `b7e4c2d9a615` 后增加 Email 平台约束、
`EMAIL_IMAP` checkpoint、`EMAIL_UIDVALIDITY_CHANGED` gap 和 Email Bot sender-rate 索引。

Railway 必须保持 API、Worker、Scheduler 使用同一个不可变提交构建出的同一镜像 digest，并为
七个 Email 配置项提供相同值。标准顺序：

1. 先以 `EMAIL_ENABLED=false`、`EMAIL_AUTO_REPLY_ENABLED=false` 部署目标镜像；
2. API 先启动并执行数据库准备/迁移，确认数据库唯一 head 为 `e9a1c4f7b620`，API deployment
   `SUCCESS` 且 `/healthz` 正常；
3. 再启动或替换 Worker、Scheduler，确认三角色均为 `SUCCESS`、同一 digest、相同 Email flags
   和 allowlist；
4. 管理员完成 Phase 0 后，在三个角色同时设置 `EMAIL_ENABLED=true` 并协调重启，再通过
   `/admin/accounts` 或 `POST /api/v1/platform-accounts/email` 接入账号；
5. 保持新账号 `BOT_DRAFT_ONLY` 完成 real smoke。当前 Railway rollout 的目标配置固定为
   `EMAIL_AUTO_REPLY_ENABLED=false`，不得逐账号提升到 `BOT_ACTIVE`。只有后续实现并用真实邮件
   验证可信 envelope sender 和 SPF/DKIM/DMARC 证据，且完成独立安全评审与单独发布批准后，
   才能修改这一目标。

不要在 API 已启用而旧 Worker/Scheduler 仍运行，或三个角色 flags/allowlist 不一致时接入账号。
数据库迁移成功只证明 schema 已升级，不证明 DNS、TLS、邮箱凭证、IMAP 或 SMTP 的真实通路。

## 5. 运行行为与运维

- 默认轮询间隔 `EMAIL_POLL_INTERVAL_SECONDS=60`；实际端到端延迟取决于 Scheduler、队列和决策
  耗时，不能承诺固定 1-2 分钟。
- 入站会过滤自动回复、bulk/list 邮件、系统发件人、空退信地址、自发邮件，以及默认策略下的
  同域内部邮件。Admin 中“同域内部邮件”默认选择“忽略（推荐）”，用于降低自动回复循环风险；
  只有明确选择“允许进入处理流程”时才处理同域来信。附件与日历邀请只保留受限元数据，并走现有 unsupported attachment 人工路径，
  不自动回复附件内容。
- 出站为纯文本并维护线程头；自动回复还带 RFC 3834 防循环头。

| 事项 | 操作 |
| --- | --- |
| 专用密码轮换 | 在 provider 侧吊销旧密码并生成新密码，再通过账号接入流程更新凭证并重新执行 IMAP/SMTP probe |
| 暂停渠道 | 三角色同时设 `EMAIL_ENABLED=false` 并重启；Outbox、账号、checkpoint 和 gap 保留 |
| 暂停自动外发 | 三角色同时设 `EMAIL_AUTO_REPLY_ENABLED=false`；新 Bot 决策保持草稿/发送前 fail closed，人工发送仍按现有权限链路处理 |
| 单账号停用 | 在 `/admin/accounts` 停用账号 |
| 观察轮询 | Scheduler 日志中的 `sweep_name=poll_email_messages` 及账号级稳定错误码 |
| 收信断流 | 检查总 gate、账号 active/READY、凭证是否吊销、allowlist、DNS 公共目标、IMAP 登录/SELECT，以及 UIDVALIDITY gap |
| 发送失败 | 检查 Outbox `last_error_code`；SMTP 5xx 通常为永久失败，4xx/连接前网络错误按现有重试语义处理，发送后结果不明确时不盲目重试 |

## 6. 回滚边界

优先关闭两个 Email gates 并回滚应用镜像，同时保留 additive schema。若必须 downgrade 到
`b7e4c2d9a615`，revision `e9a1c4f7b620` 会在存在任一 Email account、`EMAIL_IMAP`
checkpoint 或 `EMAIL_UIDVALIDITY_CHANGED` gap 时 fail closed。删除这些持久化事实会丢失账号和
同步审计上下文，只能按已批准的导出/备份恢复计划执行；关闭 feature flag 本身不满足 downgrade
条件。完整迁移与回滚流程见 [Production migration notes](production-migration.md)。
