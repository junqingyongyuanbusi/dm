"""知识检索集成测试：pgvector 相似度命中/不命中 + require_knowledge 无命中降级转人工"""

import uuid

import pytest
from sqlalchemy import insert, select

import social_reply.infrastructure.queue.broker  # noqa: F401  确保测试用 StubBroker
from social_reply.application.knowledge.retrieval import (
    retrieve_exact_knowledge,
    retrieve_knowledge,
)
from social_reply.application.reply_decision import runner
from social_reply.application.reply_decision.pipeline import DecisionSnapshot
from social_reply.domain.automation.state_machine import ensure_state
from social_reply.domain.knowledge.embeddings import FakeEmbeddingClient
from social_reply.infrastructure.database import models
from social_reply.shared.config import get_settings

pytestmark = pytest.mark.integration

_EMBEDDER = FakeEmbeddingClient()
_TPL_HOURS = "问：你们几点营业？\n答：每天 9:00-18:00。"
_TPL_EMAIL = "问：怎么改绑定邮箱？\n答：请在设置-账号安全中修改。"


async def _seed_chunk(
    session,
    content: str,
    *,
    brand_id="b1",
    platform=None,
    status="published",
    embedding_version=None,
    reply=None,
    is_official_contact=False,
):
    doc_id = uuid.uuid4()
    await session.execute(
        insert(models.KnowledgeDocument).values(
            id=doc_id,
            brand_id=brand_id,
            platform=platform,
            question=content,
            reply=reply or content,
            status=status,
            is_official_contact=is_official_contact,
        )
    )
    embedding = (await _EMBEDDER.embed([content]))[0]
    await session.execute(
        insert(models.KnowledgeChunk).values(
            document_id=doc_id,
            content=content,
            content_hash=uuid.uuid4().hex,
            embedding_version=embedding_version or _EMBEDDER.version,
            embedding=embedding,
        )
    )
    await session.commit()


async def test_短模板忽略大小写和空白精确命中(session):
    await _seed_chunk(session, "True")
    hit = await retrieve_exact_knowledge(
        session, "  true  ", tenant_id="default", brand_id="b1", platform="telegram"
    )
    assert hit is not None
    assert hit.reply == "True"
    assert hit.similarity == 1.0


async def test_精确匹配排除草稿并传播官方联系方式标记(session):
    await _seed_chunk(session, "draft contact", status="draft", is_official_contact=True)
    await _seed_chunk(session, "official contact", is_official_contact=True)
    assert (
        await retrieve_exact_knowledge(
            session,
            "draft contact",
            tenant_id="default",
            brand_id="b1",
            platform="telegram",
        )
        is None
    )
    hit = await retrieve_exact_knowledge(
        session,
        "official contact",
        tenant_id="default",
        brand_id="b1",
        platform="telegram",
    )
    assert hit is not None
    assert hit.is_official_contact is True


async def test_精确匹配仍过滤品牌和平台(session):
    await _seed_chunk(session, "True", brand_id="other")
    await _seed_chunk(session, "Hi", platform="instagram")
    assert (
        await retrieve_exact_knowledge(
            session, "true", tenant_id="default", brand_id="b1", platform="telegram"
        )
        is None
    )
    assert (
        await retrieve_exact_knowledge(
            session, "hi", tenant_id="default", brand_id="b1", platform="telegram"
        )
        is None
    )


async def test_同文本查询相似度为一命中(session):
    await _seed_chunk(session, _TPL_HOURS)
    await _seed_chunk(session, _TPL_EMAIL)
    vec = (await _EMBEDDER.embed([_TPL_HOURS]))[0]
    hits = await retrieve_knowledge(
        session,
        vec,
        tenant_id="default",
        brand_id="b1",
        platform="telegram",
        embedding_version=_EMBEDDER.version,
        top_k=3,
        min_similarity=0.9,
    )
    assert [h.content for h in hits] == [_TPL_HOURS]
    assert hits[0].similarity == pytest.approx(1.0, abs=1e-6)
    assert hits[0].is_official_contact is False


async def test_向量检索传播官方联系方式标记(session):
    await _seed_chunk(session, _TPL_HOURS, is_official_contact=True)
    vec = (await _EMBEDDER.embed([_TPL_HOURS]))[0]
    hits = await retrieve_knowledge(
        session,
        vec,
        tenant_id="default",
        brand_id="b1",
        platform="telegram",
        embedding_version=_EMBEDDER.version,
        min_similarity=0.9,
    )
    assert len(hits) == 1
    assert hits[0].is_official_contact is True


async def test_不相干文本低于阈值不命中(session):
    # Fake 向量分量全正，随机对余弦约 0.75，用 0.9 阈值区分命中与不命中
    await _seed_chunk(session, _TPL_HOURS)
    vec = (await _EMBEDDER.embed(["今天天气怎么样"]))[0]
    hits = await retrieve_knowledge(
        session,
        vec,
        tenant_id="default",
        brand_id="b1",
        platform="telegram",
        embedding_version=_EMBEDDER.version,
        top_k=3,
        min_similarity=0.9,
    )
    assert hits == []


async def test_过滤条件_品牌_状态_版本(session):
    await _seed_chunk(session, _TPL_HOURS, brand_id="other")  # 品牌不符
    await _seed_chunk(session, _TPL_EMAIL, status="draft")  # 未发布
    await _seed_chunk(session, "问：A\n答：B", embedding_version="old-model")  # 版本不符
    for content in (_TPL_HOURS, _TPL_EMAIL, "问：A\n答：B"):
        vec = (await _EMBEDDER.embed([content]))[0]
        hits = await retrieve_knowledge(
            session,
            vec,
            tenant_id="default",
            brand_id="b1",
            platform="telegram",
            embedding_version=_EMBEDDER.version,
            top_k=3,
            min_similarity=0.9,
        )
        assert hits == []


# ---------- runner 层：require_knowledge 降级 / KNOWLEDGE_HIT ----------


async def _seed_conversation(session):
    account_id, contact_id, conv_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id, brand_id="b1", platform="telegram", name="acc", chatwoot_inbox_id=101
        )
    )
    await session.execute(
        insert(models.Contact).values(
            id=contact_id, platform="telegram", platform_account_id=account_id, external_user_id="9"
        )
    )
    await session.execute(
        insert(models.Conversation).values(
            id=conv_id,
            brand_id="b1",
            platform="telegram",
            platform_account_id=account_id,
            contact_id=contact_id,
            conversation_key="telegram:x:9",
        )
    )
    msg_id = uuid.uuid4()
    await session.execute(
        insert(models.Message).values(
            id=msg_id,
            conversation_id=conv_id,
            direction="inbound",
            sender_type="contact",
            text="请问几点营业",
            chatwoot_message_id=55,
        )
    )
    await ensure_state(session, conv_id, "BOT_ACTIVE")
    await session.commit()
    return account_id, conv_id, msg_id


def _snapshot(account_id):
    return DecisionSnapshot(
        text="请问几点营业",
        platform="telegram",
        tenant_id="default",
        brand_id="b1",
        account_id=str(account_id),
        conversation_key="telegram:x:9",
        automation_state="BOT_ACTIVE",
        state_version=1,
    )


@pytest.fixture
def knowledge_enabled(monkeypatch):
    """开检索开关并注入 Fake embedder；结束后恢复单例与 settings 缓存"""
    monkeypatch.setenv("KNOWLEDGE_RETRIEVAL_ENABLED", "true")
    try:
        get_settings.cache_clear()
        runner._llm = None
        runner._embedder = FakeEmbeddingClient()
        yield monkeypatch
    finally:
        get_settings.cache_clear()
        runner._llm = None
        runner._embedder = None


async def test_require_knowledge_无命中降级转人工(session, knowledge_enabled):
    knowledge_enabled.setenv("REQUIRE_KNOWLEDGE", "true")
    get_settings.cache_clear()
    account_id, conv_id, msg_id = await _seed_conversation(session)  # 知识库为空
    outbox_id = await runner.run_and_persist_decision(
        _snapshot(account_id), conv_id, msg_id, account_id
    )
    assert outbox_id is None  # handoff 不产 outbox
    dec = (await session.execute(select(models.ReplyDecision))).scalar_one()
    assert dec.action == "handoff"
    assert "INSUFFICIENT_KNOWLEDGE" in dec.reason_codes


async def test_llm_handoff_转为公开兜底且不锁会话(session, knowledge_enabled, monkeypatch):
    from social_reply.domain.reply.decision import ReplyAction, ReplyDecision

    class HandoffLLM:
        async def decide(self, context):
            return ReplyDecision(
                action=ReplyAction.HANDOFF,
                reason_codes=("OPENAI",),
                source="llm",
            )

    knowledge_enabled.setenv("REQUIRE_KNOWLEDGE", "false")
    get_settings.cache_clear()
    runner._llm = HandoffLLM()
    account_id, conv_id, msg_id = await _seed_conversation(session)
    outbox_id = await runner.run_and_persist_decision(
        _snapshot(account_id), conv_id, msg_id, account_id
    )
    assert outbox_id is None
    dec = (await session.execute(select(models.ReplyDecision))).scalar_one()
    assert dec.action == "handoff"
    assert dec.reason_codes == ["OPENAI"]
    state = (await session.execute(select(models.AutomationState))).scalar_one()
    assert state.state == "HANDOFF_PENDING"
    work = (await session.execute(select(models.HumanWorkItem))).scalar_one()
    assert work.status == "WAITING"
    assert work.reason_code == "OPENAI"


async def test_命中时决策附_knowledge_hit(session, knowledge_enabled):
    # Fake 向量默认阈值 0.5 下任意模板都会命中，足以验证接线与 reason code
    await _seed_chunk(session, _TPL_HOURS)
    account_id, conv_id, msg_id = await _seed_conversation(session)
    await runner.run_and_persist_decision(_snapshot(account_id), conv_id, msg_id, account_id)
    dec = (await session.execute(select(models.ReplyDecision))).scalar_one()
    assert dec.action == "auto_reply"  # Stub LLM
    assert "KNOWLEDGE_HIT" in dec.reason_codes


# ---------- 混合检索：词法召回 + RRF 融合 + verbatim 安全闸门 ----------


async def test_词法路命中向量漏掉的关键词(session):
    from social_reply.application.knowledge.retrieval import retrieve_hybrid_knowledge

    # 查询含专有名词 "pip"，与库中问题精确共词；用一个几乎正交的查询向量，
    # 使向量路给不出高相似度，靠词法路把它召回（RRF 融合仍应命中）。
    await _seed_chunk(session, "What is pip")
    off_vec = (await _EMBEDDER.embed(["完全不相干的中文长句用于压低向量相似度"]))[0]
    hits = await retrieve_hybrid_knowledge(
        session,
        off_vec,
        "pip",
        tenant_id="default",
        brand_id="b1",
        platform="telegram",
        embedding_version=_EMBEDDER.version,
        top_k=3,
        min_similarity=0.99,  # 卡到极高，向量路必不达标
    )
    assert any(h.content == "What is pip" for h in hits)  # 词法把它召回
    # 向量未达阈值，该命中不得被标记为可原文直答
    pip_hit = next(h for h in hits if h.content == "What is pip")
    assert pip_hit.verbatim_safe is False
    assert pip_hit.is_official_contact is False


async def test_混合检索词法路径排除草稿并传播官方标记(session):
    from social_reply.application.knowledge.retrieval import retrieve_hybrid_knowledge

    await _seed_chunk(session, "official pip contact", is_official_contact=True)
    await _seed_chunk(session, "draft pip contact", status="draft", is_official_contact=True)
    off_vec = (await _EMBEDDER.embed(["unrelated query"]))[0]
    hits = await retrieve_hybrid_knowledge(
        session,
        off_vec,
        "pip contact",
        tenant_id="default",
        brand_id="b1",
        platform="telegram",
        embedding_version=_EMBEDDER.version,
        top_k=10,
        min_similarity=0.99,
    )
    assert {hit.content for hit in hits} == {"official pip contact"}
    assert hits[0].is_official_contact is True


async def test_verbatim_闸门_低相似度词法命中不原文外发(session, knowledge_enabled):
    """安全回归护栏：词法-only 命中喂 LLM 可以，但绝不能当原文直接发给用户。"""
    knowledge_enabled.setenv("KNOWLEDGE_VERBATIM_REPLY", "true")
    knowledge_enabled.setenv("KNOWLEDGE_MIN_SIMILARITY", "0.99")
    get_settings.cache_clear()
    # 库中问题与消息共词 "营业" 触发词法路，但 Fake 向量相似度达不到 0.99
    await _seed_chunk(session, "营业 时间 政策")
    account_id, conv_id, msg_id = await _seed_conversation(session)
    await runner.run_and_persist_decision(_snapshot(account_id), conv_id, msg_id, account_id)
    dec = (await session.execute(select(models.ReplyDecision))).scalar_one()
    # 不得走 KNOWLEDGE_VERBATIM（那会把模板原文直接外发）
    assert "KNOWLEDGE_VERBATIM" not in dec.reason_codes


async def test_已发布分类官方联系方式精确模板可创建_outbox(session, knowledge_enabled):
    knowledge_enabled.setenv("KNOWLEDGE_VERBATIM_REPLY", "true")
    get_settings.cache_clear()
    reply = "Official site: https://support.example.com/contact"
    await _seed_chunk(
        session,
        "请问几点营业",
        reply=reply,
        is_official_contact=True,
    )
    account_id, conv_id, msg_id = await _seed_conversation(session)
    outbox_id = await runner.run_and_persist_decision(
        _snapshot(account_id), conv_id, msg_id, account_id
    )
    assert outbox_id is not None
    decision = (await session.execute(select(models.ReplyDecision))).scalar_one()
    assert decision.action == "auto_reply"
    assert decision.reply_text == reply
    assert "KNOWLEDGE_VERBATIM" in decision.reason_codes


async def test_同模板未分类为官方联系方式时阻止发送(session, knowledge_enabled):
    knowledge_enabled.setenv("KNOWLEDGE_VERBATIM_REPLY", "true")
    get_settings.cache_clear()
    await _seed_chunk(
        session,
        "请问几点营业",
        reply="Telegram ID: wikifx_support",
        is_official_contact=False,
    )
    account_id, conv_id, msg_id = await _seed_conversation(session)
    outbox_id = await runner.run_and_persist_decision(
        _snapshot(account_id), conv_id, msg_id, account_id
    )
    assert outbox_id is None
    decision = (await session.execute(select(models.ReplyDecision))).scalar_one()
    assert decision.action == "handoff"
    assert decision.reply_text is None
    assert "GUARD_PII_LEAK" in decision.reason_codes


async def test_草稿官方联系方式不会被选中或发送(session, knowledge_enabled):
    knowledge_enabled.setenv("KNOWLEDGE_VERBATIM_REPLY", "true")
    knowledge_enabled.setenv("REQUIRE_KNOWLEDGE", "true")
    get_settings.cache_clear()
    await _seed_chunk(
        session,
        "请问几点营业",
        reply="Official support: support@example.com",
        status="draft",
        is_official_contact=True,
    )
    account_id, conv_id, msg_id = await _seed_conversation(session)
    outbox_id = await runner.run_and_persist_decision(
        _snapshot(account_id), conv_id, msg_id, account_id
    )
    assert outbox_id is None
    decision = (await session.execute(select(models.ReplyDecision))).scalar_one()
    assert decision.action == "handoff"
    assert "INSUFFICIENT_KNOWLEDGE" in decision.reason_codes
    assert "KNOWLEDGE_VERBATIM" not in decision.reason_codes


async def test_精确匹配仍优先于混合检索(session, knowledge_enabled):
    """精确匹配命中时短路，similarity=1.0 且 verbatim_safe，可原文直答。"""
    knowledge_enabled.setenv("KNOWLEDGE_VERBATIM_REPLY", "true")
    get_settings.cache_clear()
    await _seed_chunk(session, "请问几点营业")  # 与消息文本完全一致
    account_id, conv_id, msg_id = await _seed_conversation(session)
    await runner.run_and_persist_decision(_snapshot(account_id), conv_id, msg_id, account_id)
    dec = (await session.execute(select(models.ReplyDecision))).scalar_one()
    assert dec.action == "auto_reply"
    assert "KNOWLEDGE_VERBATIM" in dec.reason_codes
    assert dec.reply_text == "请问几点营业"
