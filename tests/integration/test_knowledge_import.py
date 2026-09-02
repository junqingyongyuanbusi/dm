"""回复模板 CSV 导入集成测试（Fake embedder）"""

import io

import pytest
from sqlalchemy import select

from social_reply.application.knowledge.importer import (
    MAX_IMPORT_ROWS,
    import_knowledge_csv,
    import_knowledge_rows,
)
from social_reply.application.knowledge.upload import (
    MAX_KNOWLEDGE_UPLOAD_BYTES,
    decode_knowledge_csv_upload,
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
    assert {audit.action for audit in audits} >= {
        "CREATE_KNOWLEDGE_DOCUMENT",
        "IMPORT_KNOWLEDGE_BATCH",
    }
    if expected:
        official_audit = next(
            audit for audit in audits if audit.action == "SET_KNOWLEDGE_OFFICIAL_CONTACT"
        )
        assert official_audit.actor == "knowledge-import"
        assert official_audit.detail["content_hash"]
    else:
        assert "SET_KNOWLEDGE_OFFICIAL_CONTACT" not in {audit.action for audit in audits}


async def test_官方联系方式无效布尔值报错(migrated_db):
    with pytest.raises(ValueError, match="is_official_contact"):
        await import_knowledge_rows(
            io.StringIO("question,reply,is_official_contact\nq,r,maybe\n"),
            source_name="invalid.csv",
            embedder=FakeEmbeddingClient(),
        )


async def test_protected_values_are_tenant_knowledge_metadata(migrated_db, session):
    csv_text = (
        "question,reply,protected_values_json\n"
        'platform,Use Acme Portal with MT4.,"[""Acme Portal"", ""MT4""]"\n'
    )
    await import_knowledge_rows(
        io.StringIO(csv_text),
        source_name="protected.csv",
        embedder=FakeEmbeddingClient(),
    )

    doc = (await session.execute(select(KnowledgeDocument))).scalar_one()
    assert doc.protected_values == ["Acme Portal", "MT4"]


async def test_protected_value_revision_is_not_skipped_as_duplicate(migrated_db, session):
    first = (
        "question,reply,protected_values_json\n"
        'platform,Use Acme Portal with MT4.,"[""Acme Portal""]"\n'
    )
    second = (
        'question,reply,protected_values_json\nplatform,Use Acme Portal with MT4.,"[""MT4""]"\n'
    )

    first_report = await import_knowledge_rows(
        io.StringIO(first),
        source_name="protected-v1.csv",
        embedder=FakeEmbeddingClient(),
    )
    second_report = await import_knowledge_rows(
        io.StringIO(second),
        source_name="protected-v2.csv",
        embedder=FakeEmbeddingClient(),
    )

    assert first_report.inserted == 1
    assert second_report.inserted == 1
    documents = (await session.execute(select(KnowledgeDocument))).scalars().all()
    chunks = (await session.execute(select(KnowledgeChunk))).scalars().all()
    assert {tuple(document.protected_values) for document in documents} == {
        ("Acme Portal",),
        ("MT4",),
    }
    assert len({chunk.content_hash for chunk in chunks}) == 2


@pytest.mark.parametrize(
    "value",
    ['{"not":"an-array"}', '["missing"]', "[1]"],
)
async def test_invalid_protected_values_are_rejected(migrated_db, value):
    escaped_value = value.replace('"', '""')
    csv_text = f'question,reply,protected_values_json\nq,approved,"{escaped_value}"\n'
    with pytest.raises(ValueError, match="protected"):
        await import_knowledge_rows(
            io.StringIO(csv_text),
            source_name="invalid-protected.csv",
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


async def test_csv_unknown_columns_fail_closed(migrated_db):
    with pytest.raises(ValueError, match="unexpected CSV columns"):
        await import_knowledge_rows(
            io.StringIO("question,reply,secret_field\nq,r,do-not-accept\n"),
            source_name="unexpected.csv",
            embedder=FakeEmbeddingClient(),
        )


async def test_csv_extra_data_columns_fail_closed(migrated_db):
    with pytest.raises(ValueError, match="unexpected extra columns"):
        await import_knowledge_rows(
            io.StringIO("question,reply\nq,r,hidden\n"),
            source_name="extra-values.csv",
            embedder=FakeEmbeddingClient(),
        )


async def test_csv_manual_symmetric_scope_fields_are_persisted(migrated_db, session):
    csv_text = (
        "question,reply,brand_id,platform,category,is_official_contact,"
        "protected_values_json\n"
        'How to sign in?,Use Acme Portal.,retail,telegram,account,false,"[""Acme Portal""]"\n'
    )
    report = await import_knowledge_rows(
        io.StringIO(csv_text),
        source_name="symmetric.csv",
        embedder=FakeEmbeddingClient(),
        actor="user:knowledge-admin",
    )
    assert report.inserted == 1
    document = (await session.execute(select(KnowledgeDocument))).scalar_one()
    assert document.brand_id == "retail"
    assert document.platform == "telegram"
    assert document.category == "account"
    assert document.protected_values == ["Acme Portal"]
    audits = (await session.execute(select(AuditLog))).scalars().all()
    assert {audit.action for audit in audits} >= {
        "CREATE_KNOWLEDGE_DOCUMENT",
        "IMPORT_KNOWLEDGE_BATCH",
    }
    assert all("How to sign in?" not in str(audit.detail) for audit in audits)
    assert all("Use Acme Portal." not in str(audit.detail) for audit in audits)
    assert all("Acme Portal" not in str(audit.detail) for audit in audits)


def test_csv_upload_boundary_requires_utf8_and_two_mib_limit() -> None:
    assert decode_knowledge_csv_upload(b"question,reply\nq,r\n") == "question,reply\nq,r\n"
    with pytest.raises(ValueError, match="UTF-8"):
        decode_knowledge_csv_upload(b"\xff")
    with pytest.raises(ValueError, match="2 MiB"):
        decode_knowledge_csv_upload(b"x" * (MAX_KNOWLEDGE_UPLOAD_BYTES + 1))
