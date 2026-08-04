"""回复模板 CSV 导入集成测试（Fake embedder）"""

import io

import pytest
from sqlalchemy import select

from social_reply.application.knowledge.importer import (
    MAX_IMPORT_ROWS,
    import_knowledge_csv,
    import_knowledge_rows,
)
from social_reply.domain.knowledge.embeddings import FakeEmbeddingClient
from social_reply.infrastructure.database.models import AuditLog, KnowledgeChunk, KnowledgeDocument

_CSV = (
    "question,reply,category\n"
    "怎么修改绑定邮箱,您好！请在 App「设置-账号安全」中操作,账号\n"
    "\n"
    "退款多久到账,您好，退款一般 3-5 个工作日原路退回,售后\n"
    "发货多久,一般 48 小时内发货,物流\n"
)


@pytest.fixture
def csv_file(tmp_path):
    path = tmp_path / "模板.csv"
    path.write_text(_CSV, encoding="utf-8")
    return path


async def test_导入三行并幂等重复跳过(migrated_db, session, csv_file):
    report = await import_knowledge_csv(csv_file, embedder=FakeEmbeddingClient())
    assert (report.inserted, report.skipped, report.total) == (3, 0, 3)

    docs = (await session.execute(select(KnowledgeDocument))).scalars().all()
    chunks = (await session.execute(select(KnowledgeChunk))).scalars().all()
    assert len(docs) == 3
    assert len(chunks) == 3
    assert all(len(c.embedding) == 1536 for c in chunks)
    assert {d.category for d in docs} == {"账号", "售后", "物流"}
    assert {d.status for d in docs} == {"draft"}
    assert {d.is_official_contact for d in docs} == {False}

    # 非对称嵌入：embed_text 只存 question（不含答案），content 仍是问+答拼接
    by_q = {d.question: d for d in docs}
    q = "怎么修改绑定邮箱"
    doc = by_q[q]
    chunk = next(c for c in chunks if c.document_id == doc.id)
    assert chunk.embed_text == q  # 只嵌入问题
    assert "答" not in chunk.embed_text  # 答案未混入向量文本
    assert chunk.content.startswith("问：") and "答：" in chunk.content  # 展示文本仍含答案

    # 重复导入：content_hash 幂等，全部 skip 且不新增
    report2 = await import_knowledge_csv(csv_file, embedder=FakeEmbeddingClient())
    assert (report2.inserted, report2.skipped) == (0, 3)
    docs2 = (await session.execute(select(KnowledgeDocument))).scalars().all()
    assert len(docs2) == 3


async def test_文本流入口基本导入(migrated_db, session):
    report = await import_knowledge_rows(
        io.StringIO(_CSV),
        source_name="stream.csv",
        embedder=FakeEmbeddingClient(),
    )
    assert (report.inserted, report.skipped, report.blank) == (3, 0, 0)
    docs = (await session.execute(select(KnowledgeDocument))).scalars().all()
    assert len(docs) == 3
    assert all(d.source_file == "stream.csv" for d in docs)


async def test_空行与空字段跳过计数(migrated_db, tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("question,reply\nq1,r1\n,r2\nq3,\n", encoding="utf-8")
    report = await import_knowledge_csv(path, embedder=FakeEmbeddingClient())
    assert report.inserted == 1
    assert report.blank == 2


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("true", True),
        ("TRUE", True),
        ("1", True),
        ("yes", True),
        ("No", False),
        ("0", False),
        ("false", False),
        ("", False),
    ],
)
async def test_官方联系方式布尔列严格解析(migrated_db, session, value, expected):
    csv_text = f"question,reply,is_official_contact\nq-{value or 'blank'},r,{value}\n"
    await import_knowledge_rows(
        io.StringIO(csv_text),
        source_name="official.csv",
        embedder=FakeEmbeddingClient(),
    )
    doc = (await session.execute(select(KnowledgeDocument))).scalar_one()
    assert doc.status == "draft"
    assert doc.is_official_contact is expected
    audits = (await session.execute(select(AuditLog))).scalars().all()
    assert len(audits) == int(expected)
    if expected:
        assert audits[0].actor == "knowledge-import"
        assert audits[0].action == "SET_KNOWLEDGE_OFFICIAL_CONTACT"
        assert audits[0].detail["content_hash"]


async def test_官方联系方式无效布尔值报错(migrated_db):
    with pytest.raises(ValueError, match="is_official_contact"):
        await import_knowledge_rows(
            io.StringIO("question,reply,is_official_contact\nq,r,maybe\n"),
            source_name="invalid.csv",
            embedder=FakeEmbeddingClient(),
        )


async def test_缺表头中文报错(migrated_db, tmp_path):
    path = tmp_path / "missing.csv"
    path.write_text("q,a\nx,y\n", encoding="utf-8")
    with pytest.raises(ValueError, match="表头"):
        await import_knowledge_csv(path, embedder=FakeEmbeddingClient())


async def test_超行数上限报错(migrated_db):
    lines = ["question,reply"] + [f"q{i},r{i}" for i in range(MAX_IMPORT_ROWS + 1)]
    with pytest.raises(ValueError, match="上限"):
        await import_knowledge_rows(
            io.StringIO("\n".join(lines) + "\n"),
            source_name="too-many.csv",
            embedder=FakeEmbeddingClient(),
        )
