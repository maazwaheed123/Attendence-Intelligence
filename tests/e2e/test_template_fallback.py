"""Every model down -> the deterministic template engine still answers correctly."""

import pytest

from tests.support.llm import all_down, ask

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize(
    ("persona", "question", "must_contain"),
    [
        (
            "a_eng_manager",
            "Who was present on 1 September 2026?",
            "Alice Johnson (E001), Bob Smith (E002)",
        ),
        ("a_eng_manager", "What was Engineering's average attendance % in September?", "91.83%"),
        (
            "a_hr_admin",
            "Which department had the highest attendance in September?",
            "Engineering had the highest",
        ),
        ("a_hr_admin", "Who had the lowest attendance in week 2?", "Bob Smith (E002)"),
        ("b_manager", "What was the attendance in September?", "90.87%"),
    ],
)
def test_template_answers_when_all_providers_fail(
    api, auth, monkeypatch, persona, question, must_contain
):
    providers = all_down(monkeypatch)
    r = ask(api, auth, persona, question)
    assert r["status"] == "answered"
    assert must_contain in r["answer"]
    assert r["provider"] == "template" and r["model"] == "deterministic"
    assert r["fallback_path"] == "ollama-primary>ollama-fallback>template"
    assert all(p.calls >= 1 for p in providers)  # the chain really was tried
    assert r["citations"] and r["confidence_band"] == "high"


def test_fallback_is_audited(api, auth, monkeypatch):
    from sqlalchemy import text

    from app.db.session import owner_session

    all_down(monkeypatch)
    r = ask(api, auth, "a_eng_manager", "Who was present on 1 September 2026?")
    with owner_session() as s:
        rows = s.execute(
            text(
                "SELECT event_type, outcome, fallback_path FROM audit_events "
                "WHERE request_id = :r ORDER BY event_id"
            ),
            {"r": r["request_id"]},
        ).all()
    kinds = [row[0] for row in rows]
    assert "llm_call" in kinds and "query" in kinds
    assert ("llm_call", "all_providers_failed") in [(k, o) for k, o, _ in rows]
    query = next(row for row in rows if row[0] == "query")
    assert query[2].endswith("template")
