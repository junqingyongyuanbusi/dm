"""Member-management labels, isolated from the shared shell catalog."""

from social_reply.application.account_management.ui_i18n import get_locale

_LABELS = {
    "team.title": ("成员与权限", "Members and permissions"),
    "team.description": (
        "角色决定能做什么，账号范围决定能看到什么。",
        "Roles define actions. Account scope defines visibility.",
    ),
    "team.members": ("成员", "Members"),
    "team.roles": ("角色权限", "Role permissions"),
    "team.scope": ("访问规则", "Access rules"),
    "team.add": ("添加成员", "Add member"),
    "team.create_hint": (
        "创建登录账号，首次登录须修改密码。",
        "Create a login; a password change is required on first sign-in.",
    ),
    "team.role": ("角色", "Role"),
    "team.group": ("所属团队", "Team"),
    "team.accounts": ("可访问账号", "Accessible accounts"),
    "team.status": ("状态", "Status"),
    "team.actions": ("操作", "Actions"),
    "team.you": ("你", "You"),
    "team.all_accounts": ("全部账号", "All accounts"),
    "team.assigned_accounts": ("个指定账号", "assigned accounts"),
    "team.assign": ("分配账号", "Assign accounts"),
    "team.empty": ("暂无成员", "No members yet"),
    "team.active": ("正常", "Active"),
    "team.disabled": ("已停用", "Disabled"),
    "team.password_required": ("待修改密码", "Password change required"),
    "team.operator_note": (
        "运营仅访问本人或分配的账号；回复和接管权限在成员账号页单独设置。",
        "Operators access owned or assigned accounts; set reply and takeover permissions per member.",
    ),
    "team.capability": ("能力", "Capability"),
    "team.allowed": ("允许", "Allowed"),
    "team.denied": ("不允许", "Not allowed"),
    "team.per_member": ("按成员设置", "Set per member"),
    "team.matrix_note": (
        "角色权限只读；运营的回复与接管开关在「分配账号」中设置。",
        "Role permissions are read-only; configure operator reply and takeover under Assign accounts.",
    ),
    "team.rule_scope": (
        "成员仅可访问当前工作区内本人或分配的账号；管理员可访问全部账号。",
        "Members access owned or assigned accounts in this workspace; "
        "administrators access all accounts.",
    ),
    "team.rule_actions": (
        "能查看不等于能回复，具体操作还取决于角色与渠道能力。",
        "Viewing does not grant replies; actions also depend on role and channel capabilities.",
    ),
    "team.rule_revoke": (
        "权限变更后，成员需重新登录，已领取任务和待发人工消息会同步处理。",
        "Permission changes require a new sign-in, release claimed work "
        "and cancel pending human messages.",
    ),
    "summary.WORKSPACE_ADMIN": (
        "管理工作区、成员与全部账号。", "Manage the workspace, members and all accounts.",
    ),
    "summary.MANAGER": (
        "监督接待，查看与测试配置。", "Supervise support; view and test configuration.",
    ),
    "summary.OPERATOR": (
        "连接本人账号，处理分配范围。", "Connect owned accounts and work within assigned scope.",
    ),
    "summary.AGENT": (
        "回复与接管授权范围内的会话。", "Reply to and take over conversations within scope.",
    ),
    "summary.VIEWER": (
        "查看授权范围内的会话、报表与审计。", "View conversations, reports and audit within scope.",
    ),
    "capability.home.read": ("接待总览", "Overview"),
    "capability.inbox.read": ("查看收件箱", "View inbox"),
    "capability.contacts.read": ("查看联系人", "View contacts"),
    "capability.agents.read": ("查看 AI Agent", "View AI agents"),
    "capability.flows.read": ("查看自动化流程", "View flows"),
    "capability.knowledge.read": ("查看知识库", "View knowledge"),
    "capability.playground.read": ("测试台", "Playground"),
    "capability.channels.read": ("查看渠道账号", "View channels"),
    "capability.reports.read": ("查看数据报表", "View reports"),
    "capability.team.read": ("成员与权限管理", "Manage members"),
    "capability.audit.read": ("查看审计日志", "View audit"),
    "capability.settings.read": ("工作区设置", "Workspace settings"),
    "capability.reply": ("人工回复", "Human replies"),
    "capability.takeover": ("接管会话", "Take over conversations"),
    "capability.connect": ("连接渠道账号", "Connect accounts"),
    "capability.configure": (
        "维护 AI、流程与知识配置", "Maintain AI, flow and knowledge configuration",
    ),
    "role.WORKSPACE_ADMIN": ("工作区管理员", "Workspace administrator"),
    "role.MANAGER": ("主管", "Manager"),
    "role.OPERATOR": ("运营", "Operator"),
    "role.AGENT": ("客服", "Agent"),
    "role.VIEWER": ("只读成员", "Viewer"),
    "access.title": ("分配账号", "Assign accounts"),
    "access.description": (
        "勾选成员可查看的账号，本人授权账号的访问权保留。",
        "Select accounts this member can view. Owned account access is retained.",
    ),
    "access.owner": ("拥有者（隐式访问，不可取消）", "Owner (implicit access, cannot be removed)"),
    "access.accounts": ("允许访问的账号", "Allowed accounts"),
    "access.operations": ("操作权限", "Operation permissions"),
    "access.empty": ("当前工作区没有账号。", "There are no accounts in this workspace."),
    "access.operator_reply": ("允许运营回复消息", "Allow operator replies"),
    "access.operator_takeover": ("允许运营接管会话", "Allow operator takeover"),
    "access.operator_hint": (
        "仅对运营角色生效，其他角色按角色权限执行。",
        "Applies to operators only; other roles use their role permissions.",
    ),
    "access.admin_hint": (
        "管理员可访问全部账号，不受勾选范围限制。",
        "Administrators access all accounts regardless of selection.",
    ),
    "access.revocation_hint": (
        "保存变更会撤销该成员的登录会话、释放已领取任务，并取消待发人工消息。",
        "Saving changes revokes this member's sessions, releases claimed work, "
        "and cancels pending human messages.",
    ),
    "access.save": ("保存成员权限", "Save member permissions"),
    "access.back": ("返回成员列表", "Back to members"),
}


def member_translate(key: str) -> str:
    labels = _LABELS.get(key)
    if labels is None:
        return key.removeprefix("capability.")
    return labels[1] if get_locale() == "en" else labels[0]


def member_role_label(role: str) -> str:
    normalized = "AGENT" if role == "USER" else role
    key = f"role.{normalized}"
    return member_translate(key) if key in _LABELS else role
