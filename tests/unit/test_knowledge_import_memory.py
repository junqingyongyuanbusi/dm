import io
import weakref
from array import array
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.application.knowledge import commands, importer
from social_reply.application.knowledge.authorization import _trusted_system_import_capability
from social_reply.application.knowledge.commands import (
    ImportKnowledgeBatchCommand,
    KnowledgeValidationError,
)
from social_reply.application.knowledge.importer import _import_knowledge_rows_system


class _TrackedVector(list[float]):
    pass


class _TrackedBatch(list[list[float]]):
    pass


def _embedding_values(question: str) -> list[float]:
    return [float(question.removeprefix("question-")), 0.1, 1.0000000000000002, -0.0, 1e-310]


@pytest.fixture
def import_command() -> ImportKnowledgeBatchCommand:
    return ImportKnowledgeBatchCommand(
        required_tenant_id="tenant-a",
        principal=None,
        system_import_capability=_trusted_system_import_capability(),
        source_name="memory.csv",
        csv_text="question,reply\n"
        + "".join(f"question-{index},reply-{index}\n" for index in range(5)),
    )


@pytest.fixture
def import_session(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    monkeypatch.setattr(commands, "_EMBED_BATCH_SIZE", 2)
    monkeypatch.setattr(
        commands, "existing_content_hashes", AsyncMock(side_effect=[set(), set()])
    )
    monkeypatch.setattr(commands, "acquire_xact_lock", AsyncMock())
    monkeypatch.setattr(
        commands, "_persist_knowledge_draft_with_duplicate_fallback", AsyncMock()
    )
    return MagicMock(spec=AsyncSession)


async def test_compact_embeddings_release_source_lists_and_preserve_precision_and_lock_order(
    monkeypatch, import_command, import_session
):
    events: list[str] = []
    source_references: list[weakref.ReferenceType[object]] = []
    packed_vectors: list[array] = []
    lookup_requests: list[list[str]] = []

    def assert_source_lists_released() -> None:
        assert all(reference() is None for reference in source_references)

    class RecordingArray(array):
        def tolist(self) -> list[float]:
            events.append("expand")
            return super().tolist()

    def pack_vector(typecode: str, values: list[float]) -> array:
        packed_vector = RecordingArray(typecode, values)
        packed_vectors.append(packed_vector)
        return packed_vector

    class TrackingEmbedder:
        version = "memory-test"

        async def embed(self, texts: list[str]) -> list[list[float]]:
            assert_source_lists_released()
            events.append("embed")
            vectors = _TrackedBatch(_TrackedVector(_embedding_values(text)) for text in texts)
            source_references.append(weakref.ref(vectors))
            source_references.extend(weakref.ref(vector) for vector in vectors)
            return vectors

    async def lookup_hashes(session, *, tenant_id, content_hashes):
        events.append("lookup")
        assert tenant_id == import_command.required_tenant_id
        lookup_requests.append(content_hashes)
        return set() if len(lookup_requests) == 1 else {content_hashes[2]}

    async def record_lock(session, lock_key):
        assert_source_lists_released()
        events.append("lock")

    monkeypatch.setattr(commands, "array", pack_vector, raising=False)
    commands.existing_content_hashes.side_effect = lookup_hashes
    commands.acquire_xact_lock.side_effect = record_lock
    persistence = commands._persist_knowledge_draft_with_duplicate_fallback
    persistence.side_effect = lambda *arguments, **keywords: events.append("persist")

    report = await commands.execute_import_knowledge_batch(
        import_session, import_command, embedder=TrackingEmbedder()
    )

    assert_source_lists_released()
    assert len(packed_vectors) == 5
    assert all(vector.typecode == "d" for vector in packed_vectors)
    assert events == ["lookup"] + ["embed"] * 3 + ["lock"] * 5 + ["lookup"] + [
        "expand", "persist"
    ] * 4
    assert lookup_requests[0] == lookup_requests[1]
    assert [call.args[1] for call in commands.acquire_xact_lock.await_args_list] == [
        commands.knowledge_content_hash_lock_key(import_command.required_tenant_id, content_hash)
        for content_hash in sorted(lookup_requests[0])
    ]
    for call, index in zip(persistence.await_args_list, (0, 1, 3, 4), strict=True):
        assert call.args[0] is import_session
        assert call.args[1].question == f"question-{index}"
        embedding = call.kwargs["embedding"]
        assert type(embedding) is list
        assert [value.hex() for value in embedding] == [
            value.hex() for value in _embedding_values(f"question-{index}")
        ]
        assert call.kwargs["embedding_version"] == TrackingEmbedder.version
        assert call.kwargs["actor"] == "system:knowledge-import"
    assert (report.inserted, report.skipped, report.total) == (4, 1, 5)
    audit = import_session.add.call_args.args[0]
    assert audit.action == "IMPORT_KNOWLEDGE_BATCH"
    assert audit.detail["inserted_count"] == 4
    assert audit.detail["skipped_count"] == 1


@pytest.mark.parametrize(
    ("returned_counts", "expected_calls"),
    [((1, 3, 1), 1), ((3, 1, 1), 1), ((2, 1, 2), 2), ((2, 3, 0), 2)],
)
async def test_embedding_counts_cannot_cancel_across_batches(
    import_command, import_session, returned_counts, expected_calls
):
    embedder = SimpleNamespace(
        version="memory-test",
        embed=AsyncMock(side_effect=[[[0.1]] * count for count in returned_counts]),
    )

    with pytest.raises(KnowledgeValidationError) as error:
        await commands.execute_import_knowledge_batch(
            import_session, import_command, embedder=embedder
        )

    assert error.value.code == "knowledge_embedding_count_invalid"
    assert embedder.embed.await_count == expected_calls
    assert commands.existing_content_hashes.await_count == 1
    commands.acquire_xact_lock.assert_not_awaited()
    commands._persist_knowledge_draft_with_duplicate_fallback.assert_not_awaited()
    import_session.add.assert_not_called()
    import_session.flush.assert_not_awaited()


async def test_failed_later_embedding_batch_does_not_lock_or_write(import_command, import_session):
    provider_error = RuntimeError("embedding provider unavailable")
    embedder = SimpleNamespace(
        version="memory-test",
        embed=AsyncMock(side_effect=[[[0.1], [0.2]], provider_error]),
    )

    with pytest.raises(RuntimeError) as error:
        await commands.execute_import_knowledge_batch(
            import_session, import_command, embedder=embedder
        )

    assert error.value is provider_error
    assert embedder.embed.await_count == 2
    assert commands.existing_content_hashes.await_count == 1
    commands.acquire_xact_lock.assert_not_awaited()
    commands._persist_knowledge_draft_with_duplicate_fallback.assert_not_awaited()
    import_session.add.assert_not_called()
    import_session.flush.assert_not_awaited()


class _BoundedTextStream(io.StringIO):
    def __init__(self, text: str, expected_read_size: int) -> None:
        super().__init__(text)
        self.expected_read_size = expected_read_size
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> str:
        assert 0 < size <= self.expected_read_size
        self.read_sizes.append(size)
        return super().read(size)


@pytest.mark.parametrize(
    "csv_text",
    ["x" * 10000, "question,reply\nq," + "\u4e2d" * 16 + "\n"],
    ids=["bounded-ascii-read", "utf8-bytes-exceed-character-count"],
)
async def test_oversized_stream_is_rejected_before_creating_session(monkeypatch, csv_text):
    byte_limit = 64
    monkeypatch.setattr(importer, "MAX_KNOWLEDGE_UPLOAD_BYTES", byte_limit, raising=False)
    stream = _BoundedTextStream(csv_text, byte_limit + 1)
    session_factory = MagicMock(side_effect=AssertionError("must not create a session"))
    monkeypatch.setattr(importer, "get_session_factory", session_factory)
    embedder = SimpleNamespace(version="memory-test", embed=AsyncMock())

    with pytest.raises(KnowledgeValidationError) as error:
        await _import_knowledge_rows_system(stream, source_name="large.csv", embedder=embedder)

    assert error.value.code == "knowledge_csv_too_large"
    assert stream.read_sizes == [byte_limit + 1]
    assert stream.tell() == min(len(csv_text), byte_limit + 1)
    session_factory.assert_not_called()
    embedder.embed.assert_not_awaited()


@pytest.mark.parametrize("reply", ["answer", "\u4e2d\u6587"], ids=["ascii", "utf8"])
async def test_stream_at_exact_utf8_byte_limit_imports_and_commits(
    monkeypatch, import_session, reply
):
    csv_text = f"question,reply\nquestion-0,{reply}\n"
    byte_limit = len(csv_text.encode("utf-8"))
    monkeypatch.setattr(importer, "MAX_KNOWLEDGE_UPLOAD_BYTES", byte_limit, raising=False)
    monkeypatch.setattr(commands, "MAX_KNOWLEDGE_UPLOAD_BYTES", byte_limit)
    stream = _BoundedTextStream(csv_text, byte_limit + 1)
    import_session.__aenter__.return_value = import_session
    session_factory = MagicMock(return_value=MagicMock(return_value=import_session))
    monkeypatch.setattr(importer, "get_session_factory", session_factory)
    embedder = SimpleNamespace(version="memory-test", embed=AsyncMock(return_value=[[0.1]]))

    report = await _import_knowledge_rows_system(
        stream, source_name="boundary.csv", embedder=embedder, tenant_id="tenant-a"
    )

    assert report.inserted == 1
    assert stream.read_sizes == [byte_limit + 1, 1]
    session_factory.assert_called_once_with()
    import_session.commit.assert_awaited_once_with()
    draft = commands._persist_knowledge_draft_with_duplicate_fallback.await_args.args[1]
    assert draft.reply == reply
    assert draft.tenant_id == "tenant-a"


class _ShortReadTextStream(_BoundedTextStream):
    def read(self, size: int = -1) -> str:
        assert 0 < size <= self.expected_read_size
        self.read_sizes.append(size)
        return io.StringIO.read(self, min(size, 33))


async def test_short_read_does_not_silently_commit_only_the_first_csv_record(
    monkeypatch, import_session
):
    csv_text = "question,reply\nquestion-0,reply-0\nquestion-1,reply-1\n"
    byte_limit = len(csv_text.encode("utf-8"))
    stream = _ShortReadTextStream(csv_text, byte_limit + 1)
    monkeypatch.setattr(importer, "MAX_KNOWLEDGE_UPLOAD_BYTES", byte_limit)
    import_session.__aenter__.return_value = import_session
    monkeypatch.setattr(importer, "get_session_factory", lambda: lambda: import_session)
    embedder = SimpleNamespace(version="memory-test", embed=AsyncMock(return_value=[[0.1], [0.2]]))

    report = await _import_knowledge_rows_system(
        stream,
        source_name="short.csv",
        embedder=embedder,
    )

    assert report.inserted == 2
    assert len(stream.read_sizes) >= 3
    embedder.embed.assert_awaited_once_with(["question-0", "question-1"])
    import_session.commit.assert_awaited_once_with()


async def test_short_reads_still_enforce_total_byte_limit_before_creating_session(monkeypatch):
    byte_limit = 64
    stream = _ShortReadTextStream("question,reply\nq,r\n" + "x" * 100, byte_limit + 1)
    monkeypatch.setattr(importer, "MAX_KNOWLEDGE_UPLOAD_BYTES", byte_limit)
    session_factory = MagicMock(side_effect=AssertionError("must not create a session"))
    monkeypatch.setattr(importer, "get_session_factory", session_factory)
    embedder = SimpleNamespace(version="memory-test", embed=AsyncMock())

    with pytest.raises(KnowledgeValidationError, match="knowledge_csv_too_large"):
        await _import_knowledge_rows_system(
            stream,
            source_name="short-large.csv",
            embedder=embedder,
        )

    assert stream.tell() == byte_limit + 1
    session_factory.assert_not_called()
    embedder.embed.assert_not_awaited()
