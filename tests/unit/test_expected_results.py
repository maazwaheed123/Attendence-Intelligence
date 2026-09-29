"""Independent re-computation of expected results (does NOT reuse the generator's code)."""

import pytest

pytestmark = pytest.mark.unit

VALUE = {"present": 1, "wfh": 1, "half_day": 0.5}


def _pct(rows):
    sched = [r for r in rows if r["status"] != "holiday"]
    return round(100 * sum(VALUE.get(r["status"], 0) for r in sched) / len(sched), 2)


def _clean(truth, tenant, entity=None):
    return [
        r
        for r in truth["rows"]
        if r["tenant_id"] == tenant
        and r["evidence_status"] == "clean"
        and (entity is None or r["entity_id"] == entity)
    ]


def test_department_percentages(truth, expected):
    got = expected["by_persona"]["a_hr_admin"]["periods"]["month"]["by_department"]
    for dept in ("engineering", "hr", "sales"):
        assert got[dept] == _pct(_clean(truth, "tenant_a", dept))
    assert got == {"engineering": 91.83, "hr": 88.89, "sales": 89.76}


def test_employee_percentages_and_ranks(truth, expected):
    month = expected["by_persona"]["a_hr_admin"]["periods"]["month"]
    for emp, pct in month["by_employee"].items():
        rows = [r for r in _clean(truth, "tenant_a") if r["employee_id"] == emp]
        assert pct == _pct(rows), emp
    assert month["employee_rank"]["highest"]["keys"] == ["E001"]
    assert month["employee_rank"]["lowest"]["keys"] == ["E009"]
    assert month["department_rank"]["highest"]["keys"] == ["engineering"]
    assert month["department_rank"]["lowest"]["keys"] == ["hr"]


def test_conflict_and_review_excluded_from_aggregates(expected):
    eng = expected["by_persona"]["a_eng_manager"]["periods"]["month"]
    assert eng["by_employee"]["E002"] == 82.5
    sales = expected["by_persona"]["a_hr_admin"]["periods"]["month"]["by_employee"]
    assert sales["E011"] == 92.5


def test_week2_lowest_is_bob(expected):
    w2 = expected["by_persona"]["a_hr_admin"]["periods"]["week2"]
    assert w2["employee_rank"]["lowest"] == {"keys": ["E002"], "unique": True, "value": 60.0}


def test_scopes_are_isolated(expected):
    per = expected["by_persona"]
    ids = lambda p: {e for d in per[p]["by_date"].values() for lst in d.values() for e in lst}  # noqa: E731
    assert ids("a_eng_manager") == {"E001", "E002", "E003", "E004", "E012"}
    assert ids("a_employee_e001") == {"E001"}
    assert not ids("a_hr_admin") & ids("b_manager")
    assert per["x_other_product"]["record_count"] == 4
    assert per["x_other_product"]["by_date"]["2026-09-01"]["absent"] == ["E001"]
    assert per["a_eng_manager"]["by_date"]["2026-09-01"]["present"] == [
        "E001",
        "E002",
        "E003",
        "E012",
    ]


def test_tenant_b(truth, expected):
    month = expected["by_persona"]["b_manager"]["periods"]["month"]
    assert month["attendance_pct"] == _pct(_clean(truth, "tenant_b"))
    assert month["employee_rank"]["highest"]["keys"] == ["E102"]
    assert month["employee_rank"]["lowest"]["keys"] == ["E103"]
