"""Read-only financial knowledge examples; never persistence or publication commands."""

import re
from dataclasses import dataclass
from urllib.parse import urlencode

from social_reply.application.account_management.templating import render_template
from social_reply.application.account_management.ui_i18n import get_locale


@dataclass(frozen=True)
class FinancialKnowledgeContent:
    title: str
    question: str
    reply: str
    source_hint: str
    verified_at_hint: str
    safety_boundary: str
    required_information: str


@dataclass(frozen=True)
class FinancialKnowledgeTemplate:
    key: str
    keyword: str
    zh: FinancialKnowledgeContent
    en: FinancialKnowledgeContent


_ZH_VERIFICATION = "尚未核验。人工核对来源后填写核验日期 YYYY-MM-DD、核验人及适用地区。"
_EN_VERIFICATION = (
    "Not verified. Record verification date YYYY-MM-DD, reviewer and jurisdiction "
    "only after checking the source."
)
_ZH_BOUNDARY = (
    "仅提供一般金融知识和操作指引，不提供个性化投资建议、买卖信号或收益承诺；"
    "不代表监管机构或投资顾问，不保证机构资质、资金安全或投诉结果。"
)
_EN_BOUNDARY = (
    "General education and operational guidance only, not personalized investment advice, "
    "trading signals or return promises. This does not represent a regulator or investment "
    "adviser and does not guarantee authorization, fund safety or complaint outcomes."
)

FINANCIAL_KNOWLEDGE_TEMPLATES: tuple[FinancialKnowledgeTemplate, ...] = (
    FinancialKnowledgeTemplate(
        key="forex-basics",
        keyword="forex",
        zh=FinancialKnowledgeContent(
            title="外汇基础",
            question="外汇中的货币对、点差和杠杆分别是什么？",
            reply=(
                "货币对表示两种货币之间的报价关系；点差是买价与卖价之差。"
                "杠杆使名义交易敞口高于所需保证金，并可能放大亏损。具体合约、"
                "报价单位和保证金规则应以适用产品的正式文件为准。"
            ),
            source_hint="待补充：适用监管机构的投资者教育原文、产品风险披露及官方链接。",
            verified_at_hint=_ZH_VERIFICATION,
            safety_boundary=_ZH_BOUNDARY,
            required_information="补全：产品类型、适用地区、术语定义来源及文件版本。",
        ),
        en=FinancialKnowledgeContent(
            title="Forex basics",
            question="What are currency pairs, spreads and leverage?",
            reply=(
                "A currency pair quotes one currency against another. The spread is the "
                "difference between bid and ask prices. Leverage creates exposure above "
                "the required margin and can amplify losses. Consult the applicable "
                "product documents for contract, quotation and margin rules."
            ),
            source_hint="Required: official investor education and product risk disclosure links.",
            verified_at_hint=_EN_VERIFICATION,
            safety_boundary=_EN_BOUNDARY,
            required_information="Complete: product type, jurisdiction, definitions and version.",
        ),
    ),
    FinancialKnowledgeTemplate(
        key="broker-regulation",
        keyword="regulator",
        zh=FinancialKnowledgeContent(
            title="经纪商及监管信息",
            question="如何核对经纪商的监管信息？",
            reply=(
                "先确认签约法律实体、官方网站及服务地区，再通过适用监管机构的官方"
                "登记系统核对名称、编号、状态和许可范围。相似名称、网站标识或登记"
                "记录不能单独证明某项业务获准，更不构成资金安全保证。信息冲突时请人工核实。"
            ),
            source_hint="待补充：相关监管机构官方登记链接和法律实体原始披露；不以评分代替。",
            verified_at_hint=_ZH_VERIFICATION,
            safety_boundary=_ZH_BOUNDARY,
            required_information="补全：法律实体、司法辖区、登记编号、许可范围及查验时间。",
        ),
        en=FinancialKnowledgeContent(
            title="Brokers and regulation",
            question="How can I check broker regulatory information?",
            reply=(
                "Identify the contracting legal entity, official website and service jurisdiction. "
                "Check the relevant regulator's official register for its name, reference, "
                "status and permitted activities. A similar name, logo or register entry alone "
                "does not establish authorization for a specific service or guarantee fund "
                "safety. Ask for human verification when details conflict."
            ),
            source_hint="Required: official regulator register and legal entity disclosure.",
            verified_at_hint=_EN_VERIFICATION,
            safety_boundary=_EN_BOUNDARY,
            required_information="Complete: legal entity, jurisdiction, reference, scope and time.",
        ),
    ),
    FinancialKnowledgeTemplate(
        key="platform-operations",
        keyword="platform",
        zh=FinancialKnowledgeContent(
            title="交易平台使用",
            question="遇到交易平台登录或使用问题时应该怎么办？",
            reply=(
                "确认使用经核验的官方入口和适用版本，再按官方帮助文档排查连接及"
                "错误提示。不要向聊天助手提供密码、验证码、私钥或远程控制权限。"
                "涉及账户恢复、订单执行或真实资金状态的问题须由授权人工渠道核实。"
            ),
            source_hint="待补充：对应平台及版本的官方帮助文档和经核验的支持入口。",
            verified_at_hint=_ZH_VERIFICATION,
            safety_boundary=_ZH_BOUNDARY,
            required_information="补全：平台名称、客户端版本、错误说明及公开排障步骤。",
        ),
        en=FinancialKnowledgeContent(
            title="Trading platform use",
            question="What should I do about platform login or usage problems?",
            reply=(
                "Check the verified official entry point and applicable version, then follow "
                "official help for connection or error messages. Never give a chat assistant "
                "passwords, verification codes, private keys or remote access. Account recovery, "
                "order execution and actual fund status require authorized human verification."
            ),
            source_hint="Required: version-specific official help and verified support channel.",
            verified_at_hint=_EN_VERIFICATION,
            safety_boundary=_EN_BOUNDARY,
            required_information="Complete: platform, client version, error and public help steps.",
        ),
    ),
    FinancialKnowledgeTemplate(
        key="fees-funding",
        keyword="fees",
        zh=FinancialKnowledgeContent(
            title="费用与出入金说明",
            question="在哪里核对交易费用和出入金条件？",
            reply=(
                "查阅适用法律实体、账户类型及支付方式的最新官方费用和出入金条款。"
                "点差、佣金、隔夜费用、换汇费用及支付服务商费用可能不同。"
                "到账时限和提现资格需要按实际情况人工确认；不要仅凭聊天消息转账，"
                "不要向未经核验的收款地址付款。"
            ),
            source_hint="待补充：官方费用表、出入金条款、版本及适用账户说明。",
            verified_at_hint=_ZH_VERIFICATION,
            safety_boundary=_ZH_BOUNDARY,
            required_information="补全：法律实体、账户及币种、支付方式、费用条件和限制。",
        ),
        en=FinancialKnowledgeContent(
            title="Fees, deposits and withdrawals",
            question="Where can I check fees and funding conditions?",
            reply=(
                "Read current official fees and funding terms for the legal entity, account "
                "type and payment method. Spreads, commissions, overnight, conversion and "
                "payment-provider fees can differ. Arrival times and withdrawal eligibility "
                "need case-specific human confirmation. Do not transfer funds solely on chat "
                "instructions or pay an unverified recipient."
            ),
            source_hint="Required: official fees, funding terms, version and account scope.",
            verified_at_hint=_EN_VERIFICATION,
            safety_boundary=_EN_BOUNDARY,
            required_information="Complete: entity, account, currency, method and fee conditions.",
        ),
    ),
    FinancialKnowledgeTemplate(
        key="risk-education",
        keyword="risk",
        zh=FinancialKnowledgeContent(
            title="风险教育",
            question="外汇和杠杆产品有哪些需要了解的风险？",
            reply=(
                "价格波动、杠杆、流动性、交易对手及执行问题均可能造成损失；"
                "某些产品或地区下，损失可能超过初始投入。历史表现不代表未来结果，"
                "止损也不保证按指定价格成交。阅读适用风险披露；需要个人适用性建议时，"
                "请咨询当地具备相应资格的专业人士。"
            ),
            source_hint="待补充：产品风险披露及适用监管机构的风险教育原文。",
            verified_at_hint=_ZH_VERIFICATION,
            safety_boundary=_ZH_BOUNDARY,
            required_information="补全：产品、适用地区、风险披露版本及负余额保护适用条件。",
        ),
        en=FinancialKnowledgeContent(
            title="Risk education",
            question="What risks should I understand about forex and leveraged products?",
            reply=(
                "Market moves, leverage, liquidity, counterparties and execution problems can "
                "cause losses. Depending on product and jurisdiction, losses may exceed the "
                "initial amount. Past performance does not predict future results, and stop "
                "orders do not guarantee execution prices. Read applicable risk disclosures; "
                "seek appropriately qualified local advice for personal suitability questions."
            ),
            source_hint="Required: product risk disclosure and official regulator education.",
            verified_at_hint=_EN_VERIFICATION,
            safety_boundary=_EN_BOUNDARY,
            required_information="Complete: product, jurisdiction, version and protections.",
        ),
    ),
    FinancialKnowledgeTemplate(
        key="complaints-escalation",
        keyword="complaint",
        zh=FinancialKnowledgeContent(
            title="投诉与人工升级",
            question="遇到争议或怀疑欺诈时如何寻求人工帮助？",
            reply=(
                "保留相关时间、沟通记录和经过脱敏的问题摘要，通过经核验的官方投诉"
                "或人工支持渠道提交。不要在公开评论中发布身份或金融账户资料，不要"
                "支付所谓保证追回费用。涉及资金安全或疑似欺诈时，请及时联系支付"
                "机构及适用的官方报案渠道；处理时限和结果需要相关机构确认。"
            ),
            source_hint="待补充：经核验的公开投诉政策、支持渠道及适用官方报案指引。",
            verified_at_hint=_ZH_VERIFICATION,
            safety_boundary=_ZH_BOUNDARY,
            required_information="补全：公开入口、服务时间、适用地区及必要的最少资料清单。",
        ),
        en=FinancialKnowledgeContent(
            title="Complaints and human escalation",
            question="How can I seek human help for a dispute or suspected fraud?",
            reply=(
                "Keep dates, correspondence and a redacted issue summary, then use verified "
                "official complaints or human support channels. Do not post identity or "
                "financial account details publicly or pay guaranteed-recovery fees. For fund "
                "safety concerns or suspected fraud, promptly contact the payment provider "
                "and applicable official reporting channel. Timelines and outcomes require "
                "confirmation by the responsible organization."
            ),
            source_hint="Required: verified public complaint policy and official support guidance.",
            verified_at_hint=_EN_VERIFICATION,
            safety_boundary=_EN_BOUNDARY,
            required_information="Complete: public channel, hours, jurisdiction and required data.",
        ),
    ),
)


def render_financial_knowledge_guide(root: str, can_manage: bool) -> str:
    """Render escaped public-safe examples, using a trusted, tenant-scoped route root.

    The embedding route owns authentication and keyword handling. This helper neither
    grants permissions nor saves drafts; mark only its returned HTML as trusted.
    """
    if not re.fullmatch(r"/app/t/[A-Za-z0-9_-]+", root):
        raise ValueError("Invalid tenant root for financial knowledge guide")
    locale = get_locale()
    english = locale == "en"
    cards = tuple(
        {
            "key": template.key,
            "content": template.en if english else template.zh,
            "english_content": template.en,
            "href": f"{root}/knowledge-query?{urlencode({'keyword': template.keyword})}",
        }
        for template in FINANCIAL_KNOWLEDGE_TEMPLATES
    )
    return render_template(
        "tenant/financial_knowledge.html",
        cards=cards,
        english=english,
        can_manage=can_manage,
    )
