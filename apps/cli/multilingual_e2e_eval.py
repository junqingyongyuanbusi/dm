"""Export and evaluate English-corpus multilingual runtime decisions.

The evaluator is intentionally small: it checks the runtime contract, language equality,
knowledge provenance, and the expected AUTO_REPLY/DRAFT/HANDOFF outcome. It does not require
per-language allowlists, reviewed localization artifacts, or a calibration release.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
from pathlib import Path

from sqlalchemy import func, select

from social_reply.application.reply_decision.multilingual_generation import (
    MULTILINGUAL_GENERATION_CONTRACT_VERSION,
)
from social_reply.domain.reply.guard import redact_pii
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory

_FIELDS = (
    "decision_id",
    "tenant_id",
    "brand_id",
    "platform",
    "customer_text_redacted",
    "request_language",
    "request_language_confidence",
    "reply_language",
    "resolved_locale",
    "actual_action",
    "actual_reason_codes",
    "knowledge_content_hash",
    "outbox_id",
    "outbox_status",
    "outbox_origin_kind",
    "outbox_actor_kind",
    "outbox_message_type",
    "outbox_payload_text_hash",
    "automation_state",
    "open_human_work_count",
    "handoff_notification_count",
    "contract_version",
    "evaluation_locale",
    "case_type",
    "should_auto_reply",
    "expected_knowledge_content_hash",
    "evidence_fingerprint",
)
_ANNOTATIONS = frozenset(
    {"evaluation_locale", "case_type", "should_auto_reply", "expected_knowledge_content_hash"}
)
_CASE_TYPES = frozenset({"positive", "negative", "ambiguous", "risk"})


def _spreadsheet_safe(value: str) -> str:
    return f"'{value}" if value.startswith(("=", "+", "-", "@")) else value


def _fingerprint(row: dict[str, object]) -> str:
    payload = {
        field: "" if row.get(field) is None else str(row.get(field))
        for field in _FIELDS
        if field not in _ANNOTATIONS and field != "evidence_fingerprint"
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


async def _session_rows() -> list:
    open_work_count = (
        select(func.count())
        .select_from(models.HumanWorkItem)
        .where(
            models.HumanWorkItem.conversation_id == models.ReplyDecision.conversation_id,
            models.HumanWorkItem.status.in_(["WAITING", "CLAIMED"]),
        )
        .correlate(models.ReplyDecision)
        .scalar_subquery()
    )
    notification_count = (
        select(func.count())
        .select_from(models.HandoffNotificationIntent)
        .where(
            models.HandoffNotificationIntent.conversation_id == models.ReplyDecision.conversation_id
        )
        .correlate(models.ReplyDecision)
        .scalar_subquery()
    )
    async with get_session_factory()() as session:
        result = await session.execute(
            select(
                models.ReplyDecision,
                models.Conversation.brand_id,
                models.Conversation.platform,
                models.Message.text.label("customer_text"),
                models.OutboxMessage.status.label("outbox_status"),
                models.OutboxMessage.origin_kind.label("outbox_origin_kind"),
                models.OutboxMessage.actor_kind.label("outbox_actor_kind"),
                models.OutboxMessage.message_type.label("outbox_message_type"),
                models.OutboxMessage.payload.label("outbox_payload"),
                models.AutomationState.state.label("automation_state"),
                open_work_count.label("open_human_work_count"),
                notification_count.label("handoff_notification_count"),
            )
            .join(
                models.Conversation,
                models.Conversation.id == models.ReplyDecision.conversation_id,
            )
            .join(models.Message, models.Message.id == models.ReplyDecision.message_id)
            .join(
                models.AutomationState,
                models.AutomationState.conversation_id == models.ReplyDecision.conversation_id,
            )
            .outerjoin(
                models.OutboxMessage,
                models.OutboxMessage.id == models.ReplyDecision.outbox_id,
            )
            .where(
                models.ReplyDecision.multilingual_contract_version
                == MULTILINGUAL_GENERATION_CONTRACT_VERSION
            )
            .order_by(models.ReplyDecision.created_at, models.ReplyDecision.id)
        )
        return list(result.all())


async def export_decisions(output: Path) -> None:
    rows = await _session_rows()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_FIELDS)
        writer.writeheader()
        for evidence_row in rows:
            decision = evidence_row.ReplyDecision
            payload = evidence_row.outbox_payload or {}
            text = payload.get("text") if isinstance(payload, dict) else None
            row = {
                "decision_id": str(decision.id),
                "tenant_id": decision.tenant_id,
                "brand_id": evidence_row.brand_id,
                "platform": evidence_row.platform,
                "customer_text_redacted": _spreadsheet_safe(
                    redact_pii(evidence_row.customer_text or "")
                ),
                "request_language": decision.request_language,
                "request_language_confidence": decision.request_language_confidence,
                "reply_language": decision.reply_language,
                "resolved_locale": decision.resolved_locale,
                "actual_action": decision.action,
                "actual_reason_codes": json.dumps(decision.reason_codes, ensure_ascii=False),
                "knowledge_content_hash": decision.knowledge_content_hash,
                "outbox_id": str(decision.outbox_id or ""),
                "outbox_status": evidence_row.outbox_status or "",
                "outbox_origin_kind": evidence_row.outbox_origin_kind or "",
                "outbox_actor_kind": evidence_row.outbox_actor_kind or "",
                "outbox_message_type": evidence_row.outbox_message_type or "",
                "outbox_payload_text_hash": (
                    hashlib.sha256(text.encode()).hexdigest() if isinstance(text, str) else ""
                ),
                "automation_state": evidence_row.automation_state,
                "open_human_work_count": evidence_row.open_human_work_count,
                "handoff_notification_count": evidence_row.handoff_notification_count,
                "contract_version": decision.multilingual_contract_version,
                "evaluation_locale": decision.request_language,
                "case_type": "positive" if decision.action == "auto_reply" else "negative",
                "should_auto_reply": "true" if decision.action == "auto_reply" else "false",
                "expected_knowledge_content_hash": decision.knowledge_content_hash or "",
                "evidence_fingerprint": "",
            }
            row["evidence_fingerprint"] = _fingerprint(row)
            writer.writerow(row)
    print(json.dumps({"output": str(output), "rows": len(rows)}, ensure_ascii=False))


def _runtime_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = set(_FIELDS) - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"runtime evaluation file missing columns: {sorted(missing)}")
        rows = [dict(row) for row in reader]
    if not rows:
        raise ValueError("runtime evaluation file is empty")
    decision_ids: set[str] = set()
    fingerprints: set[str] = set()
    for row in rows:
        if row["decision_id"] in decision_ids:
            raise ValueError(f"duplicate decision id: {row['decision_id']}")
        if row["evidence_fingerprint"] in fingerprints:
            raise ValueError("duplicate decision evidence fingerprint")
        decision_ids.add(row["decision_id"])
        fingerprints.add(row["evidence_fingerprint"])
        if row["contract_version"] != MULTILINGUAL_GENERATION_CONTRACT_VERSION:
            raise ValueError(f"decision {row['decision_id']} has an invalid runtime contract")
        if row["case_type"] not in _CASE_TYPES:
            raise ValueError(f"decision {row['decision_id']} requires a valid case_type")
        if row["should_auto_reply"].casefold() not in {"true", "false"}:
            raise ValueError(f"decision {row['decision_id']} requires should_auto_reply")
        if row["evidence_fingerprint"] != _fingerprint(row):
            raise ValueError(f"decision {row['decision_id']} evidence fingerprint mismatch")
        if row["should_auto_reply"].casefold() == "true" and not row[
            "expected_knowledge_content_hash"
        ].strip():
            raise ValueError(f"decision {row['decision_id']} requires expected knowledge hash")
    return rows


def evaluate(path: Path, output: Path) -> None:
    rows = _runtime_rows(path)
    safety = {
        "wrong_language_outbox": 0,
        "knowledge_provenance_failure": 0,
        "unexpected_customer_outbox": 0,
    }
    positives = 0
    for row in rows:
        language_matches = (
            row["reply_language"].split("-", 1)[0].casefold()
            == row["request_language"].split("-", 1)[0].casefold()
            and row["resolved_locale"].split("-", 1)[0].casefold()
            == row["request_language"].split("-", 1)[0].casefold()
        )
        sent_customer_text = (
            row["outbox_status"] == "SENT"
            and row["outbox_origin_kind"] == "DECISION"
            and row["outbox_actor_kind"] == "BOT"
            and row["outbox_message_type"] == "text"
        )
        if sent_customer_text and not language_matches:
            safety["wrong_language_outbox"] += 1
        if row["should_auto_reply"].casefold() == "true":
            positives += 1
            if not (
                row["actual_action"] == "auto_reply"
                and sent_customer_text
                and language_matches
                and row["knowledge_content_hash"]
                == row["expected_knowledge_content_hash"]
            ):
                safety["knowledge_provenance_failure"] += 1
        elif sent_customer_text:
            safety["unexpected_customer_outbox"] += 1

    passed = positives > 0 and all(value == 0 for value in safety.values())
    report = {
        "status": "pass" if passed else "fail",
        "contract_version": MULTILINGUAL_GENERATION_CONTRACT_VERSION,
        "runtime_languages": sorted(
            {row["evaluation_locale"].split("-", 1)[0] for row in rows}
        ),
        "safety": safety,
        "rows": len(rows),
        "positive_rows": positives,
        "dataset_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "status": report["status"]}, ensure_ascii=False))
    if not passed:
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="English-corpus multilingual runtime evaluation")
    subparsers = parser.add_subparsers(dest="command", required=True)
    export_parser = subparsers.add_parser("export")
    export_parser.add_argument("--output", type=Path, required=True)
    evaluate_parser = subparsers.add_parser("evaluate")
    evaluate_parser.add_argument("--input", type=Path, required=True)
    evaluate_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "export":
        asyncio.run(export_decisions(args.output))
    else:
        evaluate(args.input, args.output)


if __name__ == "__main__":
    main()
