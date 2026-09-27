"""End-to-end: ingest every text-based sample file through the REAL pipeline (API ->
parse -> normalize -> RLS-scoped persist), then check v_attendance against
expected_results.json for every persona. OCR files join in Step 7."""

import pytest
from sqlalchemy import text

from app.db.session import scoped_session
from tests.support.ingest import upload

pytestmark = pytest.mark.e2e

UPLOADS = [
    ("a_hr_admin", "tenant_a_sep.csv"),
    ("a_hr_admin", "tenant_a_sep_v2.csv"),
    ("a_hr_admin", "tenant_a_sales_sep.xlsx"),
    ("a_eng_manager", "tenant_a_week2.docx"),
    ("a_hr_admin", "tenant_a_hr_contacts.xlsx"),
    ("b_manager", "tenant_b_sep.pdf"),
    ("a_hr_admin", "conflict_note.pdf"),
    ("a_eng_manager", "injection_memo.docx"),
    ("x_other_product", "other_product.csv"),
]
PCT = "round(100 * sum(present_value) / nullif(sum(is_scheduled::int), 0), 2)"
PERSONAS = [
    "a_hr_admin",
    "a_eng_manager",
    "a_hr_manager",
    "a_employee_e001",
    "b_manager",
    "x_other_product",
]


@pytest.fixture
def ingested(ingest_api, auth):
    results = {}
    for persona, name in UPLOADS:
        body = upload(ingest_api, auth(persona), name).json()
        assert body["status"] == "completed", (name, body)
        results[name] = body
    return results


def test_every_file_completed_without_row_failures(ingested):
    for name, body in ingested.items():
        assert body["counts"]["row_failures"] == 0, (name, body["failures"][:3])


def test_view_matches_expected_for_all_personas(ingested, scope_for, expected):
    for persona in PERSONAS:
        exp = expected["by_persona"][persona]
        with scoped_session(scope_for(persona)) as s:
            overall = s.execute(text(f"SELECT {PCT} FROM v_attendance")).scalar_one()
            by_emp = dict(
                s.execute(text(f"SELECT employee_id, {PCT} FROM v_attendance GROUP BY 1")).all()
            )
            by_date = s.execute(
                text(
                    "SELECT attendance_date::text, status, array_agg(employee_id ORDER BY employee_id) "
                    "FROM v_attendance WHERE status <> 'conflict' GROUP BY 1, 2"
                )
            ).all()
        assert float(overall) == exp["periods"]["month"]["attendance_pct"], persona
        assert {k: float(v) for k, v in by_emp.items()} == exp["periods"]["month"]["by_employee"], (
            persona
        )
        got = {}
        for d, st, ids in by_date:
            got.setdefault(d, {})[st] = list(ids)
        assert got == exp["by_date"], persona
