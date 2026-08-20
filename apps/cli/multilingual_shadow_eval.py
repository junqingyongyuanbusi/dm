"""Export and evaluate production multilingual knowledge shadow evidence."""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
from pathlib import Path

from sqlalchemy import select

from social_reply.domain.reply.guard import redact_pii
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.shared.config import get_settings

_DEFAULT_CORE_LANGUAGES = frozenset({"en", "zh", "ja", "es", "fr", "de", "pt", "ar", "ru", "th"})

_EXPORT_FIELDS = (
    "decision_id",
    "message_id",
    "tenant_id",
    "brand_id",
    "platform",
    "customer_text_redacted",
    "actual_action",
    "actual_reason_codes",
    "reply_language",
    "resolved_locale",
    "localization_id",
    "localization_release",
    "localization_text_hash",
    "outbox_id",
    "grounding_verified",
    "language",
    "language_confidence",
    "language_source",
    "top1_content_hash",
    "top1_question",
    "top1_approved_reply",
    "top1_similarity",
    "top2_content_hash",
    "top2_question",
    "top2_approved_reply",
    "top2_similarity",
    "margin",
    "match_status",
    "gate_version",
    "corpus_version",
    "embedding_version",
    "retrieval_mode",
    "error_code",
    "evidence_fingerprint",
    "contract_version",
    "renderer_version",
    "case_type",
    "dataset_split",
    "should_auto_reply",
    "expected_content_hash",
    "reviewer",
    "reviewed_at",
    "review_notes",
)


_ANNOTATION_FIELDS = {
    "evidence_fingerprint",
    "case_type",
    "dataset_split",
    "should_auto_reply",
    "expected_content_hash",
    "reviewer",
    "reviewed_at",
    "review_notes",
}


def _evidence_fingerprint(row: dict[str, object]) -> str:
    payload = {
        field: "" if row.get(field) is None else str(row.get(field))
        for field in _EXPORT_FIELDS
        if field not in _ANNOTATION_FIELDS
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _spreadsheet_safe(value: str) -> str:
    return f"'{value}" if value.startswith(("=", "+", "-", "@")) else value


async def export_shadow(output: Path) -> None:
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(
                    models.ReplyDecision,
                    models.Message.text.label("customer_text"),
                    models.Conversation.brand_id,
                    models.Conversation.platform,
                )
                .join(models.Message, models.ReplyDecision.message_id == models.Message.id)
                .join(
                    models.Conversation,
                    models.ReplyDecision.conversation_id == models.Conversation.id,
                )
                .where(models.ReplyDecision.multilingual_shadow.is_(True))
                .order_by(models.ReplyDecision.created_at, models.ReplyDecision.id)
            )
        ).all()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_EXPORT_FIELDS)
        writer.writeheader()
        for decision, customer_text, brand_id, platform in rows:
            evidence = decision.multilingual_shadow_evidence or {}
            language = evidence.get("language") or {}
            top1 = evidence.get("top1") or {}
            top2 = evidence.get("top2") or {}
            export_row = {
                "decision_id": str(decision.id),
                "message_id": str(decision.message_id or ""),
                "tenant_id": decision.tenant_id,
                "brand_id": brand_id,
                "platform": platform,
                "customer_text_redacted": _spreadsheet_safe(redact_pii(customer_text or "")),
                "actual_action": decision.action,
                "actual_reason_codes": json.dumps(decision.reason_codes, ensure_ascii=False),
                "reply_language": decision.reply_language,
                "resolved_locale": decision.resolved_locale,
                "localization_id": str(decision.knowledge_localization_id or ""),
                "localization_release": (
                    decision.knowledge_localization_release_id
                    or evidence.get("localization_release")
                ),
                "localization_text_hash": decision.knowledge_localization_text_hash,
                "outbox_id": str(decision.outbox_id or ""),
                "grounding_verified": decision.grounding_verified,
                "language": language.get("tag") or "und",
                "language_confidence": language.get("confidence"),
                "language_source": language.get("source"),
                "top1_content_hash": top1.get("content_hash"),
                "top1_question": _spreadsheet_safe(top1.get("question") or ""),
                "top1_approved_reply": _spreadsheet_safe(top1.get("approved_reply") or ""),
                "top1_similarity": top1.get("similarity"),
                "top2_content_hash": top2.get("content_hash"),
                "top2_question": _spreadsheet_safe(top2.get("question") or ""),
                "top2_approved_reply": _spreadsheet_safe(top2.get("approved_reply") or ""),
                "top2_similarity": top2.get("similarity"),
                "margin": evidence.get("margin"),
                "match_status": evidence.get("match_status"),
                "gate_version": evidence.get("gate_version"),
                "contract_version": evidence.get("contract_version"),
                "renderer_version": evidence.get("renderer_version"),
                "corpus_version": evidence.get("corpus_version"),
                "embedding_version": evidence.get("embedding_version"),
                "retrieval_mode": evidence.get("retrieval_mode"),
                "error_code": evidence.get("error_code"),
                "evidence_fingerprint": "",
                "case_type": "",
                "dataset_split": "",
                "should_auto_reply": "",
                "expected_content_hash": "",
                "reviewer": "",
                "reviewed_at": "",
                "review_notes": "",
            }
            export_row["evidence_fingerprint"] = _evidence_fingerprint(export_row)
            writer.writerow(export_row)
    print(json.dumps({"output": str(output), "rows": len(rows)}, ensure_ascii=False))


def _reviewed_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = set(_EXPORT_FIELDS) - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"shadow review file missing columns: {sorted(missing)}")
        rows = [dict(row) for row in reader]
    if not rows:
        raise ValueError("shadow review file is empty")
    for row in rows:
        decision_id = row.get("decision_id")
        supplied_fingerprint = (row.get("evidence_fingerprint") or "").strip()
        if not supplied_fingerprint or supplied_fingerprint != _evidence_fingerprint(row):
            raise ValueError(f"row {decision_id} evidence fingerprint mismatch")
        should_auto = (row.get("should_auto_reply") or "").casefold()
        case_type = (row.get("case_type") or "").casefold()
        split = (row.get("dataset_split") or "").casefold()
        if should_auto not in {"true", "false"}:
            raise ValueError(f"row {decision_id} requires should_auto_reply=true/false")
        if case_type not in {"positive", "negative", "ambiguous", "risk"}:
            raise ValueError(f"row {decision_id} requires a valid case_type")
        if split not in {"train", "holdout"}:
            raise ValueError(f"row {decision_id} requires dataset_split=train/holdout")
        if not (row.get("reviewer") or "").strip() or not (row.get("reviewed_at") or "").strip():
            raise ValueError(f"row {decision_id} requires reviewer and reviewed_at")
        if case_type == "positive" and should_auto != "true":
            raise ValueError(f"positive row {decision_id} must set should_auto_reply=true")
        if case_type != "positive" and should_auto != "false":
            raise ValueError(f"{case_type} row {decision_id} must set should_auto_reply=false")
        if should_auto == "true" and not (row.get("expected_content_hash") or "").strip():
            raise ValueError(f"positive row {decision_id} requires expected_content_hash")
    return rows


def _metrics(rows: list[dict[str, str]], similarity: float, margin: float) -> dict:
    true_positive = false_positive = true_negative = false_negative = 0
    for row in rows:
        should_auto = (row.get("should_auto_reply") or "").casefold() == "true"
        top1_similarity = float(row.get("top1_similarity") or 0)
        observed_margin = float(row.get("margin") or 1)
        expected_hash = (row.get("expected_content_hash") or "").strip()
        top1_hash = (row.get("top1_content_hash") or "").strip()
        predicted = top1_similarity >= similarity and observed_margin >= margin and bool(top1_hash)
        correct_evidence = bool(expected_hash) and top1_hash == expected_hash
        if predicted and should_auto and correct_evidence:
            true_positive += 1
        elif predicted:
            false_positive += 1
        elif should_auto:
            false_negative += 1
        else:
            true_negative += 1
    positives = true_positive + false_negative
    recall = true_positive / positives if positives else 0.0
    return {
        "true_positive": true_positive,
        "false_positive": false_positive,
        "true_negative": true_negative,
        "false_negative": false_negative,
        "recall": recall,
    }


def _validate_sample_coverage(
    rows: list[dict[str, str]], supported_languages: frozenset[str]
) -> None:
    if len(rows) < 100:
        raise ValueError("at least 100 reviewed non-error shadow rows are required")
    language_counts = {
        language: sum(
            (row.get("language") or "und").split("-", 1)[0].casefold() == language for row in rows
        )
        for language in supported_languages
    }
    insufficient_languages = {
        language: count for language, count in language_counts.items() if count < 5
    }
    if insufficient_languages:
        raise ValueError(f"insufficient core language coverage: {insufficient_languages}")
    for split, minimum_positive, minimum_negative, minimum_ambiguous in (
        ("train", 25, 25, 10),
        ("holdout", 10, 10, 5),
    ):
        split_rows = [row for row in rows if (row.get("dataset_split") or "").casefold() == split]
        positives = sum(
            (row.get("should_auto_reply") or "").casefold() == "true" for row in split_rows
        )
        negatives = sum(
            (row.get("should_auto_reply") or "").casefold() == "false" for row in split_rows
        )
        ambiguous = sum(
            (row.get("case_type") or "").casefold() == "ambiguous" for row in split_rows
        )
        if (
            positives < minimum_positive
            or negatives < minimum_negative
            or ambiguous < minimum_ambiguous
        ):
            raise ValueError(
                f"insufficient {split} coverage: positives={positives}, negatives={negatives}, "
                f"ambiguous={ambiguous}"
            )
        per_language_requirements = (
            {"positive": 2, "negative": 2, "ambiguous": 1, "risk": 1}
            if split == "train"
            else {"positive": 1, "negative": 1, "ambiguous": 1, "risk": 1}
        )
        for language in supported_languages:
            language_rows = [
                row
                for row in split_rows
                if (row.get("language") or "und").split("-", 1)[0].casefold() == language
            ]
            counts = {
                case_type: sum(
                    (row.get("case_type") or "").casefold() == case_type for row in language_rows
                )
                for case_type in per_language_requirements
            }
            missing = {
                case_type: required - counts[case_type]
                for case_type, required in per_language_requirements.items()
                if counts[case_type] < required
            }
            if missing:
                raise ValueError(f"insufficient {split}/{language} case coverage: {missing}")


def evaluate(path: Path, output: Path) -> None:
    reviewed_rows = _reviewed_rows(path)
    review_dataset_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    settings = get_settings()
    supported_languages = frozenset(
        locale.casefold().split("-", 1)[0] for locale in settings.multilingual_live_locale_set
    ) or (settings.multilingual_supported_language_set or _DEFAULT_CORE_LANGUAGES)
    error_rows = [row for row in reviewed_rows if (row.get("error_code") or "").strip()]
    unsupported_rows = [
        row
        for row in reviewed_rows
        if (row.get("language") or "und").split("-", 1)[0].casefold() not in supported_languages
    ]
    rows = [
        row
        for row in reviewed_rows
        if not (row.get("error_code") or "").strip()
        and (row.get("language") or "und").split("-", 1)[0].casefold() in supported_languages
    ]
    _validate_sample_coverage(rows, supported_languages)
    version_fields = (
        "corpus_version",
        "embedding_version",
        "gate_version",
        "contract_version",
        "renderer_version",
        "localization_release",
    )
    versions = {
        field: sorted({(row.get(field) or "").strip() for row in rows}) for field in version_fields
    }
    if any(len(values) != 1 or not values[0] for values in versions.values()):
        raise ValueError(f"review rows must share one nonblank version set: {versions}")
    train_rows = [row for row in rows if (row.get("dataset_split") or "").casefold() == "train"]
    holdout_rows = [row for row in rows if (row.get("dataset_split") or "").casefold() == "holdout"]
    candidates = []
    for similarity_percent in range(50, 96):
        similarity_threshold = similarity_percent / 100
        for margin_percent in range(0, 21):
            margin_threshold = margin_percent / 100
            metrics = _metrics(train_rows, similarity_threshold, margin_threshold)
            if metrics["false_positive"] == 0 and metrics["recall"] >= 0.8:
                candidates.append(
                    {
                        "min_similarity": similarity_threshold,
                        "min_margin": margin_threshold,
                        **metrics,
                    }
                )
    ranked = sorted(
        candidates,
        key=lambda candidate: (
            -candidate["true_positive"],
            candidate["false_negative"],
            candidate["min_similarity"],
            candidate["min_margin"],
        ),
    )
    selected = ranked[0] if ranked else None
    holdout = (
        _metrics(holdout_rows, selected["min_similarity"], selected["min_margin"])
        if selected
        else None
    )
    passed = bool(
        selected
        and holdout
        and holdout["false_positive"] == 0
        and holdout["recall"] >= 0.8
        and holdout["true_positive"] > 0
    )
    report = {
        "review_dataset_sha256": review_dataset_sha256,
        "reviewers": sorted({(row.get("reviewer") or "").strip() for row in rows}),
        "language_counts": {
            language: sum(
                (row.get("language") or "und").split("-", 1)[0].casefold() == language
                for row in rows
            )
            for language in sorted(supported_languages)
        },
        "supported_languages": sorted(supported_languages),
        "reviewed_rows": len(reviewed_rows),
        "evaluated_rows": len(rows),
        "retrieval_error_rows": len(error_rows),
        "unsupported_language_rows": len(unsupported_rows),
        "versions": {field: values[0] for field, values in versions.items()},
        "selected_thresholds": selected,
        "holdout": holdout,
        "requirements": {"false_positive": 0, "minimum_recall": 0.8},
        "status": "pass" if passed else "fail",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "status": report["status"]}, ensure_ascii=False))
    if not passed:
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    export_parser = subparsers.add_parser("export")
    export_parser.add_argument("--output", type=Path, required=True)
    evaluate_parser = subparsers.add_parser("evaluate")
    evaluate_parser.add_argument("--input", type=Path, required=True)
    evaluate_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "export":
        asyncio.run(export_shadow(args.output))
    else:
        evaluate(args.input, args.output)


if __name__ == "__main__":
    main()
