"""OCR ingestion: printed scans (Tesseract), handwriting (vision + cross-check),
ambiguity handling, and the no-vision fallback. Vision output is recorded
(tests/fixtures/vision/*.json) so the gate is deterministic; the real model is
exercised by `pytest -m live` and scripts/ocr_eval.py."""

import pytest
from sqlalchemy import text

from app.db.session import scoped_session
from tests.support.ingest import records, upload
from tests.support.vision import use_recorded_vision, vision_down

pytestmark = [pytest.mark.ocr, pytest.mark.integration]


def _truth(truth, emp, date):
    return next(
        r
        for r in truth["rows"]
        if r["tenant_id"] == "tenant_a" and r["employee_id"] == emp and r["attendance_date"] == date
    )


@pytest.mark.parametrize("name", ["scan_printed.png", "scan_printed.pdf"])
def test_printed_scan(ingest_api, auth, manifests, truth, name, monkeypatch):
    vision_down(monkeypatch)
    body = upload(ingest_api, auth("a_hr_admin"), name).json()
    assert body["status"] == "completed", body
    assert body["counts"]["records_created"] == 8 and body["counts"]["review_required"] == 0
    recs = {r["source_locator"]: r for r in records(f"source_file = '{name}'")}
    expected = {row["locator"]: row for row in manifests["files"][name]["rows"]}
    assert set(recs) == set(expected)
    fields = correct = 0
    for loc, row in expected.items():
        rec, t = recs[loc], _truth(truth, row["employee_id"], row["attendance_date"])
        for got, want in (
            (rec["employee_id"], t["employee_id"]),
            (rec["status"], t["status"]),
            (rec["attendance_date"].isoformat(), t["attendance_date"]),
        ):
            fields += 1
            correct += got == want
        for col in ("check_in", "check_out"):
            if rec[col] is not None:
                assert rec[col].strftime("%H:%M") == t[col], (loc, col)
            else:
                assert any(col in reason for reason in rec["review_reasons"]), (loc, col)
        assert (
            rec["extraction_method"] == "ocr_tesseract"
            and 0.75 <= float(rec["extraction_confidence"]) <= 0.95
        )
    assert correct / fields >= 0.9


def test_handwriting_with_vision(ingest_api, auth, truth, monkeypatch):
    use_recorded_vision(monkeypatch, "handwritten_sheet")
    body = upload(ingest_api, auth("a_hr_admin"), "handwritten_sheet.png").json()
    assert body["status"] == "completed", body
    assert body["counts"]["records_created"] == 3 and body["counts"]["review_required"] == 0
    for rec in records("source_file = 'handwritten_sheet.png'"):
        t = _truth(truth, rec["employee_id"], "2026-09-30")
        assert rec["status"] == t["status"] and rec["extraction_method"] == "ocr_reconciled"
        assert float(rec["extraction_confidence"]) <= 0.95
        assert rec["raw_values"]["_engine"] == "vision"


def test_ambiguous_handwriting_flagged_not_trusted(ingest_api, auth, monkeypatch, scope_for):
    use_recorded_vision(monkeypatch, "handwritten_ambiguous")
    body = upload(ingest_api, auth("a_hr_admin"), "handwritten_ambiguous.png").json()
    assert body["status"] == "completed", body
    by_emp = {r["employee_id"]: r for r in records("source_file = 'handwritten_ambiguous.png'")}
    assert set(by_emp) == {"E008", "E009", "E010", "E011"}
    assert not by_emp["E008"]["review_required"] and not by_emp["E010"]["review_required"]
    noah, lucas = by_emp["E009"], by_emp["E011"]
    assert noah["review_required"] and any("status unclear" in r for r in noah["review_reasons"])
    assert lucas["review_required"] and lucas["status"] == "unknown"
    assert lucas["raw_values"]["status"] == "P?" and lucas["raw_values"]["employee_id"] == "E01?"
    assert float(lucas["extraction_confidence"]) < 0.5
    with scoped_session(scope_for("a_hr_admin")) as s:
        in_view = set(
            s.execute(
                text("SELECT employee_id FROM v_attendance WHERE attendance_date = '2026-09-30'")
            ).scalars()
        )
    assert in_view == {"E008", "E010"}


def test_handwriting_without_vision_is_all_review(ingest_api, auth, monkeypatch):
    vision_down(monkeypatch)
    body = upload(ingest_api, auth("a_hr_admin"), "handwritten_sheet.png").json()
    assert body["status"] == "completed"
    assert any("vision OCR unavailable" in w for w in body["warnings"])
    recs = records("source_file = 'handwritten_sheet.png'")
    assert recs and all(r["review_required"] for r in recs)
    assert all(float(r["extraction_confidence"]) <= 0.6 for r in recs)
    assert all(any("Tesseract only" in x for x in r["review_reasons"]) for r in recs)


def test_unreadable_without_vision_is_retryable(ingest_api, auth, monkeypatch):
    vision_down(monkeypatch)
    body = upload(ingest_api, auth("a_hr_admin"), "handwritten_ambiguous.png").json()
    assert body["status"] == "failed" and body["attempts"] == 3
    assert "vision OCR unavailable" in body["failures"][-1]["message"]
    use_recorded_vision(monkeypatch, "handwritten_ambiguous")
    again = ingest_api.post(f"/v1/jobs/{body['job_id']}/retry", headers=auth("a_hr_admin")).json()
    assert again["status"] == "completed" and again["counts"]["records_created"] == 4


def test_vision_model_never_sees_other_tenants(ingest_api, auth, monkeypatch):
    """The vision prompt contains only the image + fixed instructions."""
    provider = use_recorded_vision(monkeypatch, "handwritten_sheet")
    upload(ingest_api, auth("a_hr_admin"), "handwritten_sheet.png")
    (messages,) = provider.calls
    text_parts = " ".join(p["text"] for p in messages[0]["content"] if p["type"] == "text")
    assert "tenant" not in text_parts.lower() and "E101" not in text_parts
    assert messages[0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_ocr_overlap_with_csv_is_consistent(ingest_api, auth, monkeypatch, scope_for):
    vision_down(monkeypatch)
    upload(ingest_api, auth("a_hr_admin"), "tenant_a_sep_v2.csv")
    upload(ingest_api, auth("a_hr_admin"), "scan_printed.png")
    with scoped_session(scope_for("a_hr_admin")) as s:
        rows = s.execute(
            text(
                "SELECT employee_id, status, source_count, source_file FROM v_attendance WHERE attendance_date = '2026-09-29'"
            )
        ).all()
    assert len(rows) == 8 and all(r.source_count == 2 and r.status != "conflict" for r in rows)
    assert {r.source_file for r in rows} == {"tenant_a_sep_v2.csv"}
