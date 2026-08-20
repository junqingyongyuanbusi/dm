import csv
import json
from types import SimpleNamespace

import pytest

from apps.cli import multilingual_e2e_eval as evaluator


def _row(*, locale: str, case_type: str, should_auto: bool) -> dict[str, str]:
    request_language = locale.split("-", 1)[0]
    row = {field: "" for field in evaluator._FIELDS}
    row.update(
        {
            "decision_id": f"decision-{case_type}",
            "tenant_id": "tenant-a",
            "brand_id": "brand-a",
            "platform": "telegram",
            "customer_text_redacted": "safe customer text",
            "request_language": request_language,
            "request_language_confidence": "1.0",
            "reply_language": locale if should_auto else "und",
            "resolved_locale": locale if should_auto else "und",
            "actual_action": "auto_reply" if should_auto else "handoff",
            "actual_reason_codes": "[]",
            "knowledge_content_hash": "b" * 64 if should_auto else "",
            "localization_id": "artifact-1" if should_auto else "",
            "localization_release": "release-v1" if should_auto else "",
            "localization_text_hash": "a" * 64 if should_auto else "",
            "outbox_id": "outbox-1" if should_auto else "",
            "outbox_status": "SENT" if should_auto else "",
            "outbox_origin_kind": "DECISION" if should_auto else "",
            "outbox_actor_kind": "BOT" if should_auto else "",
            "outbox_message_type": "text" if should_auto else "",
            "outbox_payload_text_hash": "a" * 64 if should_auto else "",
            "automation_state": "BOT_ACTIVE" if should_auto else "HANDOFF_PENDING",
            "open_human_work_count": "0" if should_auto else "1",
            "handoff_notification_count": "0" if should_auto else "1",
            "contract_version": "multilingual-v2-reviewed-localization",
            "evaluation_locale": locale,
            "case_type": case_type,
            "should_auto_reply": "true" if should_auto else "false",
            "expected_content_hash": "b" * 64 if should_auto else "",
            "reviewer": "reviewer",
            "reviewed_at": "2026-08-19T00:00:00Z",
        }
    )
    row["evidence_fingerprint"] = evaluator._fingerprint(row)
    return row


def _write_review(path, locale: str) -> None:
    rows = [
        _row(locale=locale, case_type="positive", should_auto=True),
        _row(locale=locale, case_type="negative", should_auto=False),
        _row(locale=locale, case_type="ambiguous", should_auto=False),
        _row(locale=locale, case_type="risk", should_auto=False),
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=evaluator._FIELDS)
        writer.writeheader()
        writer.writerows(rows)


@pytest.mark.parametrize("locale", ["ja", "pt-BR"])
def test_e2e_coverage_uses_explicit_evaluation_locale(tmp_path, monkeypatch, locale):
    review_path = tmp_path / "review.csv"
    calibration_path = tmp_path / "calibration.json"
    output_path = tmp_path / "e2e.json"
    _write_review(review_path, locale)
    calibration = {
        "status": "pass",
        "versions": {
            "corpus_version": "corpus-v1",
            "embedding_version": "text-embedding-3-small",
            "gate_version": "strong-gate-v1",
            "contract_version": "multilingual-v2-reviewed-localization",
            "renderer_version": "reviewed-localization-v1",
            "localization_release": "release-v1",
        },
        "selected_thresholds": {"min_similarity": 0.8, "min_margin": 0.08},
    }
    calibration_path.write_text(json.dumps(calibration), encoding="utf-8")
    monkeypatch.setattr(
        evaluator,
        "get_settings",
        lambda: SimpleNamespace(
            multilingual_live_locale_set=frozenset({locale}),
            knowledge_localization_release="release-v1",
        ),
    )

    evaluator.evaluate(review_path, calibration_path, output_path)

    report = json.loads(output_path.read_text())
    assert report["status"] == "pass"
    assert report["supported_locales"] == [locale]


def test_e2e_review_rejects_invalid_evaluation_locale(tmp_path):
    review_path = tmp_path / "review.csv"
    _write_review(review_path, "ja")
    rows = list(csv.DictReader(review_path.open(encoding="utf-8-sig")))
    rows[0]["evaluation_locale"] = "not_a_locale!"
    with review_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=evaluator._FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    with pytest.raises(ValueError, match="evaluation_locale"):
        evaluator._reviewed_rows(review_path)


def test_spreadsheet_safe_escapes_formula_prefixes() -> None:
    assert evaluator._spreadsheet_safe("=HYPERLINK('x')").startswith("'")
    assert evaluator._spreadsheet_safe("plain text") == "plain text"


def test_e2e_review_rejects_duplicate_decision_rows(tmp_path) -> None:
    review_path = tmp_path / "review.csv"
    _write_review(review_path, "ja")
    rows = list(csv.DictReader(review_path.open(encoding="utf-8-sig")))
    rows.append(dict(rows[0]))
    with review_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=evaluator._FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    with pytest.raises(ValueError, match="duplicate decision"):
        evaluator._reviewed_rows(review_path)
