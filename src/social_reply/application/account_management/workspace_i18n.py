"""Fixed bilingual workspace copy, using the shared request locale without global edits."""

from social_reply.application.account_management.ui_i18n import get_locale

_COPY: dict[str, tuple[str, str]] = {
    "contacts.title": ("联系人", "Contacts"),
    "contacts.description": (
        "查看联系人与最近会话。",
        "Browse contacts and recent conversations.",
    ),
    "contacts.scope": (
        "仅显示你可访问账号下的联系人；跨渠道身份不自动合并。每页最多 50 条。",
        "Only contacts on accessible accounts. "
        "Cross-channel identities are not merged. 50 per page.",
    ),
    "contacts.search": ("搜索联系人或渠道账号", "Search contacts or channel accounts"),
    "contacts.placeholder": (
        "姓名、平台用户 ID 或账号名称",
        "Name, platform user ID or account name",
    ),
    "contacts.empty": (
        "未找到可见联系人，请调整搜索或等待真实入站消息。",
        "No visible contacts found. Adjust the search or wait for real inbound messages.",
    ),
    "contacts.name": ("联系人", "Contact"),
    "contacts.external_id": ("平台用户 ID", "Platform user ID"),
    "contacts.account": ("渠道账号", "Channel account"),
    "contacts.recorded": ("添加时间", "Added"),
    "contacts.detail": ("联系人详情", "Contact details"),
    "contacts.recent": ("最近创建的会话", "Recently created conversations"),
    "contacts.detail_note": (
        "最近 20 个会话。",
        "Latest 20 conversations.",
    ),
    "contacts.no_conversations": (
        "该联系人暂无可见会话。",
        "No visible conversations for this contact.",
    ),
    "contacts.back": ("返回联系人", "Back to contacts"),
    "contacts.visible": ("可访问联系人", "Accessible contacts"),
    "contacts.page_count": ("本页记录", "Records on this page"),
    "contacts.identity_note": (
        "同名不代表同一个人。仅展示真实渠道身份，不推断客户语言、标签或负责人。",
        "Matching names do not establish identity. Real channel identities only; "
        "no inferred language, tags or owner.",
    ),
    "common.search": ("搜索", "Search"),
    "common.previous": ("上一页", "Previous"),
    "common.next": ("下一页", "Next"),
    "common.open": ("查看详情", "View details"),
    "common.conversation": ("查看会话", "View conversation"),
    "common.platform": ("平台", "Platform"),
    "common.channel": ("会话类型", "Conversation type"),
    "common.created": ("创建时间", "Created"),
    "common.count": ("数量", "Count"),
    "common.status": ("当前状态", "Current status"),
    "common.unknown": ("未记录", "Not recorded"),
    "common.no_records": ("所选范围暂无记录。", "No records in the selected scope."),
    "reports.title": ("数据报表", "Reports"),
    "reports.description": (
        "查看消息趋势与渠道表现。",
        "Track message trends and channel activity.",
    ),
    "reports.scope": (
        "仅统计当前可访问账号。时间为滚动 UTC 区间，包含起点、不包含终点。",
        "Accessible accounts only. Rolling UTC interval, inclusive start and exclusive end.",
    ),
    "reports.window": ("统计周期", "Reporting period"),
    "reports.seven": ("最近 7 天", "Last 7 days"),
    "reports.thirty": ("最近 30 天", "Last 30 days"),
    "reports.apply": ("更新报表", "Update report"),
    "reports.inbound": ("入站消息", "Inbound messages"),
    "reports.outbound": ("出站消息", "Outbound messages"),
    "reports.active_conversations": ("有消息的会话", "Conversations with messages"),
    "reports.new_conversations": ("新增会话", "New conversations"),
    "reports.human_replies": ("人工出站消息", "Human outbound messages"),
    "reports.human_items": ("新建人工工作项", "New human work items"),
    "reports.outbox_total": ("新建投递记录", "New delivery records"),
    "reports.platforms": ("消息平台分布", "Messages by platform"),
    "reports.human_status": (
        "区间内新建人工工作项 · 当前状态",
        "Human items created in window · current status",
    ),
    "reports.outbox_status": (
        "区间内新建 Outbox · 当前状态",
        "Outbox created in window · current status",
    ),
    "reports.methodology": ("统计口径", "Counting methodology"),
    "reports.trend": ("每日消息趋势", "Daily message trend"),
    "reports.distribution": ("渠道消息分布", "Channel message distribution"),
    "reports.distribution_note": (
        "按当前统计区间的真实入站消息展示，不生成缺失的历史趋势。",
        "Actual inbound messages in the selected window; "
        "missing historical trends are not generated.",
    ),
    "reports.messages_note": (
        "仅统计可访问账号，按消息记录时间计数，不含私有备注。",
        "Accessible accounts only; message counts use recorded time and exclude private notes.",
    ),
    "reports.conversations_note": (
        "周期为滚动 UTC 区间；趋势首尾日可能不完整。活跃会话去重，新增会话按创建时间计数。",
        "Rolling UTC window; edge days may be partial. Active conversations are distinct; "
        "new conversations use creation time.",
    ),
    "reports.cohort_note": (
        "人工工作项与 Outbox 均按 created_at 归入区间，再展示当前状态；"
        "不是区间内的状态变更次数或发送完成次数，不代表历史快照或成功率。",
        "Human items and Outbox are grouped by created_at, then shown by current status. "
        "These are not in-window transitions or sends, historical snapshots or success rates.",
    ),
    "reports.not_available": (
        "未计算满意度、首次响应时长、收益或转化率：当前页面没有足够的已核实数据。",
        "Satisfaction, first-response time, returns and conversion rates are not calculated: "
        "verified data is insufficient here.",
    ),
    "flows.title": ("自动化流程", "Automation flows"),
    "flows.description": (
        "查看回复流程与 Agent 配置。",
        "View reply flows and Agent settings.",
    ),
    "flows.notice": (
        "只读预览，不会修改或运行流程。",
        "Read-only preview. No flows are changed or run.",
    ),
    "flows.chain": ("回复流程", "Reply flow"),
    "flows.readonly": ("只读", "Read only"),
    "flows.inspector": ("节点说明", "Node details"),
    "flows.select_note": (
        "选择画布节点查看执行条件；选择仅影响此页面，不保存或改变生产流程。",
        "Select a node to inspect conditions. Selection only changes this page, not production.",
    ),
    "flows.chain_note": (
        "展示代码路径，不表示任何账号已启用自动外发。实际分支受 Agent 配置和功能开关控制。",
        "This shows code paths, not whether any account is live. "
        "Actual branches depend on Agent configuration and feature flags.",
    ),
    "flows.match_only_note": (
        "重要分支差异：match-only 当前仅执行空文本与长度约束，跳过完整硬性输出 Guard、"
        "语义依据验证及语言观察；传统风险词规则也未完整执行。草稿模式最终降级仍保留。"
        "此页仅如实展示，不修改这些生产策略。",
        "Important branch difference: match-only currently checks empty text and length, "
        "but skips the full hard-output Guard, grounding verification and language observation; "
        "legacy risk-word rules are also limited. Final draft-mode downgrade still applies. "
        "This page documents, but does not change, production policy.",
    ),
    "flows.state": ("自动化状态与急停", "Automation state and kill switch"),
    "flows.state_note": (
        "检查自动回复状态；人工接待时不自动回复。",
        "Check automation status; do not auto-reply during human handling.",
    ),
    "flows.rules": ("规则与知识候选", "Rules and knowledge candidates"),
    "flows.rules_note": (
        "按 Agent 配置匹配规则与知识，必要时转人工。",
        "Match rules and knowledge using Agent settings; hand off when needed.",
    ),
    "flows.hard": ("硬性输出校验", "Hard output checks"),
    "flows.hard_note": (
        "按回复模式检查内容约束，不合规的候选不直接发送。",
        "Apply the reply mode's content checks before sending a candidate.",
    ),
    "flows.grounding": ("语义依据验证", "Semantic grounding verification"),
    "flows.grounding_note": (
        "适用的知识回复核对已批准内容；并非所有模式启用。",
        "Eligible knowledge replies are checked against approved content; not used in every mode.",
    ),
    "flows.language": ("语言观察与草稿降级", "Language observation and draft downgrade"),
    "flows.language_note": (
        "按模式检查回复语言；草稿模式不会自动外发。",
        "Check language according to the reply mode; draft mode never sends automatically.",
    ),
    "flows.delivery": ("发送前确认", "Pre-send checks"),
    "flows.delivery_note": (
        "再次检查发送资格与权限，避免重复发送。",
        "Recheck sending eligibility and permissions, and prevent duplicate sends.",
    ),
    "flows.outcomes": ("可能结果", "Possible outcomes"),
    "flows.outcomes_note": (
        "忽略 / 转人工 / 生成草稿 / 发送回复。",
        "Ignore / hand off / draft / send reply.",
    ),
    "agents.heading": ("可见 Agent", "Visible Agents"),
    "agents.scope": (
        "最多显示 200 个真实 Agent 或已有运行品牌范围；可见范围沿用账号权限。",
        "Up to 200 real Agents or persisted legacy runtime brands; "
        "visibility follows account access.",
    ),
    "agents.empty": (
        "当前没有可见的真实 Agent。请由管理员配置并关联渠道账号。",
        "No real Agents are visible. Ask an administrator to configure and link accounts.",
    ),
    "agents.configure": ("查看 Agent 配置", "View Agent settings"),
    "agents.test": ("打开独立测试页", "Open standalone test"),
    "playground.title": ("测试台", "Playground"),
    "playground.description": (
        "选择 Agent，试一条客户消息。",
        "Choose an Agent and try a customer message.",
    ),
    "playground.notice": (
        "提示词试答，不含完整知识检索，不会向客户外发。",
        "Prompt trial only; not full knowledge retrieval. Nothing is sent to customers.",
    ),
    "playground.select": ("选择测试 Agent", "Select an Agent to test"),
    "playground.load": ("切换 Agent", "Switch Agent"),
    "playground.message": ("客户消息", "Customer message"),
    "playground.placeholder": (
        "输入一条不含个人敏感信息的客户消息…",
        "Enter a customer message without sensitive personal data…",
    ),
    "playground.run": ("运行测试", "Run test"),
    "playground.running": ("正在试答…", "Running trial…"),
    "playground.result": ("测试结果", "Test result"),
    "playground.waiting": ("等待测试", "Awaiting test"),
    "playground.complete": ("试答完成", "Trial completed"),
    "playground.empty_title": ("先试一条客户消息", "Try a customer message"),
    "playground.empty_note": (
        "从左侧选择真实 Agent 并输入问题，查看模型试答与处理摘要。",
        "Select a real Agent on the left and enter a question to inspect its trial response.",
    ),
    "playground.failed": (
        "未能获取试答结果。请检查登录、权限或输入，必要时打开现有测试页重试。",
        "Could not obtain a trial result. Check login, permissions or input; "
        "open the existing test page if needed.",
    ),
    "playground.timeout": (
        "等待结果超时，后端可能仍在执行。请稍后再试，避免立即重复调用。",
        "Waiting for the result timed out; the backend may still be running. "
        "Wait before retrying to avoid duplicate calls.",
    ),
    "playground.no_send": ("提示词试答 · 不外发", "Prompt trial · no sending"),
    "playground.boundaries": ("测试边界", "Test boundaries"),
    "playground.boundaries_note": (
        "进入现有测试路由后，沿用其授权、CSRF、输入校验和试答限流；"
        "仅具备相应试答权限的用户可执行。本页不会代理模型请求或绕过权限。",
        "The existing test route retains authorization, CSRF, input validation "
        "and trial rate limits. Only users with trial permission may run it. "
        "This page neither proxies models nor bypasses access.",
    ),
    "playground.no_simulation": (
        "不模拟渠道发送窗口、完整知识检索、人工接管或 Outbox。试答通过不代表生产可发送。",
        "No simulation of channel windows, full retrieval, takeover or Outbox. "
        "A successful trial does not imply production send eligibility.",
    ),
    "playground.example": ("建议验证的业务问题", "Suggested business questions"),
    "playground.example_note": (
        "例如：如何查阅外汇风险披露？账户验证需要哪些已公布材料？"
        "请勿输入真实证件、账户密码或交易指令；这些是问题示例，不是系统答案。",
        "For example: Where can I read forex risk disclosures? "
        "Which published documents are needed "
        "for account verification? Do not enter real IDs, passwords or trade instructions. "
        "These are sample questions, not system answers.",
    ),
    "status.WAITING": ("待处理", "Waiting"),
    "status.CLAIMED": ("已接管", "Claimed"),
    "status.RESOLVED": ("已解决", "Resolved"),
    "status.CANCELLED": ("已取消", "Cancelled"),
    "status.PENDING": ("待发送", "Pending"),
    "status.SENDING": ("发送中", "Sending"),
    "status.SENT": ("已发送", "Sent"),
    "status.FAILED": ("失败", "Failed"),
    "status.NEEDS_REVIEW": ("待复核", "Needs review"),
    "status.EXPIRED": ("已过期", "Expired"),
    "status.unknown": ("其他状态", "Other status"),
}


def workspace_text(key: str) -> str:
    return _COPY[key][1 if get_locale() == "en" else 0]


def workspace_status(status: str) -> str:
    key = f"status.{status}"
    return workspace_text(key if key in _COPY else "status.unknown")
