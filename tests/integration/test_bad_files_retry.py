"""Validation at the door, failure reporting, transient retry, the RQ worker path."""

import pytest

from app.ingestion import service
from app.ingestion.types import TransientError
from tests.support.ingest import records, upload

pytestmark = pytest.mark.integration


def _err(r):
    return r.status_code, r.json()["error"]["code"]


def test_corrupt_xlsx_rejected(ingest_api, auth):
    r = upload(ingest_api, auth("a_hr_admin"), "corrupt.xlsx")
    assert _err(r) == (415, "UNSUPPORTED_FILE")
    assert "does not match" in r.json()["error"]["message"]


def test_empty_file_rejected(ingest_api, auth):
    assert _err(upload(ingest_api, auth("a_hr_admin"), "empty.csv")) == (422, "VALIDATION_ERROR")


def test_renamed_executable_rejected(ingest_api, auth):
    r = upload(ingest_api, auth("a_hr_admin"), "payload.csv", content=b"MZ\x90\x00" + b"\x00" * 100)
    assert _err(r) == (415, "UNSUPPORTED_FILE")


def test_unsupported_extension(ingest_api, auth):
    assert _err(upload(ingest_api, auth("a_hr_admin"), "run.exe", content=b"MZ")) == (
        415,
        "UNSUPPORTED_FILE",
    )


def test_no_header_fails_permanently(ingest_api, auth):
    r = upload(ingest_api, auth("a_hr_admin"), "notes.csv", content=b"hello,world\n1,2\n")
    body = r.json()
    assert body["status"] == "failed" and body["attempts"] == 1  # permanent: not retried
    assert "no header row" in body["failures"][-1]["message"]
    failed = ingest_api.get(
        "/v1/jobs", headers=auth("a_hr_admin"), params={"status": "failed"}
    ).json()["jobs"]
    assert [j["job_id"] for j in failed] == [body["job_id"]]


def test_row_level_failures_reported(ingest_api, auth):
    csv = (
        b"date,employee_id,employee_name,status\n"
        b"2026-09-01,E001,Alice Johnson,Present\n"
        b"2026-09-01,E999,Nobody Real,Present\n"
        b"not-a-date,E002,Bob Smith,Present\n"
        b"2026-09-01,E001,Alice Johnson,Present\n"
        b"2026-09-02,E002,Bob Smith,Maybe\n"
    )
    body = upload(ingest_api, auth("a_hr_admin"), "mixed.csv", content=csv).json()
    assert body["status"] == "completed"
    assert body["counts"]["records_created"] == 2 and body["counts"]["row_failures"] == 3
    reasons = {f["locator"]: f["reason"] for f in body["failures"]}
    assert "not found" in reasons["row=3"]
    assert "date" in reasons["row=4"]
    assert "duplicate" in reasons["row=5"]
    unknown = records("source_file = 'mixed.csv' AND employee_id = 'E002'")[0]
    assert unknown["status"] == "unknown" and unknown["review_required"]
    assert any("unrecognized status" in r for r in unknown["review_reasons"])
    assert body["counts"]["review_required"] == 1


def test_transient_error_is_retried(ingest_api, auth, monkeypatch):
    calls = {"n": 0}
    real = service.PARSERS["csv"]

    def flaky(content, filename):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TransientError("storage temporarily unavailable")
        return real(content, filename)

    monkeypatch.setitem(service.PARSERS, "csv", flaky)
    body = upload(ingest_api, auth("a_hr_admin"), "tenant_a_sep_v2.csv").json()
    assert body["status"] == "completed" and body["attempts"] == 2
    assert "retrying" in [h["status"] for h in body["stage_history"]]
    assert body["counts"]["records_created"] == 176


def test_exhausted_retries_then_manual_retry(ingest_api, auth, monkeypatch):
    real = service.PARSERS["csv"]
    monkeypatch.setitem(service.PARSERS, "csv", lambda c, f: (_ for _ in ()).throw(OSError("disk")))
    body = upload(ingest_api, auth("a_hr_admin"), "tenant_a_sep_v2.csv").json()
    assert body["status"] == "failed" and body["attempts"] == 3
    monkeypatch.setitem(service.PARSERS, "csv", real)
    again = ingest_api.post(f"/v1/jobs/{body['job_id']}/retry", headers=auth("a_hr_admin")).json()
    assert again["status"] == "completed" and again["counts"]["records_created"] == 176
    assert (
        ingest_api.post(f"/v1/jobs/{body['job_id']}/retry", headers=auth("a_hr_admin")).status_code
        == 409
    )


def test_worker_path(ingest_api, auth, monkeypatch):
    """Async mode: API enqueues to Redis; an RQ worker processes the job."""
    from rq import Queue, SimpleWorker

    from app.ingestion.worker import get_queue_connection

    monkeypatch.setattr(service.get_settings(), "ingest_sync", False)
    conn = get_queue_connection()
    Queue("ingestion", connection=conn).empty()
    r = upload(ingest_api, auth("a_hr_admin"), "tenant_a_sales_sep.xlsx")
    assert r.status_code == 202 and r.json()["status"] == "queued"
    SimpleWorker([Queue("ingestion", connection=conn)], connection=conn).work(burst=True)
    job = ingest_api.get(f"/v1/jobs/{r.json()['job_id']}", headers=auth("a_hr_admin")).json()
    assert job["status"] == "completed" and job["counts"]["records_created"] == 87
