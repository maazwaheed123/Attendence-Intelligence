"""POST /v1/query contract, RBAC, persistence (redacted + hashed), audit, and the LLM paths."""

import hashlib

import pytest
from sqlalchemy import text

from app.db.session import owner_session
from tests.support.llm import ask, scripted

pytestmark = pytest.mark.integration

PCT = "round(100 * sum(present_value) / nullif(sum(is_scheduled::int), 0), 2)"
Q2 = "What was Engineering's average attendance % in September?"
Q2_SQL = (
    f"SELECT {PCT} AS attendance_pct FROM v_attendance "
    "WHERE attendance_date BETWEEN '2026-09-01' AND '2026-09-30' AND entity_id = 'engineering'"
)
KEYS = {
    "applied_feedback",
    "request_id", "status", "answer", "retrieval_mode", "context", "citations", "citation_total",
    "confidence", "confidence_band", "confidence_explanation", "unavailable_reason", "provider",
    "model", "fallback_path", "prompt_version", "retrieval_version", "warnings",
}  # fmt: skip


def test_response_contract(api, auth):
    r = ask(api, auth, "a_eng_manager", "Who was present on 1 September 2026?")
    assert set(r) == KEYS
    assert r["context"] == {
        "tenant_id": "tenant_a",
        "product_id": "attendance_ai",
        "module": "attendance",
        "role": "manager",
        "entity_scope": ["engineering"],
    }
    assert set(r["citations"][0]) == {"record_id", "source_file", "locator", "excerpt"}
    assert r["prompt_version"] == "p1.0" and 0 <= r["confidence"] <= 1


def test_debug_only_on_request(api, auth):
    r = ask(api, auth, "a_eng_manager", "Who was present on 1 September 2026?", include_debug=True)
    assert r["debug"]["intent"] == "list_by_status"
    assert "FROM v_attendance" in r["debug"]["sql"]
    assert r["debug"]["slots"]["date_from"] == "2026-09-01"


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"question": ""}, 422),
        ({"question": "   "}, 422),
        ({"question": "x" * 501}, 422),
        ({}, 422),
        (
            {
                "question": "attendance",
                "filters": {"date_from": "2026-09-30", "date_to": "2026-09-01"},
            },
            422,
        ),
        ({"question": "attendance", "filters": {"date_from": "not-a-date"}}, 422),
    ],
)
def test_validation(api, auth, body, code):
    assert api.post("/v1/query", json=body, headers=auth("a_eng_manager")).status_code == code


def test_rbac(api, auth):
    assert api.post("/v1/query", json={"question": "attendance"}).status_code == 401
    r = api.post("/v1/query", json={"question": "attendance"}, headers=auth("a_auditor"))
    assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN"
    for persona in ("a_hr_admin", "a_eng_manager", "a_reviewer", "a_employee_e001"):
        assert (
            api.post(
                "/v1/query", json={"question": "attendance"}, headers=auth(persona)
            ).status_code
            == 200
        )


def test_filters_narrow_the_answer(api, auth):
    r = ask(
        api,
        auth,
        "a_hr_admin",
        "What was the attendance?",
        filters={"entity_id": "hr", "date_from": "2026-09-07", "date_to": "2026-09-11"},
    )
    assert "Human Resources attendance for 07/09/2026 - 11/09/2026 was 100%" in r["answer"]


def test_persisted_redacted_and_hashed(api, auth):
    q = "Who was present on 1 September 2026? call me on +44 20 7946 0958"
    r = ask(api, auth, "a_eng_manager", q)
    with owner_session() as s:
        row = s.execute(
            text(
                "SELECT tenant_id, sub, question_redacted, question_hash, mode, sql_executed, "
                "response, prompt_version, retrieval_version FROM query_responses "
                "WHERE request_id = :r"
            ),
            {"r": r["request_id"]},
        ).one()
    assert row.tenant_id == "tenant_a" and row.sub == "user:a_eng_manager"
    assert "7946" not in row.question_redacted and "[PHONE]" in row.question_redacted
    assert row.question_hash == hashlib.sha256(" ".join(q.lower().split()).encode()).hexdigest()
    assert row.mode == "structured" and "v_attendance" in row.sql_executed
    assert row.response["answer"] == r["answer"]
    assert (row.prompt_version, row.retrieval_version) == ("p1.0", r["retrieval_version"])


def test_query_audit_event(api, auth):
    r = ask(api, auth, "a_eng_manager", "Who was present on 1 September 2026?")
    none = ask(api, auth, "a_eng_manager", "What was John Carter's attendance?")
    with owner_session() as s:
        ev = s.execute(
            text("SELECT * FROM audit_events WHERE request_id = :r AND event_type = 'query'"),
            {"r": r["request_id"]},
        ).one()
        denied = s.execute(
            text(
                "SELECT outcome, details FROM audit_events WHERE request_id = :r AND event_type = 'query'"
            ),
            {"r": none["request_id"]},
        ).one()
    assert (ev.tenant_id, ev.role, ev.query_mode, ev.outcome) == (
        "tenant_a",
        "manager",
        "structured",
        "answered",
    )
    assert sorted(ev.retrieved_source_ids) == sorted(c["record_id"] for c in r["citations"])
    assert ev.provider == r["provider"] and ev.fallback_path == r["fallback_path"]
    assert float(ev.confidence) == r["confidence"]
    assert ev.details["intent"] == "list_by_status" and len(ev.details["sql_hash"]) == 64
    assert "Who was present" not in str(ev.details)  # raw question never audited
    assert denied.outcome == "filtered_or_absent"


# ------------------------------------------------------------------ LLM paths


def test_llm_sql_agrees_with_template(api, auth, monkeypatch):
    scripted(
        monkeypatch,
        sql={"sql": Q2_SQL, "explanation": "pooled metric"},
        phrase={"answer": "Engineering attendance in September 2026 was 91.83%."},
    )
    r = ask(api, auth, "a_eng_manager", Q2)
    assert r["answer"] == "Engineering attendance in September 2026 was 91.83%."
    assert (r["provider"], r["model"], r["fallback_path"]) == ("mock", "mock-1", "mock")
    assert "agree" in r["confidence_explanation"] and r["confidence_band"] == "high"
    assert not any("disagreed" in w for w in r["warnings"])


def test_llm_sql_disagreement_uses_template(api, auth, monkeypatch):
    wrong = Q2_SQL.replace("entity_id = 'engineering'", "status = 'present'")  # 100%
    scripted(monkeypatch, sql={"sql": wrong})
    r = ask(api, auth, "a_eng_manager", Q2)
    assert "91.83%" in r["answer"]
    assert any("disagreed" in w for w in r["warnings"])
    assert r["confidence_band"] == "medium"


def test_second_sql_attempt_after_rejection(api, auth, monkeypatch):
    def sql(messages):
        rejected = "rejected" in messages[-1]["content"]
        return {"sql": Q2_SQL if rejected else "SELECT * FROM employees"}

    mock = scripted(monkeypatch, sql=sql)
    r = ask(api, auth, "a_eng_manager", Q2)
    assert "agree" in r["confidence_explanation"]
    assert any("rejected by the validator" in w for w in r["warnings"])
    assert sum(m[-1]["content"].startswith("TASK: sql") for m in mock.calls) == 2


def test_model_only_intent_uses_llm_sql_and_its_lineage(api, auth, monkeypatch):
    sql = (
        "SELECT employee_id, employee_name, count(*) AS absences FROM v_attendance "
        "WHERE status = 'absent' AND attendance_date BETWEEN '2026-09-01' AND '2026-09-30' "
        "GROUP BY employee_id, employee_name ORDER BY absences DESC, employee_id LIMIT 1"
    )
    scripted(monkeypatch, sql={"sql": sql}, phrase={"answer": "Bob Smith (E002) had 2 absences."})
    r = ask(api, auth, "a_hr_admin", "Who had the most absences in September?")
    assert r["status"] == "answered"
    assert r["answer"] == "Bob Smith (E002) had 2 absences."
    assert r["citation_total"] == 2
    assert all("E002" in c["excerpt"] and c["excerpt"].endswith("absent") for c in r["citations"])
    assert "without a deterministic cross-check" in r["confidence_explanation"]


def test_model_only_intent_without_a_model(api, auth, monkeypatch):
    scripted(monkeypatch)  # every stage returns junk
    r = ask(api, auth, "a_hr_admin", "Who had the most absences in September?")
    assert r["status"] == "unavailable" and r["unavailable_reason"] == "insufficient_evidence"


def test_like_patterns_in_llm_sql_execute(api, auth, monkeypatch):
    sql = (
        "SELECT count(*) AS n FROM v_attendance WHERE employee_name ILIKE '%son%' "
        "AND check_in > '09:30' AND attendance_date BETWEEN '2026-09-01' AND '2026-09-30'"
    )
    scripted(monkeypatch, sql={"sql": sql})
    r = ask(api, auth, "a_eng_manager", "Who had the most late arrivals in September?")
    assert r["status"] in ("answered", "unavailable")
    assert not any("failed to execute" in w for w in r["warnings"])


def test_generic_rendering_when_phrasing_fails(api, auth, monkeypatch):
    sql = (
        "SELECT employee_id, count(*) AS absences FROM v_attendance WHERE status = 'absent' "
        "GROUP BY employee_id ORDER BY absences DESC, employee_id LIMIT 2"
    )
    scripted(monkeypatch, sql={"sql": sql})
    r = ask(api, auth, "a_hr_admin", "Who had the most absences in September?")
    assert r["answer"].startswith("Result (2 rows): employee_id E002, absences 2")
    assert r["provider"] == "template"
