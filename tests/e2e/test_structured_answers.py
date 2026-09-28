"""Structured answers over the API equal expected_results.json (Appendix K Q1-Q4 and more).

Default test chain (mock -> template): the mock model returns invalid output, so these
answers come from the deterministic template path; the LLM paths are covered in
tests/integration/test_query_llm_paths.py.
"""

import re

import pytest
from freezegun import freeze_time

from app.generation.answer_templates import num
from tests.support.llm import ask

pytestmark = pytest.mark.e2e

IDS = re.compile(r"\bE\d{3}\b")


def ids_in(text: str) -> list[str]:
    return sorted(set(IDS.findall(text)))


def test_q1_who_was_present(api, auth, expected):
    r = ask(api, auth, "a_eng_manager", "Who was present on 1 September 2026?")
    exp = expected["by_persona"]["a_eng_manager"]["by_date"]["2026-09-01"]
    assert r["status"] == "answered" and r["retrieval_mode"] == "structured"
    assert ids_in(r["answer"]) == exp["present"] == ["E001", "E002", "E003", "E012"]
    assert "E004" not in r["answer"]  # absent
    assert sorted(c["excerpt"].split(" | ")[1][:4] for c in r["citations"]) == exp["present"]
    assert r["citation_total"] == 4 and r["confidence_band"] == "high"


def test_q2_engineering_average(api, auth, expected):
    r = ask(api, auth, "a_eng_manager", "What was Engineering's average attendance % in September?")
    month = expected["by_persona"]["a_eng_manager"]["periods"]["month"]
    assert month["attendance_pct"] == 91.83
    assert "91.83%" in r["answer"]
    assert "95.5 present employee-days out of 104 scheduled" in r["answer"]
    assert "01/09/2026 - 30/09/2026" in r["answer"]
    assert r["citation_total"] == 111  # 5 x 22 employee-days; the conflict day cites 2 sources
    assert any("conflicting sources" in w for w in r["warnings"])


def test_q3_highest_department(api, auth, expected):
    r = ask(api, auth, "a_hr_admin", "Which department had the highest attendance in September?")
    rank = expected["by_persona"]["a_hr_admin"]["periods"]["month"]["department_rank"]["highest"]
    assert rank == {"keys": ["engineering"], "unique": True, "value": 91.83}
    assert r["answer"].startswith("Engineering had the highest attendance")
    assert "91.83%" in r["answer"]
    # citations cover the winning department only
    assert {c["excerpt"].split(" | ")[1][:4] for c in r["citations"]} <= {
        "E001",
        "E002",
        "E003",
        "E004",
        "E012",
    }


def test_q3_lowest_department(api, auth, expected):
    r = ask(api, auth, "a_hr_admin", "Which department had the lowest attendance in September?")
    assert r["answer"].startswith("Human Resources had the lowest attendance")
    assert "88.89%" in r["answer"]


def test_q4_lowest_week2(api, auth, expected):
    r = ask(api, auth, "a_hr_admin", "Who had the lowest attendance in week 2?")
    rank = expected["by_persona"]["a_hr_admin"]["periods"]["week2"]["employee_rank"]["lowest"]
    assert rank["keys"] == ["E002"] and rank["value"] == 60.0
    assert r["answer"].startswith("Bob Smith (E002) had the lowest attendance for week 2")
    assert "60%" in r["answer"] and "07/09/2026 - 11/09/2026" in r["answer"]
    assert r["citation_total"] == 5 and all("E002" in c["excerpt"] for c in r["citations"])


def test_month_highest_and_lowest_employee(api, auth, expected):
    month = expected["by_persona"]["a_hr_admin"]["periods"]["month"]["employee_rank"]
    hi = ask(api, auth, "a_hr_admin", "Who had the highest attendance in September?")
    lo = ask(api, auth, "a_hr_admin", "Who had the lowest attendance in September?")
    assert ids_in(hi["answer"]) == month["highest"]["keys"] == ["E001"]
    assert ids_in(lo["answer"]) == month["lowest"]["keys"] == ["E009"]
    assert f"{num(month['lowest']['value'])}%" in lo["answer"]


def test_ties_are_reported(api, auth, expected):
    week1 = expected["by_persona"]["a_eng_manager"]["periods"]["week1"]["employee_rank"]["highest"]
    assert not week1["unique"]
    r = ask(api, auth, "a_eng_manager", "Who had the highest attendance in week 1?")
    assert "tied" in r["answer"] and ids_in(r["answer"]) == week1["keys"]


PERSONAS = [
    "a_hr_admin",
    "a_eng_manager",
    "a_hr_manager",
    "a_employee_e001",
    "b_manager",
    "x_other_product",
]


@pytest.mark.parametrize("persona", PERSONAS)
def test_month_percentage_per_persona(api, auth, expected, persona):
    month = expected["by_persona"][persona]["periods"]["month"]
    r = ask(api, auth, persona, "What was the attendance percentage in September 2026?")
    assert f"was {num(month['attendance_pct'])}%" in r["answer"], r["answer"]
    assert f"{num(month['present_days'])} present" in r["answer"]
    assert f"out of {num(month['scheduled_days'])} scheduled" in r["answer"]


@pytest.mark.parametrize("persona", ["a_hr_admin", "b_manager"])
def test_department_percentages(api, auth, expected, persona):
    by_dept = expected["by_persona"][persona]["periods"]["month"]["by_department"]
    for entity_id, pct in by_dept.items():
        r = ask(
            api,
            auth,
            persona,
            "What was the attendance in September?",
            filters={"entity_id": entity_id},
        )
        assert f"was {num(pct)}%" in r["answer"], (entity_id, r["answer"])


def test_status_lists_for_every_day(api, auth, expected):
    by_date = expected["by_persona"]["a_eng_manager"]["by_date"]
    for day, statuses in by_date.items():
        d, m, y = day[8:], day[5:7], day[:4]
        r = ask(api, auth, "a_eng_manager", f"Who was absent on {d}/{m}/{y}?")
        assert ids_in(r["answer"]) == statuses.get("absent", []), (day, r["answer"])


def test_counts(api, auth, expected):
    day = expected["by_persona"]["a_hr_admin"]["by_date"]["2026-09-02"]
    r = ask(api, auth, "a_hr_admin", "How many employees were on leave on 2 September 2026?")
    assert r["answer"] == f"{len(day['leave'])} employees were on leave on 02/09/2026."
    r = ask(api, auth, "a_hr_admin", "How many employees were absent on 3 September 2026?")
    assert r["status"] == "answered"
    assert r["answer"] == "No employees were absent on 03/09/2026."


def test_employee_status_on_date(api, auth):
    r = ask(api, auth, "a_eng_manager", "Was Bob present on 9 September?")
    assert r["answer"].startswith("Bob Smith (E002) was absent on 09/09/2026")
    assert r["citation_total"] == 1


def test_employee_self_scope(api, auth, expected):
    r = ask(api, auth, "a_employee_e001", "What is my attendance?")
    assert r["answer"].startswith("Alice Johnson (E001) attendance")
    assert "100%" in r["answer"]


@freeze_time("2026-10-05")
def test_relative_period_last_month(api, auth, expected):
    r = ask(api, auth, "b_manager", "What was the attendance last month?")
    assert "90.87%" in r["answer"] and "September 2026" in r["answer"]


def test_hours(api, auth):
    r = ask(api, auth, "a_hr_admin", "What were Alice's total hours in week 2?")
    assert r["status"] == "answered" and "hours in total" in r["answer"]
    assert r["confidence_band"] == "high" and r["citation_total"] == 5


def test_q5_hybrid_answers_the_structured_part(api, auth):
    r = ask(api, auth, "a_eng_manager", "Show evidence that Alice was present on 3 Sep")
    assert r["status"] == "answered"
    assert r["answer"].startswith("Alice Johnson (E001) was present on 03/09/2026")
    # remarks are confidential; this persona has internal clearance (Step 10 hybrid)
    assert any("No supporting document text" in w for w in r["warnings"])
    assert r["citations"] and r["citations"][0]["source_file"].endswith((".csv", ".xlsx"))
