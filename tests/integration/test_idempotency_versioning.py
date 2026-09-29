import pytest
from sqlalchemy import text

from app.db.session import owner_session, scoped_session
from tests.support.ingest import GENERATED, records, upload

pytestmark = pytest.mark.integration


def test_same_file_twice_is_duplicate(ingest_api, auth):
    first = upload(ingest_api, auth("a_hr_admin"), "tenant_a_sep_v2.csv").json()
    n = len(records())
    second = upload(ingest_api, auth("a_hr_admin"), "tenant_a_sep_v2.csv").json()
    assert second["status"] == "duplicate" and second["completed"]
    assert second["duplicate_of"] == first["document_id"]
    assert second["checksum_sha256"] == first["checksum_sha256"]
    assert len(records()) == n


def test_same_content_different_name_is_duplicate(ingest_api, auth):
    upload(ingest_api, auth("a_hr_admin"), "tenant_a_sep_v2.csv")
    content = (GENERATED / "tenant_a_sep_v2.csv").read_bytes()
    body = upload(ingest_api, auth("a_hr_admin"), "renamed_copy.csv", content=content).json()
    assert body["status"] == "duplicate"


def test_reprocessing_same_document_does_not_duplicate(ingest_api, auth, scope_for):
    """Deterministic record ids + ON CONFLICT DO NOTHING: a re-run adds nothing."""
    from app.ingestion.service import process, scope_to_dict

    body = upload(ingest_api, auth("a_hr_admin"), "tenant_a_sep_v2.csv").json()
    n = len(records())
    with scoped_session(scope_for("a_hr_admin"), role="app") as db:
        db.execute(
            text("UPDATE ingestion_jobs SET status = 'queued' WHERE job_id = :j"),
            {"j": body["job_id"]},
        )
    assert process(body["job_id"], scope_to_dict(scope_for("a_hr_admin"))) == "completed"
    assert len(records()) == n


def test_changed_file_creates_new_version(ingest_api, auth, scope_for):
    v1 = upload(ingest_api, auth("a_hr_admin"), "tenant_a_sep.csv").json()
    v2 = upload(ingest_api, auth("a_hr_admin"), "tenant_a_sep_v2.csv").json()
    assert v1["logical_name"] == v2["logical_name"] == "tenant_a_sep"
    assert (v1["version"], v2["version"]) == (1, 2)
    assert v2["supersedes_document_id"] == v1["document_id"]
    diff = v2["counts"]["diff"]
    assert (diff["added"], diff["removed"], diff["changed"]) == (0, 0, 3)
    assert {(c["employee_id"], c["date"]) for c in diff["changed_rows"]} == {
        ("E006", "2026-09-10"),
        ("E007", "2026-09-17"),
        ("E003", "2026-09-22"),
    }
    assert all(not r["is_active"] for r in records("source_file = 'tenant_a_sep.csv'"))
    with scoped_session(scope_for("a_hr_admin")) as s:
        status, src = s.execute(
            text(
                "SELECT status, source_file FROM v_attendance WHERE employee_id='E006' AND attendance_date='2026-09-10'"
            )
        ).one()
        docs = dict(s.execute(text("SELECT filename, status FROM source_documents")).all())
    assert (status, src) == ("present", "tenant_a_sep_v2.csv")
    assert docs == {"tenant_a_sep.csv": "superseded", "tenant_a_sep_v2.csv": "completed"}


def test_explicit_logical_name(ingest_api, auth):
    content = (GENERATED / "tenant_a_sep_v2.csv").read_bytes()
    body = upload(
        ingest_api,
        auth("a_hr_admin"),
        "export_final.csv",
        content=content,
        logical_name="hr_monthly",
    ).json()
    assert body["logical_name"] == "hr_monthly"


@pytest.mark.parametrize("order", ["v1_first", "v2_first"])
def test_async_uploads_in_quick_succession_version_correctly(ingest_api, auth, monkeypatch, order):
    """Found while ingesting the corpus into the dev server (worker mode): v2 uploaded
    while v1 was still queued became a second "v1" and both stayed active."""
    from app.config import get_settings
    from app.ingestion import service

    queued = []
    monkeypatch.setattr(get_settings(), "ingest_sync", False)
    monkeypatch.setattr(service, "enqueue", lambda job_id, scope: queued.append((job_id, scope)))
    v1 = upload(ingest_api, auth("a_hr_admin"), "tenant_a_sep.csv").json()
    v2 = upload(ingest_api, auth("a_hr_admin"), "tenant_a_sep_v2.csv").json()
    assert (v1["version"], v2["version"]) == (1, 2)
    assert v2["supersedes_document_id"] == v1["document_id"]

    for job_id, scope in queued if order == "v1_first" else reversed(queued):
        assert service.process(job_id, scope) == "completed"
    assert all(not r["is_active"] for r in records("source_file = 'tenant_a_sep.csv'"))
    assert all(r["is_active"] for r in records("source_file = 'tenant_a_sep_v2.csv'"))
    with owner_session() as s:
        docs = dict(s.execute(text("SELECT filename, status FROM source_documents")).all())
    assert docs == {"tenant_a_sep.csv": "superseded", "tenant_a_sep_v2.csv": "completed"}


def test_concurrent_uploads_of_one_logical_name_get_distinct_versions(
    ingest_api, auth, monkeypatch
):
    import threading
    import time
    import types
    import uuid

    from app.ingestion import service

    def slow_uuid4():
        time.sleep(0.3)
        return uuid.uuid4()

    monkeypatch.setattr(service, "uuid", types.SimpleNamespace(uuid4=slow_uuid4, UUID=uuid.UUID))
    headers = auth("a_hr_admin")
    results = {}

    def send(name):
        results[name] = upload(ingest_api, headers, name).json()

    threads = [
        threading.Thread(target=send, args=(n,))
        for n in ("tenant_a_sep.csv", "tenant_a_sep_v2.csv")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    bodies = sorted(results.values(), key=lambda b: b["version"])
    assert [b["version"] for b in bodies] == [1, 2]
    assert bodies[1]["supersedes_document_id"] == bodies[0]["document_id"]
