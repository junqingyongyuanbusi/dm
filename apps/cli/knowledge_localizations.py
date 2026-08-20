"""Reviewed knowledge localization lifecycle CLI.

Examples:
  uv run python -m apps.cli.knowledge_localizations import --tenant default \
      --release ja-release-v1 --input ja.csv
  uv run python -m apps.cli.knowledge_localizations publish --tenant default --id <uuid> \
      --reviewer alice --approve-auto-reply
  uv run python -m apps.cli.knowledge_localizations revoke --tenant default --id <uuid> \
      --actor alice --reason "source policy changed"
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import uuid
from pathlib import Path

from sqlalchemy import select

from social_reply.application.knowledge.localizations import (
    LocalizationDraftInput,
    LocalizationValidationError,
    create_localization_draft,
    publish_localization,
    revoke_localization,
)
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory


def _protected_values(raw: str) -> tuple[str, ...]:
    if not raw.strip():
        return ()
    value = json.loads(raw)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("protected_values_json must be a JSON string array")
    return tuple(value)


async def import_csv(path: Path, *, tenant_id: str, release_id: str, actor: str) -> None:
    if not actor.strip():
        raise ValueError("actor is required")
    batch_id = uuid.uuid4()
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    required = {"document_id", "locale", "text"}
    if not rows:
        raise ValueError("localization CSV is empty")
    if not required.issubset(rows[0]):
        raise ValueError(f"localization CSV requires columns: {sorted(required)}")

    created: list[str] = []
    async with get_session_factory()() as session:
        for row in rows:
            artifact = await create_localization_draft(
                session,
                LocalizationDraftInput(
                    tenant_id=tenant_id,
                    document_id=uuid.UUID(row["document_id"].strip()),
                    release_id=release_id,
                    locale=row["locale"],
                    text=row["text"],
                    protected_values=_protected_values(row.get("protected_values_json") or ""),
                    source_file=path.name,
                    import_batch_id=batch_id,
                ),
            )
            created.append(str(artifact.id))
            session.add(
                models.AuditLog(
                    tenant_id=tenant_id,
                    category="admin_action",
                    actor=actor,
                    action="IMPORT_KNOWLEDGE_LOCALIZATION_DRAFT",
                    subject_type="knowledge_localization",
                    subject_id=str(artifact.id),
                    detail={
                        "document_id": str(artifact.document_id),
                        "release_id": artifact.release_id,
                        "locale": artifact.locale,
                        "source_content_hash": artifact.source_content_hash,
                        "import_batch_id": str(batch_id),
                    },
                )
            )
        await session.commit()
    print(json.dumps({"batch_id": str(batch_id), "created_ids": created}, ensure_ascii=False))


async def publish(
    *,
    tenant_id: str,
    artifact_id: uuid.UUID,
    reviewer: str,
    approve_auto_reply: bool,
    approve_official_contact: bool,
) -> None:
    async with get_session_factory()() as session:
        artifact = await publish_localization(
            session,
            tenant_id=tenant_id,
            artifact_id=artifact_id,
            reviewer=reviewer,
            approve_auto_reply=approve_auto_reply,
            approve_official_contact=approve_official_contact,
        )
        session.add(
            models.AuditLog(
                tenant_id=tenant_id,
                category="admin_action",
                actor=reviewer,
                action="PUBLISH_KNOWLEDGE_LOCALIZATION",
                subject_type="knowledge_localization",
                subject_id=str(artifact.id),
                detail={
                    "document_id": str(artifact.document_id),
                    "locale": artifact.locale,
                    "source_content_hash": artifact.source_content_hash,
                    "auto_reply_allowed": artifact.auto_reply_allowed,
                    "official_contact_authorized": artifact.official_contact_authorized,
                },
            )
        )
        await session.commit()
    print(json.dumps({"id": str(artifact_id), "status": "published"}))


async def revoke(
    *,
    tenant_id: str,
    artifact_id: uuid.UUID,
    actor: str,
    reason: str,
) -> None:
    async with get_session_factory()() as session:
        artifact = await revoke_localization(
            session,
            tenant_id=tenant_id,
            artifact_id=artifact_id,
            actor=actor,
            reason=reason,
        )
        session.add(
            models.AuditLog(
                tenant_id=tenant_id,
                category="admin_action",
                actor=actor,
                action="REVOKE_KNOWLEDGE_LOCALIZATION",
                subject_type="knowledge_localization",
                subject_id=str(artifact.id),
                detail={
                    "document_id": str(artifact.document_id),
                    "locale": artifact.locale,
                    "reason": artifact.revoke_reason,
                },
            )
        )
        await session.commit()
    print(json.dumps({"id": str(artifact_id), "status": "revoked"}))


async def export_sources(output: Path, *, tenant_id: str) -> None:
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(
                    models.KnowledgeDocument.id,
                    models.KnowledgeDocument.brand_id,
                    models.KnowledgeDocument.platform,
                    models.KnowledgeDocument.question,
                    models.KnowledgeDocument.reply,
                    models.KnowledgeDocument.is_official_contact,
                    models.KnowledgeChunk.content_hash,
                )
                .join(
                    models.KnowledgeChunk,
                    (models.KnowledgeChunk.tenant_id == models.KnowledgeDocument.tenant_id)
                    & (models.KnowledgeChunk.document_id == models.KnowledgeDocument.id),
                )
                .where(
                    models.KnowledgeDocument.tenant_id == tenant_id,
                    models.KnowledgeDocument.status == "published",
                    models.KnowledgeDocument.source_language == "en",
                    models.KnowledgeDocument.language_verified.is_(True),
                )
                .order_by(models.KnowledgeDocument.brand_id, models.KnowledgeDocument.id)
            )
        ).all()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "document_id",
                "content_hash",
                "brand_id",
                "platform",
                "is_official_contact",
                "question",
                "approved_english_reply",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    str(row.id),
                    row.content_hash,
                    row.brand_id,
                    row.platform or "",
                    row.is_official_contact,
                    row.question,
                    row.reply,
                ]
            )
    print(json.dumps({"output": str(output), "rows": len(rows)}, ensure_ascii=False))


async def list_artifacts(*, tenant_id: str) -> None:
    async with get_session_factory()() as session:
        artifacts = (
            (
                await session.execute(
                    select(models.KnowledgeLocalization)
                    .where(models.KnowledgeLocalization.tenant_id == tenant_id)
                    .order_by(
                        models.KnowledgeLocalization.document_id,
                        models.KnowledgeLocalization.locale,
                        models.KnowledgeLocalization.created_at,
                    )
                )
            )
            .scalars()
            .all()
        )
    print(
        json.dumps(
            [
                {
                    "id": str(artifact.id),
                    "document_id": str(artifact.document_id),
                    "release_id": artifact.release_id,
                    "locale": artifact.locale,
                    "status": artifact.status,
                    "source_content_hash": artifact.source_content_hash,
                    "auto_reply_allowed": artifact.auto_reply_allowed,
                    "official_contact_authorized": artifact.official_contact_authorized,
                    "reviewed_by": artifact.reviewed_by,
                }
                for artifact in artifacts
            ],
            ensure_ascii=False,
            indent=2,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Reviewed knowledge localization lifecycle")
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser("import")
    import_parser.add_argument("--tenant", required=True)
    import_parser.add_argument("--input", type=Path, required=True)
    import_parser.add_argument("--release", required=True)
    import_parser.add_argument("--actor", default="knowledge-localization-import")

    publish_parser = subparsers.add_parser("publish")
    publish_parser.add_argument("--tenant", required=True)
    publish_parser.add_argument("--id", type=uuid.UUID, required=True)
    publish_parser.add_argument("--reviewer", required=True)
    publish_parser.add_argument("--approve-auto-reply", action="store_true")
    publish_parser.add_argument("--approve-official-contact", action="store_true")

    revoke_parser = subparsers.add_parser("revoke")
    revoke_parser.add_argument("--tenant", required=True)
    revoke_parser.add_argument("--id", type=uuid.UUID, required=True)
    revoke_parser.add_argument("--actor", required=True)
    revoke_parser.add_argument("--reason", required=True)

    export_parser = subparsers.add_parser("export-sources")
    export_parser.add_argument("--tenant", required=True)
    export_parser.add_argument("--output", type=Path, required=True)
    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--tenant", required=True)

    args = parser.parse_args()
    try:
        if args.command == "import":
            asyncio.run(
                import_csv(
                    args.input,
                    tenant_id=args.tenant,
                    release_id=args.release,
                    actor=args.actor,
                )
            )
        elif args.command == "publish":
            asyncio.run(
                publish(
                    tenant_id=args.tenant,
                    artifact_id=args.id,
                    reviewer=args.reviewer,
                    approve_auto_reply=args.approve_auto_reply,
                    approve_official_contact=args.approve_official_contact,
                )
            )
        elif args.command == "revoke":
            asyncio.run(
                revoke(
                    tenant_id=args.tenant,
                    artifact_id=args.id,
                    actor=args.actor,
                    reason=args.reason,
                )
            )
        elif args.command == "export-sources":
            asyncio.run(export_sources(args.output, tenant_id=args.tenant))
        else:
            asyncio.run(list_artifacts(tenant_id=args.tenant))
    except (LocalizationValidationError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
