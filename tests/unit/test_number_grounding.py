"""Grounding: numbers, dates, ids and names in an answer must come from the results."""

import datetime as dt

import pytest

from app.governance.grounding import check

pytestmark = pytest.mark.unit

ROWS = [{"attendance_pct": 82.0, "present_days": 95.5, "scheduled_days": 104}]
SEP = (dt.date(2026, 9, 1), dt.date(2026, 9, 30))


@pytest.mark.parametrize("stated", ["82", "82.0", "82%", "82.0 %"])
def test_formatting_variants_are_accepted(stated):
    assert check(f"Attendance was {stated} in September.", ROWS, period=SEP).ok


def test_rounding_down_precision_is_accepted():
    rows = [{"attendance_pct": 91.83}]
    for text in ("91.83%", "91.8%", "92%"):
        assert check(f"It was {text}.", rows).ok, text


def test_hallucinated_numbers_are_rejected():
    for text in (
        "Attendance was 97.5%.",
        "Attendance was 91.84%.",
        "It was 83%.",
        "Out of 105 days.",
    ):
        result = check(text, ROWS)
        assert not result.ok and "not in results" in result.problems[0], text


def test_derived_counts_and_question_numbers():
    rows = [{"employee_id": "E001"}, {"employee_id": "E002"}, {"employee_id": "E003"}]
    assert check("3 employees were present: E001, E002 and E003.", rows).ok
    assert check("In week 2 nobody was late.", [], question="Who was late in week 2?").ok


def test_dates_must_match_results_or_period():
    rows = [{"attendance_date": "2026-09-15", "status": "conflict"}]
    period = (dt.date(2026, 9, 15), dt.date(2026, 9, 15))
    assert check("On 15/09/2026 the sources disagree.", rows, period=period).ok
    assert check("On 15 September 2026 the sources disagree.", rows, period=period).ok
    assert not check("On 16/09/2026 the sources disagree.", rows, period=period).ok
    assert not check("On 2026-10-01 nothing happened.", rows, period=period).ok


def test_times_and_employee_ids():
    rows = [{"employee_id": "E001", "check_in": "09:13", "status": "present"}]
    assert check("E001 checked in at 09:13.", rows).ok
    assert not check("E001 checked in at 08:00.", rows).ok
    assert not check("E101 checked in at 09:13.", rows).ok


def test_names_must_come_from_results():
    rows = [{"employee_id": "E002", "employee_name": "Bob Smith", "attendance_pct": 60.0}]
    assert check("Bob Smith (E002) had the lowest attendance at 60%.", rows).ok
    result = check("John Carter (E002) had the lowest attendance at 60%.", rows)
    assert not result.ok and "John Carter" in result.problems[0]


def test_common_capitalised_phrases_are_not_names():
    rows = [{"department": "Engineering", "attendance_pct": 91.83}]
    assert check("The Engineering Department reached 91.83% Overall Attendance.", rows).ok
