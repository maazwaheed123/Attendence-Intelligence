"""The allow-list validator for LLM-written SQL: what passes, what never does."""

import pytest

from app.retrieval.sql.validator import MAX_LIMIT, SqlRejected, validate

pytestmark = [pytest.mark.unit, pytest.mark.security]

PCT = "round(100 * sum(present_value) / nullif(sum(is_scheduled::int), 0), 2)"

ALLOWED = [
    f"SELECT {PCT} AS attendance_pct FROM v_attendance WHERE attendance_date BETWEEN '2026-09-01' AND '2026-09-30'",
    "SELECT employee_id, employee_name FROM v_attendance WHERE attendance_date = '2026-09-01' AND status = 'present' ORDER BY employee_id",
    f"SELECT entity_id, department, {PCT} AS pct FROM v_attendance GROUP BY entity_id, department ORDER BY pct DESC LIMIT 1",
    "SELECT count(*) AS n, count(DISTINCT employee_id) FROM v_attendance WHERE status = 'absent'",
    "SELECT to_char(attendance_date, 'DD/MM/YYYY'), date_trunc('month', attendance_date), extract(dow FROM attendance_date) FROM v_attendance",
    "SELECT lower(status), upper(employee_name), coalesce(total_hours, 0), CASE WHEN conflict THEN 1 ELSE 0 END FROM v_attendance",
    "SELECT count(*) FILTER (WHERE conflict) AS conflicts FROM v_attendance",
    "SELECT * FROM v_attendance WHERE employee_id IN ('E001', 'E002') AND check_in > '09:30'",
    "SELECT a.employee_id FROM v_attendance AS a JOIN v_attendance AS b ON a.employee_id = b.employee_id AND b.status = 'absent'",
    "SELECT employee_id FROM v_attendance WHERE status IN (SELECT status FROM v_attendance WHERE conflict)",
    "select employee_id from v_attendance;",  # one trailing semicolon is tolerated
    "SELECT * FROM v_attendance WHERE employee_name ILIKE '%son%'",
]

MALICIOUS = [
    # other tables / catalogs
    "SELECT * FROM attendance_records WHERE tenant_id='tenant_b'",
    "SELECT * FROM employees",
    "SELECT phone_enc FROM employees",
    "SELECT * FROM audit_events",
    "SELECT * FROM public.v_attendance",
    "SELECT * FROM pg_catalog.pg_tables",
    "SELECT * FROM information_schema.tables",
    "SELECT * FROM pg_shadow",
    # statement types
    "DROP TABLE employees",
    "SELECT 1; DROP TABLE employees",
    "SELECT * FROM v_attendance; DELETE FROM attendance_records",
    "DELETE FROM attendance_records",
    "UPDATE attendance_records SET status = 'present'",
    "INSERT INTO audit_events (event_type) VALUES ('x')",
    "COPY attendance_records TO '/tmp/x'",
    "SET app.tenant_id = 'tenant_b'",
    "SELECT * INTO stolen FROM v_attendance",
    "SELECT * FROM v_attendance FOR UPDATE",
    "WITH x AS (SELECT * FROM attendance_records) SELECT * FROM x",
    "SELECT employee_id FROM v_attendance UNION SELECT employee_name FROM employees",
    "SELECT employee_id FROM v_attendance UNION ALL SELECT employee_id FROM v_attendance",
    # dangerous functions
    "SELECT pg_sleep(10)",
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT set_config('app.tenant_id', 'tenant_b', false)",
    "SELECT current_setting('app.tenant_id')",
    "SELECT dblink('host=evil', 'SELECT 1')",
    "SELECT lo_import('/etc/passwd')",
    "SELECT version()",
    "SELECT current_user",
    "SELECT query_to_xml('select * from employees', true, true, '')",
    "SELECT generate_series(1, 100000000)",
    "SELECT employee_id, rank() OVER (ORDER BY employee_id) FROM v_attendance",
    "SELECT 'employees'::regclass",
    "SELECT * FROM v_attendance WHERE employee_name ~ '.*' OR status = 'x'",
    "SELECT * FROM v_attendance WHERE EXISTS (SELECT 1 FROM employees)",
    # comments, quoting tricks, parameters
    "SELECT * FROM v_attendance -- WHERE tenant_id = 'tenant_a'",
    "SELECT * FROM v_attendance /* hidden */",
    "SELECT $$x$$",
    "SELECT * FROM v_attendance WHERE employee_id = $1",
    # columns / joins / limits
    "SELECT raw_values FROM v_attendance",
    "SELECT phone_enc FROM v_attendance",
    "SELECT * FROM v_attendance, v_attendance",
    "SELECT * FROM v_attendance a CROSS JOIN v_attendance b",
    "SELECT * FROM v_attendance a JOIN v_attendance b ON a.x = b.x JOIN v_attendance c ON b.x = c.x",
    "SELECT * FROM v_attendance LIMIT (SELECT 1000)",
    "",
    "   ",
    "this is not sql",
]


@pytest.mark.parametrize("sql", ALLOWED)
def test_allowed_sql_passes(sql):
    out = validate(sql)
    assert out.upper().startswith("SELECT")
    assert "LIMIT" in out.upper()


@pytest.mark.parametrize("sql", MALICIOUS)
def test_malicious_sql_rejected(sql):
    with pytest.raises(SqlRejected):
        validate(sql)


def test_enough_attack_strings():
    assert len(MALICIOUS) >= 25


def test_limit_added_and_clamped():
    assert validate("SELECT employee_id FROM v_attendance").endswith(f"LIMIT {MAX_LIMIT}")
    assert validate("SELECT employee_id FROM v_attendance LIMIT 100000").endswith(
        f"LIMIT {MAX_LIMIT}"
    )
    assert validate("SELECT employee_id FROM v_attendance LIMIT 5").endswith("LIMIT 5")


def test_tenant_filter_is_syntactically_allowed():
    """Not the validator's job: RLS makes this return zero rows (tests/security/test_sql_attacks)."""
    assert validate("SELECT * FROM v_attendance WHERE tenant_id = 'tenant_b'")


def test_rejection_messages_are_specific():
    cases = {
        "SELECT pg_sleep(1)": "PG_SLEEP",
        "SELECT * FROM employees": "only the v_attendance view",
        "DROP TABLE x": "only SELECT",
        "SELECT 1; SELECT 2": "multiple statements",
        "SELECT secret FROM v_attendance": "unknown column",
    }
    for sql, fragment in cases.items():
        with pytest.raises(SqlRejected, match=fragment):
            validate(sql)
