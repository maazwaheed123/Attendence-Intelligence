"""Exporting a stored answer exports exactly the evidence it cited."""

import pytest

from tests.support.export import export, json_rows, key, pdf_ids, xlsx_rows
from tests.support.llm import ask

pytestmark = pytest.mark.e2e


def _cited(response: dict) -> list[str]:
    return list(dict.fromkeys(c.get("record_id") or c["chunk_id"] for c in response["citations"]))


def test_q1_export_equals_citations(api, auth):
    q = ask(api, auth, "a_eng_manager", "Who was present on 1 September 2026?")
    assert len(q["citations"]) == 4
    rid = q["request_id"]
    files = {
        fmt: export(api, auth, "a_eng_manager", fmt, source="query", request_id=rid)
        for fmt in ("json", "xlsx", "pdf")
    }
    assert {r.status_code for r in files.values()} == {200}
    meta, rows = json_rows(files["json"].content)
    ids = [key(r) for r in rows]
    assert ids == _cited(q)
    assert [key(r) for r in xlsx_rows(files["xlsx"].content)[1]] == ids
    assert pdf_ids(files["pdf"].content) == ids
    assert meta["source"] == {"type": "query", "request_id": rid, "status": "answered",
                              "citation_total": 4}  # fmt: skip
    by_id = {c["record_id"]: c for c in q["citations"]}
    for r in rows:
        assert (r["source_file"], r["source_locator"]) == (
            by_id[r["record_id"]]["source_file"],
            by_id[r["record_id"]]["locator"],
        )
        assert r["status"] == "present" and r["attendance_date"] == "2026-09-01"


def test_conflict_answer_exports_both_sources(api, auth):
    q = ask(api, auth, "a_hr_admin", "Was Bob present on 15 September?")
    _, rows = json_rows(
        export(api, auth, "a_hr_admin", source="query", request_id=q["request_id"]).content
    )
    assert [key(r) for r in rows] == _cited(q)
    assert sorted(r["status"] for r in rows) == ["absent", "present"]
    assert all(r["conflict"] for r in rows)


def test_document_answer_exports_chunks(api, auth):
    q = ask(api, auth, "a_hr_admin", "What did the manager note about Bob's late arrivals?")
    assert q["citations"]
    _, rows = json_rows(
        export(api, auth, "a_hr_admin", source="query", request_id=q["request_id"]).content
    )
    assert [r["chunk_id"] for r in rows] == [c["chunk_id"] for c in q["citations"]]
    assert all(r["excerpt"] for r in rows)
    assert any(r["row_type"] == "document" for r in rows)


def test_unavailable_answer_exports_nothing(api, auth):
    q = ask(api, auth, "a_eng_manager", "What is the company revenue?")
    r = export(api, auth, "a_eng_manager", source="query", request_id=q["request_id"])
    meta, rows = json_rows(r.content)
    assert rows == [] and meta["record_count"] == 0


def test_query_source_requires_request_id(api, auth):
    assert export(api, auth, "a_eng_manager", source="query").status_code == 422
