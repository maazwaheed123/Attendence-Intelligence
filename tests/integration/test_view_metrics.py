"""v_attendance (queried as rag_reader under each persona's RLS scope) must reproduce
expected_results.json exactly. This proves the view's resolution, conflict and
metric rules match the business definitions, and that scoping changes the numbers
exactly as intended."""

import pytest
from sqlalchemy import text

from app.db.session import scoped_session

pytestmark = pytest.mark.integration

PCT = "round(100 * sum(present_value) / nullif(sum(is_scheduled::int), 0), 2)"
PERSONAS = [
    "a_hr_admin",
    "a_eng_manager",
    "a_hr_manager",
    "a_employee_e001",
    "b_manager",
    "x_other_product",
]


def _month_by(s, column):
    rows = s.execute(
        text(
            f"SELECT {column}, {PCT} FROM v_attendance "
            "WHERE attendance_date BETWEEN '2026-09-01' AND '2026-09-30' GROUP BY 1"
        )
    ).all()
    return {k: float(v) for k, v in rows}


@pytest.mark.parametrize("persona", PERSONAS)
def test_percentages_match_expected(corpus_db, scope_for, expected, persona):
    exp = expected["by_persona"][persona]["periods"]["month"]
    with scoped_session(scope_for(persona)) as s:
        overall = s.execute(text(f"SELECT {PCT} FROM v_attendance")).scalar_one()
        assert float(overall) == exp["attendance_pct"]
        assert _month_by(s, "employee_id") == exp["by_employee"]
        assert _month_by(s, "entity_id") == exp["by_department"]


@pytest.mark.parametrize("persona", PERSONAS)
def test_status_lists_by_date_match_expected(corpus_db, scope_for, expected, persona):
    exp = expected["by_persona"][persona]["by_date"]
    with scoped_session(scope_for(persona)) as s:
        rows = s.execute(
            text(
                "SELECT attendance_date::text, status, array_agg(employee_id ORDER BY employee_id) "
                "FROM v_attendance WHERE status <> 'conflict' GROUP BY 1, 2"
            )
        ).all()
    got = {}
    for d, st, ids in rows:
        got.setdefault(d, {})[st] = list(ids)
    assert got == exp


def test_week2_lowest(corpus_db, scope_for):
    with scoped_session(scope_for("a_hr_admin")) as s:
        emp, pct = s.execute(
            text(
                f"SELECT employee_id, {PCT} AS p FROM v_attendance "
                "WHERE attendance_date BETWEEN '2026-09-07' AND '2026-09-11' "
                "GROUP BY 1 ORDER BY p ASC LIMIT 1"
            )
        ).one()
    assert (emp, float(pct)) == ("E002", 60.0)


def test_conflict_is_flagged_not_counted(corpus_db, scope_for):
    with scoped_session(scope_for("a_hr_admin")) as s:
        row = s.execute(
            text(
                "SELECT status, reported_statuses, conflict, is_scheduled, present_value, source_count "
                "FROM v_attendance WHERE employee_id = 'E002' AND attendance_date = '2026-09-15'"
            )
        ).one()
    assert row.status == "conflict" and row.conflict
    assert list(row.reported_statuses) == ["absent", "present"]
    assert row.is_scheduled is False and float(row.present_value) == 0.0
    assert row.source_count == 2


def test_review_required_excluded_from_view(corpus_db, scope_for):
    with scoped_session(scope_for("a_hr_admin")) as s:
        in_view = s.execute(
            text(
                "SELECT count(*) FROM v_attendance WHERE employee_id='E011' AND attendance_date='2026-09-30'"
            )
        ).scalar_one()
        pending = s.execute(
            text(
                "SELECT source_file, review_required FROM attendance_records "
                "WHERE employee_id='E011' AND attendance_date='2026-09-30'"
            )
        ).all()
    assert in_view == 0
    assert pending == [("handwritten_ambiguous.png", True)]


def test_superseded_v1_rows_not_used(corpus_db, scope_for):
    """tenant_a_sep.csv (v1) had E006 absent on 10 Sep; v2 corrected it to present."""
    with scoped_session(scope_for("a_hr_admin")) as s:
        status, source = s.execute(
            text(
                "SELECT status, source_file FROM v_attendance "
                "WHERE employee_id='E006' AND attendance_date='2026-09-10'"
            )
        ).one()
        v1_active = s.execute(
            text(
                "SELECT count(*) FROM attendance_records WHERE source_file='tenant_a_sep.csv' AND is_active"
            )
        ).scalar_one()
    assert status == "present" and source != "tenant_a_sep.csv"
    assert v1_active == 0


def test_multi_source_day_resolves_to_one_row(corpus_db, scope_for):
    """29 Sep engineering appears in CSV v2 + printed scan PNG + scanned PDF."""
    with scoped_session(scope_for("a_eng_manager")) as s:
        rows = s.execute(
            text(
                "SELECT source_count, source_file FROM v_attendance "
                "WHERE employee_id='E001' AND attendance_date='2026-09-29'"
            )
        ).all()
    assert len(rows) == 1
    assert rows[0].source_count == 3
    assert rows[0].source_file == "tenant_a_sep_v2.csv"  # highest extraction confidence wins
