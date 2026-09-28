"""JSON, XLSX and PDF exports of one selection contain the same rows, in order."""

import hashlib

import pytest

from app.export.renderers import MEDIA_TYPES
from tests.support.export import export, json_rows, key, pdf_ids, pdf_text, xlsx_rows

pytestmark = pytest.mark.e2e

SELECTIONS = [
    ("a_hr_admin", {"filters": {"date_from": "2026-09-14", "date_to": "2026-09-18"}}),
    ("a_eng_manager", {"filters": {"date_from": "2026-09-01", "date_to": "2026-09-04"}}),
    ("b_manager", {"filters": {"status": "absent"}}),
]


def _all(api, auth, persona, body):
    out = {}
    for fmt in ("json", "xlsx", "pdf"):
        r = export(api, auth, persona, fmt, source="records", **body)
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith(MEDIA_TYPES[fmt])
        assert r.headers["content-disposition"].startswith("attachment;")
        assert r.headers["content-disposition"].rstrip('"').endswith(f".{fmt}")
        out[fmt] = r
    return out


@pytest.mark.parametrize(("persona", "body"), SELECTIONS)
def test_three_formats_identical(api, auth, persona, body):
    files = _all(api, auth, persona, body)
    meta, jrows = json_rows(files["json"].content)
    xmeta, xrows = xlsx_rows(files["xlsx"].content)
    ids = [key(r) for r in jrows]
    assert ids, "selection must not be empty"
    assert [key(r) for r in xrows] == ids
    assert pdf_ids(files["pdf"].content) == ids
    # same traceability in every format
    assert [(r["source_file"], r["source_locator"]) for r in xrows] == [
        (r["source_file"], r["source_locator"]) for r in jrows
    ]
    checksum = hashlib.sha256("\n".join(ids).encode()).hexdigest()
    assert meta["checksum"] == xmeta["checksum"] == checksum
    assert {f.headers["x-export-checksum"] for f in files.values()} == {checksum}
    assert meta["record_count"] == len(ids) == int(xmeta["record_count"])
    assert checksum in pdf_text(files["pdf"].content)


def test_records_ordered_by_date_employee(api, auth):
    _, rows = json_rows(export(api, auth, "a_hr_admin", source="records").content)
    order = [(r["attendance_date"], r["employee_id"], r["record_id"]) for r in rows]
    assert order == sorted(order)


def test_default_period_is_data_coverage(api, auth):
    meta, rows = json_rows(export(api, auth, "a_hr_admin", source="records").content)
    period = meta["source"]["period"]
    assert (period["date_from"], period["date_to"]) == ("2026-09-01", "2026-09-30")
    assert min(r["attendance_date"] for r in rows) == "2026-09-01"


def test_filters_applied(api, auth):
    body = {"filters": {"employee_id": "E002", "date_from": "2026-09-07", "date_to": "2026-09-11"}}
    _, rows = json_rows(export(api, auth, "a_hr_admin", source="records", **body).content)
    assert len(rows) == 5 and {r["employee_id"] for r in rows} == {"E002"}


def test_conflict_day_flagged(api, auth):
    body = {"filters": {"employee_id": "E002", "date_from": "2026-09-15", "date_to": "2026-09-15"}}
    _, rows = json_rows(export(api, auth, "a_hr_admin", source="records", **body).content)
    assert len(rows) == 1 and rows[0]["conflict"] and rows[0]["status"] == "conflict"


def test_pdf_footer_has_classification_and_request_id(api, auth):
    r = export(api, auth, "a_eng_manager", "pdf", source="records")
    text = pdf_text(r.content)
    assert f"INTERNAL | request {r.headers['x-request-id']}" in text


def test_export_is_audited(api, auth):
    r = export(api, auth, "a_eng_manager", "xlsx", source="records")
    rid = r.headers["x-request-id"]
    events = api.get("/v1/audit", params={"request_id": rid}, headers=auth("a_auditor")).json()
    items = events["events"] if isinstance(events, dict) else events
    ev = next(e for e in items if e["event_type"] == "export")
    assert ev["details"]["format"] == "xlsx"
    assert ev["details"]["checksum"] == r.headers["x-export-checksum"]
    assert ev["details"]["record_count"] == int(r.headers["x-export-record-count"])
