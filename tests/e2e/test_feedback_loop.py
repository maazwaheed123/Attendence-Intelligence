"""Feedback / training loop: submission, repeat-query improvement, versions, rollback."""

import pytest
from sqlalchemy import text

from app.db.session import owner_session
from tests.support.feedback import IDEAL, Q2, ask_id, clean_feedback, submit  # noqa: F401

pytestmark = [pytest.mark.e2e, pytest.mark.usefixtures("clean_feedback")]


def _improve(api, auth, persona="a_reviewer", ideal=IDEAL):
    original = ask_id(api, auth, persona)
    r = submit(api, auth, persona, original["request_id"], ideal=ideal)
    assert r.status_code == 200, r.text
    return original, r.json()


def test_reviewer_submission_creates_active_v1_and_verifies(api, auth):
    original, fb = _improve(api, auth)
    assert original["answer"] != IDEAL and original["applied_feedback"] is None
    assert (fb["version"], fb["status"]) == (1, "active")
    v = fb["validation"]
    assert v["equivalent"] and v["sql_valid"] and v["reasons"] == []
    assert {"attendance_pct", "present_days", "scheduled_days", "date_from", "date_to"} <= set(
        v["placeholders"]
    )
    assert fb["scope"] == {
        "tenant_id": "tenant_a",
        "product_id": "attendance_ai",
        "module": "attendance",
        "entity_id": "engineering",
    }
    ver = fb["verification"]
    assert ver["applied"] and ver["matches_ideal"] and ver["similarity"] == 1.0
    assert ver["rerun_answer"] == IDEAL


def test_repeat_query_is_improved_with_live_numbers(api, auth):
    _, fb = _improve(api, auth)
    for persona in ("a_eng_manager", "a_reviewer", "a_hr_admin"):
        r = ask_id(api, auth, persona)
        assert r["answer"] == IDEAL, persona
        assert r["applied_feedback"]["example_id"] == fb["example_id"]
        assert r["citations"]  # still cited from the live records
    with owner_session() as s:
        n = s.execute(
            text("SELECT times_applied FROM feedback_examples WHERE example_id = :e"),
            {"e": fb["example_id"]},
        ).scalar_one()
    assert n >= 3


def test_paraphrase_is_improved(api, auth):
    _improve(api, auth)
    r = ask_id(
        api,
        auth,
        "a_eng_manager",
        "What was the average attendance percentage for Engineering in September?",
    )
    assert r["applied_feedback"] is not None and "calculated from 95.5" in r["answer"]


def test_template_recomputes_values_for_another_period(api, auth):
    _improve(api, auth)
    r = ask_id(api, auth, "a_eng_manager", "What was Engineering's average attendance % in week 2?")
    if r["applied_feedback"]:  # same kind of question, different data
        assert "from 07/09/2026 to 11/09/2026 was 92%" in r["answer"]
        assert "91.83" not in r["answer"]


def test_new_version_supersedes_then_rollback_and_deactivate(api, auth):
    _, v1 = _improve(api, auth)
    ideal2 = "From 01/09/2026 to 30/09/2026 Engineering attendance was 91.83% (95.5 of 104 scheduled days)."
    _, v2 = _improve(api, auth, ideal=ideal2)
    assert (v2["version"], v2["status"]) == (2, "active")
    assert ask_id(api, auth, "a_eng_manager")["answer"] == ideal2
    old = api.get(f"/v1/feedback/{v1['example_id']}", headers=auth("a_reviewer")).json()
    assert old["status"] == "inactive"

    rb = api.post(f"/v1/feedback/{v2['example_id']}/rollback", headers=auth("a_reviewer"))
    assert rb.status_code == 200 and rb.json()["active"]["example_id"] == v1["example_id"]
    assert ask_id(api, auth, "a_eng_manager")["answer"] == IDEAL

    d = api.post(f"/v1/feedback/{v1['example_id']}/deactivate", headers=auth("a_reviewer"))
    assert d.status_code == 200 and d.json()["status"] == "inactive"
    back = ask_id(api, auth, "a_eng_manager")
    assert (
        back["applied_feedback"] is None and back["answer"] != IDEAL and "91.83%" in back["answer"]
    )
    listed = api.get("/v1/feedback", headers=auth("a_reviewer")).json()["examples"]
    assert {e["example_id"] for e in listed} >= {v1["example_id"], v2["example_id"]}


def test_rollback_without_earlier_version_is_409(api, auth):
    _, v1 = _improve(api, auth)
    assert (
        api.post(
            f"/v1/feedback/{v1['example_id']}/rollback", headers=auth("a_reviewer")
        ).status_code
        == 409
    )


def test_wrong_numbers_are_rejected_and_not_applied(api, auth):
    _, fb = _improve(api, auth, ideal=IDEAL.replace("91.83%", "93.5%"))
    assert fb["status"] == "rejected" and fb["verification"] is None
    assert any("not supported by evidence: 93.5" in r for r in fb["validation"]["reasons"])
    assert ask_id(api, auth, "a_eng_manager")["applied_feedback"] is None


def test_feedback_submission_and_application_are_audited(api, auth):
    _, fb = _improve(api, auth)
    r = ask_id(api, auth, "a_eng_manager")
    with owner_session() as s:
        kinds = set(
            s.execute(
                text("SELECT event_type FROM audit_events WHERE details->>'example_id' = :e"),
                {"e": fb["example_id"]},
            ).scalars()
        )
        applied = s.execute(
            text(
                "SELECT 1 FROM audit_events WHERE request_id = :r AND event_type = 'feedback_applied'"
            ),
            {"r": r["request_id"]},
        ).first()
    assert {"feedback_submitted", "feedback_applied"} <= kinds and applied


def test_document_feedback_is_style_guidance(api, auth, monkeypatch):
    from tests.support.llm import prompts_text, scripted

    q = "What did the manager note about Bob's late arrivals?"
    original = ask_id(api, auth, "a_hr_admin", q)
    fb = submit(
        api,
        auth,
        "a_hr_admin",
        original["request_id"],
        ideal="Start with the employee name, then list the dates.",
    ).json()
    assert fb["status"] == "active" and fb["validation"]["kind"] == "style_guidance"
    mock = scripted(monkeypatch, answer={"answer": "", "insufficient": True})
    ask_id(api, auth, "a_hr_admin", q)
    assert "Reviewer style example" in prompts_text(
        mock
    ) and "Start with the employee name" in prompts_text(mock)
