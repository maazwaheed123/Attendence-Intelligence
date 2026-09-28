"""Citations are real, in-scope records whose locators match the source files."""

import pytest
from sqlalchemy import text

from app.db.session import owner_session
from scripts.datagen.truth import load_spec
from tests.support.llm import ask

pytestmark = pytest.mark.e2e

QUESTIONS = [
    ("a_eng_manager", "Who was present on 1 September 2026?"),
    ("a_eng_manager", "What was Engineering's average attendance % in September?"),
    ("a_hr_admin", "Which department had the lowest attendance in September?"),
    ("a_hr_admin", "Who had the lowest attendance in week 2?"),
    ("a_hr_admin", "Was Bob present on 15 September?"),
    ("a_hr_manager", "How many employees were on leave in September?"),
    ("a_employee_e001", "What is my attendance?"),
    ("b_manager", "Who was absent on 2 September 2026?"),
    ("x_other_product", "Who was present on 1 Sep?"),
]


def _manifest_locators(manifests):
    out = {}
    for name, m in manifests["files"].items():
        for row in m.get("rows", []):
            out[(name, row["locator"])] = (row["employee_id"], row["attendance_date"])
    return out


@pytest.mark.parametrize(("persona", "question"), QUESTIONS)
def test_citations_exist_are_in_scope_and_match_sources(api, auth, manifests, persona, question):
    r = ask(api, auth, persona, question)
    assert r["citations"], r
    assert len(r["citations"]) == min(50, r["citation_total"])
    p = load_spec()["personas"][persona]
    locators = _manifest_locators(manifests)
    ids = [c["record_id"] for c in r["citations"]]
    with owner_session() as s:
        rows = {
            str(row.record_id): row
            for row in s.execute(
                text(
                    "SELECT record_id, product_id, tenant_id, entity_id, employee_id, "
                    "attendance_date::text AS d, source_file, source_locator, is_active "
                    "FROM attendance_records WHERE record_id = ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": ids},
            )
        }
    assert set(rows) == set(ids), "every citation must be an existing record"
    for c in r["citations"]:
        row = rows[c["record_id"]]
        assert (row.tenant_id, row.product_id) == (p["tenant"], p["product"])
        assert p["entities"] == ["*"] or row.entity_id in p["entities"]
        if p.get("employee_id"):
            assert row.employee_id == p["employee_id"]
        assert row.is_active
        assert (c["source_file"], c["locator"]) == (row.source_file, row.source_locator)
        assert locators[(row.source_file, row.source_locator)] == (row.employee_id, row.d)
        assert row.employee_id in c["excerpt"] and row.d in c["excerpt"]


def test_conflict_cites_both_sources(api, auth):
    r = ask(api, auth, "a_hr_admin", "Was Bob present on 15 September?")
    assert r["status"] == "needs_review"
    files = sorted(c["source_file"] for c in r["citations"])
    assert "conflict_note.pdf" in files and len(files) == 2
    assert {c["excerpt"].rsplit(" | ", 1)[1] for c in r["citations"]} == {"absent", "present"}
    assert r["citation_total"] == 2


def test_superseded_version_is_never_cited(api, auth):
    r = ask(api, auth, "a_hr_admin", "Who was present on 10 September 2026?")
    assert all(c["source_file"] != "tenant_a_sep.csv" for c in r["citations"])
