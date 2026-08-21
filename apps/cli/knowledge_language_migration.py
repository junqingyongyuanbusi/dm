"""Inventory and apply reviewed language decisions for the canonical English knowledge base."""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import uuid
from pathlib import Path

from sqlalchemy import select

from social_reply.application.knowledge.readiness import (
    assert_knowledge_readiness,
    knowledge_readiness_report,
)
from social_reply.domain.reply.language import assess_knowledge_language
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.shared.config import get_settings

_FIELDNAMES = (
    "id",
    "tenant_id",
    "brand_id",
    "platform",
    "status",
    "is_official_contact",
    "updated_at",
    "review_fingerprint",
    "source_file",
    "detected_language",
    "detection_status",
    "question",
    "reply",
    "decision",
    "review_reason",
    "replacement_id",
)


def _spreadsheet_safe(value: str) -> str:
    return f"'{value}" if value.startswith(("=", "+", "-", "@")) else value


def _spreadsheet_value(value: str) -> str:
    return (
        value[1:] if value.startswith("'") and value[1:].startswith(("=", "+", "-", "@")) else value
    )


def _review_fingerprint(doc: models.KnowledgeDocument) -> str:
    payload = json.dumps(
        {
            "tenant_id": doc.tenant_id,
            "brand_id": doc.brand_id,
            "platform": doc.platform,
            "question": doc.question,
            "reply": doc.reply,
            "is_official_contact": doc.is_official_contact,
            "status": doc.status,
            "updated_at": doc.updated_at.isoformat() if doc.updated_at else None,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _review_fingerprint_from_row(row: dict[str, str]) -> str:
    payload = json.dumps(
        {
            "tenant_id": _spreadsheet_value(row.get("tenant_id") or ""),
            "brand_id": _spreadsheet_value(row.get("brand_id") or ""),
            "platform": _spreadsheet_value((row.get("platform") or "").strip()) or None,
            "question": _spreadsheet_value(row.get("question") or ""),
            "reply": _spreadsheet_value(row.get("reply") or ""),
            "is_official_contact": (row.get("is_official_contact") or "").casefold() == "true",
            "status": row.get("status") or "",
            "updated_at": (row.get("updated_at") or "").strip() or None,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


async def inventory(output: Path) -> None:
    async with get_session_factory()() as session:
        rows = (
            (
                await session.execute(
                    select(models.KnowledgeDocument).order_by(
                        models.KnowledgeDocument.tenant_id,
                        models.KnowledgeDocument.brand_id,
                        models.KnowledgeDocument.created_at,
                    )
                )
            )
            .scalars()
            .all()
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_FIELDNAMES)
        writer.writeheader()
        for doc in rows:
            detected_language, detection_status = assess_knowledge_language(
                doc.question,
                doc.reply,
            )
            counts[detection_status] = counts.get(detection_status, 0) + 1
            writer.writerow(
                {
                    "id": str(doc.id),
                    "tenant_id": _spreadsheet_safe(doc.tenant_id),
                    "brand_id": _spreadsheet_safe(doc.brand_id),
                    "platform": _spreadsheet_safe(doc.platform or ""),
                    "status": doc.status,
                    "is_official_contact": str(doc.is_official_contact).lower(),
                    "updated_at": doc.updated_at.isoformat() if doc.updated_at else "",
                    "review_fingerprint": _review_fingerprint(doc),
                    "source_file": _spreadsheet_safe(doc.source_file or ""),
                    "detected_language": detected_language,
                    "detection_status": detection_status,
                    "question": _spreadsheet_safe(doc.question),
                    "reply": _spreadsheet_safe(doc.reply),
                    "decision": "",
                    "review_reason": "",
                    "replacement_id": "",
                }
            )
    print(
        json.dumps(
            {"output": str(output), "total": len(rows), "counts": counts}, ensure_ascii=False
        )
    )


def _load_reviewed_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = set(_FIELDNAMES) - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"review file missing columns: {sorted(missing)}")
        return [dict(row) for row in reader]


async def apply_review(path: Path, *, actor: str) -> None:
    reviewed = _load_reviewed_rows(path)
    confirmation_batch_id = str(uuid.uuid4())
    counts = {"confirmed": 0, "unpublished": 0, "skipped": 0}
    async with get_session_factory()() as session:
        for row in reviewed:
            decision = (row.get("decision") or "").strip().casefold()
            if decision in {"", "skip"}:
                counts["skipped"] += 1
                continue
            try:
                document_id = uuid.UUID((row.get("id") or "").strip())
            except ValueError as exc:
                raise ValueError(f"invalid knowledge id: {row.get('id')!r}") from exc
            doc = (
                await session.execute(
                    select(models.KnowledgeDocument)
                    .where(models.KnowledgeDocument.id == document_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if doc is None:
                raise ValueError(f"knowledge document not found: {document_id}")
            reviewed_fingerprint = (row.get("review_fingerprint") or "").strip()
            row_fingerprint = _review_fingerprint_from_row(row)
            if not reviewed_fingerprint or row_fingerprint != reviewed_fingerprint:
                raise ValueError(f"review row content does not match fingerprint: {document_id}")
            applied_fingerprint = _review_fingerprint(doc)
            if not reviewed_fingerprint or reviewed_fingerprint != applied_fingerprint:
                raise ValueError(
                    f"knowledge document changed after review: {document_id}; re-run inventory"
                )
            previous_status = doc.status
            detected_language, detection_status = assess_knowledge_language(doc.question, doc.reply)
            doc.detected_language = detected_language
            doc.language_detection_status = detection_status
            if decision == "confirm_english":
                reason = (row.get("review_reason") or "").strip()
                if detection_status in {"mixed", "non_english"}:
                    raise ValueError(
                        f"{document_id} is {detection_status}; "
                        "create a reviewed English replacement"
                    )
                if detection_status == "unknown" and len(reason) < 10:
                    raise ValueError(f"{document_id} unknown language requires review_reason")
                doc.source_language = "en"
                doc.language_verified = True
                doc.status = "published"
                counts["confirmed"] += 1
                action = "CONFIRM_KNOWLEDGE_ENGLISH_MIGRATION"
            elif decision == "unpublish":
                replacement_value = (row.get("replacement_id") or "").strip()
                reason = (row.get("review_reason") or "").strip()
                if not replacement_value:
                    if len(reason) < 10:
                        raise ValueError(
                            f"{document_id} unpublish without replacement requires review_reason"
                        )
                else:
                    try:
                        replacement_id = uuid.UUID(replacement_value)
                    except ValueError as exc:
                        raise ValueError(
                            f"{document_id} unpublish requires a valid replacement_id"
                        ) from exc
                    if replacement_id == document_id:
                        raise ValueError(f"{document_id} cannot replace itself")
                    replacement = (
                        await session.execute(
                            select(models.KnowledgeDocument)
                            .where(models.KnowledgeDocument.id == replacement_id)
                            .with_for_update()
                        )
                    ).scalar_one_or_none()
                    if replacement is None:
                        raise ValueError(f"replacement not found: {replacement_id}")
                    platform_covered = (
                        replacement.platform is None
                        if doc.platform is None
                        else replacement.platform in {None, doc.platform}
                    )
                    if (
                        replacement.tenant_id != doc.tenant_id
                        or replacement.brand_id != doc.brand_id
                        or not platform_covered
                        or replacement.status != "published"
                        or replacement.source_language != "en"
                        or not replacement.language_verified
                    ):
                        raise ValueError(
                            f"replacement {replacement_id} must be published verified English "
                            "knowledge in the same tenant/brand/platform scope"
                        )
                if doc.status == "published":
                    doc.status = "draft"
                counts["unpublished"] += 1
                action = "UNPUBLISH_NON_ENGLISH_KNOWLEDGE_MIGRATION"
            else:
                raise ValueError(f"unsupported decision for {document_id}: {decision}")
            session.add(
                models.AuditLog(
                    tenant_id=doc.tenant_id,
                    category="admin_action",
                    actor=actor,
                    action=action,
                    subject_type="knowledge_document",
                    subject_id=str(doc.id),
                    detail={
                        "confirmation_batch_id": confirmation_batch_id,
                        "reviewed_fingerprint": reviewed_fingerprint,
                        "applied_fingerprint": applied_fingerprint,
                        "detected_language": detected_language,
                        "detection_status": detection_status,
                        "review_reason": reason,
                        "replacement_id": (row.get("replacement_id") or "").strip() or None,
                        "previous_status": previous_status,
                        "new_status": doc.status
                    },
                )
            )
        await session.commit()
    print(
        json.dumps(
            {"confirmation_batch_id": confirmation_batch_id, "counts": counts},
            ensure_ascii=False,
        )
    )


async def _readiness_report() -> dict:
    async with get_session_factory()() as session:
        return await knowledge_readiness_report(
            session,
            expected_embedding_version=get_settings().openai_embedding_model,
        )


async def fingerprint() -> None:
    print(json.dumps(await _readiness_report(), ensure_ascii=False))


async def readiness() -> None:
    report = await _readiness_report()
    print(json.dumps(report, ensure_ascii=False))
    try:
        assert_knowledge_readiness(report)
        expected_corpus = get_settings().knowledge_corpus_version.strip()
        if (
            expected_corpus not in {"", "unversioned"}
            and report["corpus_fingerprint"] != expected_corpus
        ):
            raise RuntimeError(
                "knowledge corpus fingerprint does not match KNOWLEDGE_CORPUS_VERSION"
            )
    except RuntimeError as exc:
        raise SystemExit(1) from exc


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    inventory_parser = subparsers.add_parser("inventory")
    inventory_parser.add_argument("--output", type=Path, required=True)
    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("--input", type=Path, required=True)
    apply_parser.add_argument("--actor", required=True)
    subparsers.add_parser("fingerprint")
    subparsers.add_parser("readiness")
    args = parser.parse_args()
    if args.command == "inventory":
        asyncio.run(inventory(args.output))
    elif args.command == "apply":
        asyncio.run(apply_review(args.input, actor=args.actor))
    elif args.command == "fingerprint":
        asyncio.run(fingerprint())
    else:
        asyncio.run(readiness())


if __name__ == "__main__":
    main()
