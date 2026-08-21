import csv
import json

import pytest

from apps.cli import multilingual_e2e_eval as evaluator
from social_reply.application.reply_decision.multilingual_generation import (
    MULTILINGUAL_GENERATION_CONTRACT_VERSION,
)


def _row(*, decision_id: str, language: str, should_auto: bool) -> dict[str, str]:
    row = {field: "" for field in evaluator._FIELDS}
    knowledge_hash = "a" * 64 if should_auto else ""
    row.update(
        {
            "decision_id": decision_id,
            "tenant_id": "tenant-a",
            "brand_id": "brand-a",
            "platform": "telegram",
            "customer_text_redacted": "safe customer text",
            "request_language": language,
            "request_language_confidence": "1.0",
            "reply_language": language if should_auto else "und",
            "resolved_locale": language if should_auto else "und",
            "actual_action": "auto_reply" if should_auto else "handoff",
            "actual_reason_codes": "[]",
            "knowledge_content_hash": knowledge_hash,
            "outbox_id": "outbox-1" if should_auto else "",
            "outbox_status": "SENT" if should_auto else "",
            "outbox_origin_kind": "DECISION" if should_auto else "",
            "outbox_actor_kind": "BOT" if should_auto else "",
            "outbox_message_type": "text" if should_auto else "",
            "outbox_payload_text_hash": "b" * 64 if should_auto else "",
            "automation_state": "BOT_ACTIVE" if should_auto else "HANDOFF_PENDING",
            "open_human_work_count": "0" if should_auto else "1",
            "handoff_notification_count": "0" if should_auto else "1",
            "contract_version": MULTILINGUAL_GENERATION_CONTRACT_VERSION,
            "evaluation_locale": language,
            "case_type": "positive" if should_auto else "negative",
            "should_auto_reply": "true" if should_auto else "false",
            "expected_knowledge_content_hash": knowledge_hash,
        }
    )
    row["evidence_fingerprint"] = evaluator._fingerprint(row)
    return row


def _write_rows(path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=evaluator._FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def test_runtime_evaluator_accepts_generation_and_handoff_rows(tmp_path) -> None:
    input_path = tmp_path / "runtime.csv"
    output_path = tmp_path / "report.json"
    _write_rows(
        input_path,
        [_row(decision_id="auto-ko", language="ko", should_auto=True), _row(
            decision_id="handoff-hi", language="hi", should_auto=False
        )],
    )

    evaluator.evaluate(input_path, output_path)

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["status"] == "pass"
    assert report["contract_version"] == MULTILINGUAL_GENERATION_CONTRACT_VERSION
    assert set(report["runtime_languages"]) == {"ko", "hi"}


def test_runtime_rows_reject_old_contract(tmp_path) -> None:
    input_path = tmp_path / "runtime.csv"
    row = _row(decision_id="old", language="ja", should_auto=True)
    row["contract_version"] = "obsolete-contract"
    row["evidence_fingerprint"] = evaluator._fingerprint(row)
    _write_rows(input_path, [row])

    with pytest.raises(ValueError, match="invalid runtime contract"):
        evaluator._runtime_rows(input_path)


def test_runtime_rows_reject_fingerprint_tampering(tmp_path) -> None:
    input_path = tmp_path / "runtime.csv"
    row = _row(decision_id="tampered", language="ja", should_auto=True)
    row["reply_language"] = "en"
    _write_rows(input_path, [row])

    with pytest.raises(ValueError, match="fingerprint mismatch"):
        evaluator._runtime_rows(input_path)
