"""Export and evaluate reviewed-localization end-to-end decisions.

This report is separate from retrieval calibration. It evaluates the actual
language -> pinned artifact -> guard -> Outbox contract.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
from pathlib import Path

from sqlalchemy import func, select

from social_reply.domain.reply.guard import redact_pii
from social_reply.domain.reply.localization import canonicalize_locale
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.shared.config import get_settings

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
    "localization_id",
    "localization_release",
    "localization_text_hash",
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
    "evidence_fingerprint",
    "evaluation_locale",
    "case_type",
    "should_auto_reply",
    "expected_content_hash",
    "reviewer",
    "reviewed_at",
    "review_notes",
)
_ANNOTATIONS = {
    "evaluation_locale",
    "case_type",
    "should_auto_reply",
    "expected_content_hash",
    "reviewer",
    "reviewed_at",
    "review_notes",
    "evidence_fingerprint",
}



def _spreadsheet_safe(value: str) -> str:
    return f"'{value}" if value.startswith(("=", "+", "-", "@")) else value

def _fingerprint(row: dict[str, object]) -> str:
    payload = {
        field: "" if row.get(field) is None else str(row.get(field))
        for field in _FIELDS
        if field not in _ANNOTATIONS
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


async def export_decisions(output: Path) -> None:
    settings = get_settings()
    rows = await _session_rows(settings)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_FIELDS)
        writer.writeheader()
        for evidence_row in rows:
            decision = evidence_row.ReplyDecision
            outbox_payload = evidence_row.outbox_payload or {}
            outbox_text = outbox_payload.get("text") if isinstance(outbox_payload, dict) else None
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
                "localization_id": str(decision.knowledge_localization_id or ""),
                "localization_release": decision.knowledge_localization_release_id,
                "localization_text_hash": decision.knowledge_localization_text_hash,
                "outbox_id": str(decision.outbox_id or ""),
                "outbox_status": evidence_row.outbox_status,
                "outbox_origin_kind": evidence_row.outbox_origin_kind,
                "outbox_actor_kind": evidence_row.outbox_actor_kind,
                "outbox_message_type": evidence_row.outbox_message_type,
                "outbox_payload_text_hash": (
                    hashlib.sha256(outbox_text.encode()).hexdigest()
                    if isinstance(outbox_text, str)
                    else ""
                ),
                "automation_state": evidence_row.automation_state,
                "open_human_work_count": evidence_row.open_human_work_count,
                "handoff_notification_count": evidence_row.handoff_notification_count,
                "contract_version": decision.multilingual_contract_version,
                "evidence_fingerprint": "",
                "evaluation_locale": "",
                "case_type": "",
                "should_auto_reply": "",
                "expected_content_hash": "",
                "reviewer": "",
                "reviewed_at": "",
                "review_notes": "",
            }
            row["evidence_fingerprint"] = _fingerprint(row)
            writer.writerow(row)
    print(json.dumps({"output": str(output), "rows": len(rows)}, ensure_ascii=False))


async def _session_rows(settings):
    live_languages = {
        locale.casefold().split("-", 1)[0] for locale in settings.multilingual_live_locale_set
    }
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
        result = (
            await session.execute(
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
                    == "multilingual-v2-reviewed-localization"
                )
                .order_by(models.ReplyDecision.created_at, models.ReplyDecision.id)
            )
        ).all()
    return [
        row
        for row in result
        if row.ReplyDecision.request_language.casefold().split("-", 1)[0] in live_languages
    ]


def _reviewed_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = set(_FIELDS) - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"E2E review file missing columns: {sorted(missing)}")
        rows = [dict(row) for row in reader]
    if not rows:
        raise ValueError("E2E review file is empty")
    decision_ids: set[str] = set()
    evidence_fingerprints: set[str] = set()
    for row in rows:
        if row["decision_id"] in decision_ids:
            raise ValueError(f"duplicate decision id: {row['decision_id']}")
        if row["evidence_fingerprint"] in evidence_fingerprints:
            raise ValueError("duplicate decision evidence fingerprint")
        decision_ids.add(row["decision_id"])
        evidence_fingerprints.add(row["evidence_fingerprint"])
        if row["evidence_fingerprint"] != _fingerprint(row):
            raise ValueError(f"decision {row['decision_id']} evidence fingerprint mismatch")
        try:
            row["evaluation_locale"] = canonicalize_locale(row["evaluation_locale"])
        except ValueError as exc:
            raise ValueError(
                f"decision {row['decision_id']} requires a valid evaluation_locale"
            ) from exc
        if row["case_type"] not in {"positive", "negative", "ambiguous", "risk"}:
            raise ValueError(f"decision {row['decision_id']} requires a valid case_type")
        if row["should_auto_reply"].casefold() not in {"true", "false"}:
            raise ValueError(f"decision {row['decision_id']} requires should_auto_reply")
        should_auto = row["should_auto_reply"].casefold() == "true"
        if (row["case_type"] == "positive") != should_auto:
            raise ValueError(
                f"decision {row['decision_id']} case_type/should_auto_reply mismatch"
            )
        if not row["reviewer"].strip() or not row["reviewed_at"].strip():
            raise ValueError(f"decision {row['decision_id']} requires reviewer metadata")
        if (
            row["should_auto_reply"].casefold() == "true"
            and not row["expected_content_hash"].strip()
        ):
            raise ValueError(f"decision {row['decision_id']} requires expected_content_hash")
    return rows


def evaluate(path: Path, calibration_path: Path, output: Path) -> None:
    settings = get_settings()
    rows = _reviewed_rows(path)
    calibration_bytes = calibration_path.read_bytes()
    calibration = json.loads(calibration_bytes)
    if calibration.get("status") != "pass":
        raise ValueError("retrieval calibration is not approved")
    versions = calibration.get("versions") or {}
    expected_versions = {
        "contract_version": "multilingual-v2-reviewed-localization",
        "renderer_version": "reviewed-localization-v1",
        "localization_release": settings.knowledge_localization_release,
    }
    for field, expected in expected_versions.items():
        if versions.get(field) != expected:
            raise ValueError(f"calibration {field} mismatch")

    safety = {
        "wrong_language_outbox": 0,
        "risk_or_case_auto_reply": 0,
        "grounding_false_accept": 0,
        "unexpected_customer_outbox": 0,
    }
    positives = 0
    coverage = {
        locale: {case_type: 0 for case_type in ("positive", "negative", "ambiguous", "risk")}
        for locale in settings.multilingual_live_locale_set
    }
    for row in rows:
        should_auto = row["should_auto_reply"].casefold() == "true"
        actual_auto = row["actual_action"] == "auto_reply"
        expected_hash = row["expected_content_hash"].strip()
        correct_hash = bool(expected_hash) and row["knowledge_content_hash"] == expected_hash
        language_matches = (
            row["reply_language"].split("-", 1)[0].casefold()
            == row["request_language"].split("-", 1)[0].casefold()
        )
        locale = row["evaluation_locale"]
        if locale not in coverage:
            raise ValueError(f"evaluation locale is not enabled: {locale}")
        coverage[locale][row["case_type"]] += 1
        sent_bot_outbox = (
            row["outbox_status"] == "SENT"
            and row["outbox_origin_kind"] == "DECISION"
            and row["outbox_actor_kind"] == "BOT"
            and row["outbox_message_type"] == "text"
            and row["outbox_payload_text_hash"] == row["localization_text_hash"]
        )
        cancelled_bot_outbox = (
            row["outbox_status"] == "CANCELLED"
            and row["outbox_origin_kind"] == "DECISION"
            and row["outbox_actor_kind"] == "BOT"
        )
        handoff_complete = (
            row["actual_action"] == "handoff"
            and (not row["outbox_id"].strip() or cancelled_bot_outbox)
            and row["automation_state"] == "HANDOFF_PENDING"
            and int(row["open_human_work_count"] or 0) == 1
            and int(row["handoff_notification_count"] or 0) >= 1
        )
        if should_auto:
            positives += 1
            if sent_bot_outbox and not language_matches:
                safety["wrong_language_outbox"] += 1
            if not (
                actual_auto
                and sent_bot_outbox
                and correct_hash
                and row["localization_id"].strip()
                and row["localization_text_hash"].strip()
                and row["localization_release"] == settings.knowledge_localization_release
                and row["resolved_locale"] in settings.multilingual_live_locale_set
            ):
                safety["grounding_false_accept"] += 1
        elif not handoff_complete:
            safety["unexpected_customer_outbox"] += 1
            if row["case_type"] in {"risk", "ambiguous", "negative"}:
                safety["risk_or_case_auto_reply"] += 1

    coverage_ok = all(
        count >= 1 for locale_counts in coverage.values() for count in locale_counts.values()
    )
    passed = positives > 0 and coverage_ok and all(value == 0 for value in safety.values())
    report = {
        "status": "pass" if passed else "fail",
        "review_dataset_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "calibration_report_sha256": hashlib.sha256(calibration_bytes).hexdigest(),
        "supported_locales": sorted(settings.multilingual_live_locale_set),
        "corpus_version": versions.get("corpus_version"),
        "versions": versions,
        "selected_thresholds": calibration.get("selected_thresholds"),
        "safety": safety,
        "reviewed_rows": len(rows),
        "positive_rows": positives,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "status": report["status"]}, ensure_ascii=False))
    if not passed:
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Reviewed-localization E2E holdout")
    subparsers = parser.add_subparsers(dest="command", required=True)
    export_parser = subparsers.add_parser("export")
    export_parser.add_argument("--output", type=Path, required=True)
    evaluate_parser = subparsers.add_parser("evaluate")
    evaluate_parser.add_argument("--input", type=Path, required=True)
    evaluate_parser.add_argument("--calibration", type=Path, required=True)
    evaluate_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "export":
        asyncio.run(export_decisions(args.output))
    else:
        evaluate(args.input, args.calibration, args.output)


if __name__ == "__main__":
    main()
