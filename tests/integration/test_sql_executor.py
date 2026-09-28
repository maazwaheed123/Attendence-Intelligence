"""The executor's own safety layers, tested WITHOUT the validator in front of it:
even SQL that slipped past validation runs read-only, time-limited, capped and scoped."""

import pytest

from app.retrieval.sql import executor
from app.retrieval.sql.executor import SqlExecutionError

pytestmark = [pytest.mark.integration, pytest.mark.security]


def test_literals_are_not_placeholders(corpus_db, scope_for):
    rows = executor.run(
        scope_for("a_eng_manager"), "SELECT ':x' AS a, '50%' AS b, '%(y)s' AS c"
    ).rows
    assert rows == [{"a": ":x", "b": "50%", "c": "%(y)s"}]


def test_read_only_transaction(corpus_db, scope_for):
    with pytest.raises(SqlExecutionError, match="read-only"):
        executor.run(scope_for("a_hr_admin"), "UPDATE attendance_records SET status = 'present'")


def test_reader_role_has_no_pii_columns(corpus_db, scope_for):
    with pytest.raises(SqlExecutionError, match="permission denied"):
        executor.run(scope_for("a_hr_admin"), "SELECT phone_enc FROM employees")


def test_statement_timeout(corpus_db, scope_for):
    with pytest.raises(SqlExecutionError, match="statement timeout"):
        executor.run(scope_for("a_eng_manager"), "SELECT pg_sleep(4)")


def test_row_cap(corpus_db, scope_for):
    result = executor.run(scope_for("a_hr_admin"), "SELECT record_id FROM v_attendance", max_rows=3)
    assert len(result.rows) == 3 and result.truncated


def test_scope_applies_to_verbatim_sql(corpus_db, scope_for):
    rows = executor.run(
        scope_for("b_manager"), "SELECT DISTINCT tenant_id FROM attendance_records"
    ).rows
    assert rows == [{"tenant_id": "tenant_b"}]


def test_json_conversion(corpus_db, scope_for):
    row = executor.run(
        scope_for("a_eng_manager"),
        "SELECT record_id, attendance_date, check_in, present_value, source_record_ids "
        "FROM v_attendance WHERE employee_id = :e AND attendance_date = :d",
        {"e": "E001", "d": "2026-09-03"},
    ).rows[0]
    assert row["attendance_date"] == "2026-09-03" and len(row["check_in"]) == 5
    assert row["present_value"] == 1.0 and isinstance(row["source_record_ids"][0], str)
