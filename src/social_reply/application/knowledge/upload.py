from __future__ import annotations

import csv
import json
import logging
import uuid
from dataclasses import dataclass
from typing import TextIO

from social_reply.application.knowledge.drafts import KnowledgeDraft, build_knowledge_draft
from social_reply.domain.reply.language import assess_knowledge_language

logger = logging.getLogger(__name__)

MAX_KNOWLEDGE_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_IMPORT_ROWS = 2000

_REQUIRED_COLUMNS = frozenset({"question", "reply"})
_OPTIONAL_COLUMNS = frozenset(
    {
        "brand_id",
        "platform",
        "category",
        "is_official_contact",
        "protected_values_json",
    }
)
_ALLOWED_COLUMNS = _REQUIRED_COLUMNS | _OPTIONAL_COLUMNS


@dataclass(frozen=True)
class ParsedKnowledgeUpload:
    rows: tuple[KnowledgeDraft, ...]
    blank_count: int

    @property
    def total_count(self) -> int:
        return len(self.rows) + self.blank_count


def decode_knowledge_csv_upload(raw: bytes) -> str:
    if len(raw) > MAX_KNOWLEDGE_UPLOAD_BYTES:
        raise ValueError("knowledge CSV upload cannot exceed 2 MiB")
    if not raw:
        raise ValueError("knowledge CSV upload is empty")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("knowledge CSV upload must be UTF-8") from exc


def parse_optional_bool(value: str | None) -> bool:
    normalized = (value or "").strip().casefold()
    if normalized in {"", "false", "0", "no"}:
        return False
    if normalized in {"true", "1", "yes"}:
        return True
    raise ValueError(f"is_official_contact must be true/false, got {value!r}")


def parse_protected_values(value: str | None) -> tuple[str, ...]:
    if not (value or "").strip():
        return ()
    try:
        parsed = json.loads(value or "")
    except json.JSONDecodeError as exc:
        raise ValueError("protected_values_json must be a JSON string array") from exc
    if not isinstance(parsed, list) or any(not isinstance(item, str) for item in parsed):
        raise ValueError("protected_values_json must be a JSON string array")
    return tuple(parsed)


def parse_knowledge_csv_rows(
    stream: TextIO,
    *,
    tenant_id: str,
    brand_id_default: str,
    source_name: str,
    batch_id: uuid.UUID,
) -> ParsedKnowledgeUpload:
    reader = csv.DictReader(stream)
    fieldnames = reader.fieldnames or []
    if len(fieldnames) != len(set(fieldnames)):
        raise ValueError("duplicate CSV columns are not allowed")
    headers = set(fieldnames)
    missing = _REQUIRED_COLUMNS - headers
    if missing:
        missing_columns = ", ".join(sorted(missing))
        raise ValueError(f"CSV 表头缺少必需列: {missing_columns}（必需 question,reply）")
    unexpected = headers - _ALLOWED_COLUMNS
    if unexpected:
        unexpected_columns = ", ".join(sorted(unexpected))
        raise ValueError(f"unexpected CSV columns: {unexpected_columns}")

    rows: list[KnowledgeDraft] = []
    blank_count = 0
    for row_number, raw_row in enumerate(reader, start=2):
        if row_number - 1 > MAX_IMPORT_ROWS:
            raise ValueError(f"CSV 超过上限 {MAX_IMPORT_ROWS} 行（当前至少 {row_number - 1} 行）")
        if None in raw_row:
            raise ValueError(f"CSV row {row_number} contains unexpected extra columns")
        question = (raw_row.get("question") or "").strip()
        reply = (raw_row.get("reply") or "").strip()
        if not question or not reply:
            blank_count += 1
            logger.warning("Skipping blank knowledge CSV row: row=%d", row_number)
            continue
        detected_language, detection_status = assess_knowledge_language(question, reply)
        rows.append(
            build_knowledge_draft(
                tenant_id=tenant_id,
                question=question,
                reply=reply,
                brand_id=(raw_row.get("brand_id") or "").strip() or brand_id_default,
                platform=(raw_row.get("platform") or "").strip() or None,
                category=(raw_row.get("category") or "").strip() or None,
                is_official_contact=parse_optional_bool(raw_row.get("is_official_contact")),
                protected_values=parse_protected_values(raw_row.get("protected_values_json")),
                detected_language=detected_language,
                language_detection_status=detection_status,
                source_file=source_name,
                import_batch_id=batch_id,
            )
        )
    return ParsedKnowledgeUpload(rows=tuple(rows), blank_count=blank_count)
