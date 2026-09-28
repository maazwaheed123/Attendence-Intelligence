"""Conflicting sources and records awaiting review are surfaced, never silently answered."""

import pytest

from tests.support.llm import ask

pytestmark = pytest.mark.e2e


def test_q17_conflict_needs_review(api, auth, expected):
    excluded = expected["by_persona"]["a_hr_admin"]["excluded_needs_review"]
    assert {"attendance_date": "2026-09-15", "employee_id": "E002"}.items() <= excluded[0].items()
    r = ask(api, auth, "a_hr_admin", "Was Bob present on 15 September?")
    assert r["status"] == "needs_review"
    assert "disagree" in r["answer"] and "absent and present" in r["answer"]
    assert r["confidence_band"] == "low" and "needs human review" in r["confidence_explanation"]
    assert len(r["citations"]) == 2


def test_q18_only_evidence_awaits_review(api, auth, expected):
    excluded = expected["by_persona"]["a_hr_admin"]["excluded_needs_review"]
    assert {
        "attendance_date": "2026-09-30",
        "employee_id": "E011",
        "evidence_status": "review_only",
    }.items() <= excluded[1].items()
    r = ask(api, auth, "a_hr_admin", "Was Lucas Martin present on 30 September?")
    assert r["status"] == "needs_review"
    assert "handwritten_ambiguous.png" in r["answer"] and "awaits human review" in r["answer"]
    assert r["confidence"] <= 0.5 and r["confidence_band"] == "low"
    assert [c["source_file"] for c in r["citations"]] == ["handwritten_ambiguous.png"]
    assert r["citations"][0]["excerpt"].endswith("awaiting review")


def test_review_records_are_flagged_next_to_an_answer(api, auth):
    r = ask(api, auth, "a_hr_admin", "Who was present on 30 September 2026?")
    assert r["status"] == "answered" and "E011" not in r["answer"]
    assert any("handwritten_ambiguous.png" in w and "review" in w for w in r["warnings"])


def test_review_records_outside_scope_are_invisible(api, auth):
    r = ask(api, auth, "a_eng_manager", "Who was present on 30 September 2026?")
    assert not any("review" in w for w in r["warnings"])


def test_other_tenant_employee_filter_is_no_data(api, auth):
    r = ask(
        api,
        auth,
        "b_manager",
        "Who was present on 1 September 2026?",
        filters={"employee_id": "E001"},
    )
    assert r["status"] == "unavailable" and r["unavailable_reason"] == "no_data_in_scope"
