"""A hostile or confused model writes the SQL. Nothing it writes can leave the caller's scope."""

import pytest
from sqlalchemy import text

from app.db.session import owner_session
from app.retrieval.sql import executor
from app.retrieval.sql.validator import SqlRejected, validate
from tests.support.llm import ask, scripted

pytestmark = [pytest.mark.security, pytest.mark.e2e]

Q1 = "Who was present on 1 September 2026?"
Q1_IDS = "Alice Johnson (E001), Bob Smith (E002), Sarah Khan (E003) and Zoe Adams (E012)"
OTHER_INTENT = "Who had the most absences in September?"

ATTACKS = [
    "SELECT * FROM attendance_records WHERE tenant_id='tenant_b'",
    "SELECT * FROM v_attendance; DROP TABLE employees",
    "DROP TABLE employees",
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT set_config('app.tenant_id', 'tenant_b', true), * FROM v_attendance",
    "SELECT employee_id FROM v_attendance UNION SELECT employee_name FROM employees",
    "SELECT * FROM v_attendance -- AND tenant_id = 'tenant_a'",
    "SELECT phone_enc FROM employees",
]


def _counts():
    with owner_session() as s:
        return {
            t: s.execute(text(f"SELECT count(*) FROM {t}")).scalar_one()
            for t in ("employees", "attendance_records", "document_chunks")
        }


@pytest.mark.parametrize("attack", ATTACKS)
def test_rejected_sql_falls_back_to_the_template(api, auth, monkeypatch, attack):
    before = _counts()
    mock = scripted(monkeypatch, sql={"sql": attack, "explanation": "x"})
    r = ask(api, auth, "a_eng_manager", Q1)
    assert r["status"] == "answered" and Q1_IDS in r["answer"]
    assert any("rejected by the validator" in w for w in r["warnings"])
    assert sum("TASK: sql" in m[-1]["content"] for m in mock.calls) == 2
    assert any("The SQL was rejected" in m[-1]["content"] for m in mock.calls)
    assert _counts() == before


def test_tenant_filter_passes_the_validator_but_rls_returns_nothing(corpus_db, scope_for):
    sql = validate("SELECT * FROM v_attendance WHERE tenant_id = 'tenant_b'")
    assert executor.run(scope_for("a_eng_manager"), sql).rows == []
    both = validate("SELECT DISTINCT tenant_id, product_id FROM v_attendance")
    assert executor.run(scope_for("a_eng_manager"), both).rows == [
        {"tenant_id": "tenant_a", "product_id": "attendance_ai"}
    ]


def test_cross_tenant_sql_through_the_pipeline_is_no_data(api, auth, monkeypatch):
    """The database boundary, end to end: valid SQL naming tenant_b yields zero rows."""
    scripted(monkeypatch, sql={"sql": "SELECT * FROM v_attendance WHERE tenant_id='tenant_b'"})
    r = ask(api, auth, "a_eng_manager", OTHER_INTENT)
    assert r["status"] == "unavailable" and r["unavailable_reason"] == "no_data_in_scope"
    assert "E10" not in r["answer"] and r["citations"] == []


def test_or_true_cannot_widen_scope(api, auth, monkeypatch):
    sql = (
        "SELECT employee_id, employee_name, count(*) AS absences FROM v_attendance "
        "WHERE status = 'absent' OR 1 = 1 GROUP BY employee_id, employee_name ORDER BY employee_id"
    )
    scripted(monkeypatch, sql={"sql": sql})
    r = ask(api, auth, "a_eng_manager", OTHER_INTENT)
    for c in r["citations"]:
        assert c["excerpt"].split(" | ")[1][:4] in {"E001", "E002", "E003", "E004", "E012"}


def test_validator_blocks_what_rls_alone_would_not():
    """set_config inside model SQL could rewrite the scope for that transaction;
    the validator is the layer that stops it (defence in depth)."""
    for sql in (
        "SELECT set_config('app.tenant_id','tenant_b',true)",
        "SELECT current_setting('app.tenant_id')",
    ):
        with pytest.raises(SqlRejected):
            validate(sql)


def test_hallucinated_number_is_re_rendered(api, auth, monkeypatch):
    scripted(monkeypatch, phrase={"answer": "Engineering attendance was 97.5% in September."})
    r = ask(api, auth, "a_eng_manager", "What was Engineering's average attendance % in September?")
    assert "97.5" not in r["answer"] and "91.83%" in r["answer"]
    assert any("not supported by the data" in w for w in r["warnings"])
    assert r["provider"] == "template" and r["fallback_path"].endswith("template")


def test_hallucinated_person_is_re_rendered(api, auth, monkeypatch):
    scripted(monkeypatch, phrase={"answer": "John Carter (E101) had the lowest attendance at 60%."})
    r = ask(api, auth, "a_hr_admin", "Who had the lowest attendance in week 2?")
    assert "John Carter" not in r["answer"] and r["answer"].startswith("Bob Smith (E002)")


def test_sql_that_fails_at_runtime_is_discarded(api, auth, monkeypatch):
    scripted(monkeypatch, sql={"sql": "SELECT employee_id::int AS x FROM v_attendance"})
    r = ask(api, auth, "a_eng_manager", Q1)
    assert r["status"] == "answered" and Q1_IDS in r["answer"]
    assert any("failed to execute" in w for w in r["warnings"])


def test_question_with_sql_payload_is_refused(api, auth):
    before = _counts()
    r = ask(api, auth, "a_eng_manager", "Who was present on 1 Sep?'; DROP TABLE employees; --")
    assert r["status"] == "unavailable"
    assert _counts() == before
