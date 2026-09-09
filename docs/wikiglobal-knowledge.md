# wikiglobal 金融知识引导组件

## 范围和状态

本组件把原型中的商品、订单、物流示例替换为外汇金融领域的教育和操作指引。
它是可嵌入后台页面的只读 HTML 片段，不是知识导入器，也不是监管、投顾或交易系统。
按照本次确认的公司命名，界面与不可变回复契约的公司身份统一为 `wikiglobal`。
回复契约仅更改公司名称，金融风险限制、事实依据要求、语言规则和六字段动作协议保持不变；
这不构成对公司资质、监管许可或与其他品牌法律关系的断言。

新增文件仅包含渲染 helper、Jinja 模板、专项单测和本文档。
现已接入管理员知识页、客服/运营主管只读知识页，以及分类关键词查询。

## 页面集成接口

```python
from social_reply.application.account_management.financial_knowledge import (
    FINANCIAL_KNOWLEDGE_TEMPLATES,
    render_financial_knowledge_guide,
)

guide_html = render_financial_knowledge_guide(
    _tenant_root(tenant_id), can_manage=principal.is_admin
)
```

返回值是经过 Jinja 自动转义的 `str`。现有 Python HTML 拼装可以直接拼接；
传入其他 Jinja 模板时仅在边界上用 `trusted_html(guide_html)`。
不得把模板字段、用户输入或原始 URL 直接标成 trusted HTML。
根路径必须是 `/app/t/<tenant>`，租户部分允许字母、数字、下划线及连字符。
权限由调用路由负责；`can_manage` 只控制人工操作说明，不能替代鉴权。
同一页面只挂载一次，避免重复标题 ID。沿用现有 `saas-*` 类，无脚本、无新样式依赖。

具体挂载点位于 `saas_console.py`：

1. `tenant_knowledge`：完成 `knowledge.read` 能力检查后，管理员保留原知识治理表单，
   在列表和工具之后挂载指南；不覆盖新增表单、英文核实、受保护字段和发布控制。
2. 非管理员进入 `knowledge_read_view.render_published_knowledge`，只读展示有权访问品牌的
   已发布且非敏感知识，并挂载 `can_manage=False` 的指南。
3. 分类链接统一是 `/app/t/<tenant>/knowledge-query?keyword=<固定词>`。
   六个固定词由不可变常量中的 `keyword` 提供：`forex`、`regulator`、`platform`、
   `fees`、`risk`、`complaint`。使用英文检索词与现有英文知识审核流程兼容，
   中英文 UI 显示同一分类的本地化名称。

### 查询路由与隐私保护

当前查询路由只在带 CSRF 的 POST 中读取 `q`，GET `q` 会重定向清除；
它同时支持限定上述六个公开词的 GET `keyword` 分支，
复用 `SearchPublishedKnowledgeQuery` 及现有租户、可见品牌和敏感信息过滤。
不接受任意 GET 用户问题，不移除 POST CSRF 或清除 GET `q` 的隐私防护。
未知 `keyword` 返回 422，不能反射或执行为任意搜索。

这些是关键词检索，不是数据库中新增的分类字段或严格分类过滤；
现有知识无匹配时应显示正常空结果，不能拿模板填充结果或伪造命中。
现有管理页确实支持 `category` 过滤，但它只允许管理员访问，且现有分类值不一定
与新指南的分类名称一致，因此不能让所有角色链接到管理页来假装完成查询。

## 模板与录入约束

`FINANCIAL_KNOWLEDGE_TEMPLATES` 是 frozen dataclass 的 tuple，含稳定 key、固定 keyword、
`zh` / `en` 内容。每种内容均有标题、question、reply、来源提示、核验日期提示、
金融安全界限及需人工补全事项。`get_locale()` 控制 zh-CN/en 显示。

所有内容都标注为未发布示例，没有虚构来源 URL、核验日期、监管编号、牌照、收益或到账承诺。
来源和核验提示明确是待补全事项，不是已经取得的证据。
管理员应核查适用地区、法律实体、官方文件和版本，并保留真实核验记录；
完成后才按现有知识审核与发布流程操作。

现有新增接口只接受 `question`、`reply`、`brand_id`、`platform`、`category`、
`is_official_contact`、`protected_values_json` 和 CSRF token。
它没有 `q/a` 预填接口，也没有本组件专用的来源、日期字段。
因此本组件不生成伪预填链接、不绕过字段白名单、不自动保存草稿。
管理员可以手工复制英文参考，将必要的公开来源和适用日期加入待审正文，
并通过现有审核流程确认；不要把内部审查备注混入对外回复。
中文界面额外提供英文参考，保留现有 `Protected values JSON`、英文核实、
official/contact 标记和发布约束，不自动勾选或认可任何核实状态。

## 对外可检索知识与内部 SOP 必须隔离

客户可能通过 RAG 回答间接看到已发布的知识内容。模板当前仅提供对外安全内容：
外汇基础、经纪商及监管核对方法、平台操作、费用和出入金核对、风险教育、
公开投诉入口与人工求助说明。

**本组件没有建立受众模型，也不声称数据库已经支持客户/内部隔离。**
不能仅凭标题带有“内部”、前端隐藏、category 或状态标签，就推断内容不会被检索。
发布前，应核对真实检索和授权链路，而不是复制原型里的“仅人工内部”标签。

内部 SOP 不得导入当前 RAG：包括反欺诈阈值、内部升级规则与人员名单、
审核判断、赔付权限、客户身份材料、账户余额或流水、凭据及私有联系方式。
投诉模板只介绍对外安全的求助方法，不包含内部处置 SOP。
内部流程应存放于有独立访问控制且不进入客户检索的载体；未来如需统一管理，
必须先设计并验证数据库、检索、导入、发布及 API 全链路的受众授权模型。

## 专项验证记录

- RED：先新增测试并运行 `uv run --frozen pytest tests/unit/test_financial_knowledge.py -q`，
  因 `financial_knowledge` 模块尚不存在而 collection 失败。
- GREEN：新增实现后，相同命令得到 13 passed；无数据库、Redis 或外部 API 调用。
- 覆盖要点：六类中英文模板和核验提示、只读权限说明、固定公开关键词 URL、
  不安全根路径拒绝、Jinja XSS 转义及常量不可变。
- 以上仅记录 helper 的验证，不等同完整端到端或覆盖率报告；工作台权限与浏览器
  验收范围见 `docs/wikiglobal-acceptance.md`。
- 本地实现尚未提交、推送或部署，未修改生产数据。
