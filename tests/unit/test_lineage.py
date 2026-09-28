"""Citation filters are derived from the query that produced the answer."""

import pytest

from app.retrieval.sql.lineage import NoLineage, citation_filter, excerpt
from app.retrieval.sql.validator import validate

pytestmark = pytest.mark.unit


def _filter(sql, rows=()):
    return citation_filter(validate(sql), list(rows))


def test_where_is_reused():
    source, where = _filter(
        "SELECT count(*) FROM v_attendance WHERE attendance_date = '2026-09-01' AND status = 'absent'"
    )
    assert source == "v_attendance"
    assert where == "attendance_date = '2026-09-01' AND status = 'absent'"


def test_no_where_means_all_rows_in_scope():
    assert _filter("SELECT count(*) FROM v_attendance")[1] == "TRUE"


def test_group_keys_restrict_to_returned_groups():
    sql = (
        "SELECT employee_id, round(100 * sum(present_value) / nullif(sum(is_scheduled::int), 0), 2) AS p "
        "FROM v_attendance WHERE attendance_date BETWEEN '2026-09-07' AND '2026-09-11' "
        "GROUP BY employee_id ORDER BY p ASC LIMIT 1"
    )
    _, where = _filter(sql, [{"employee_id": "E002", "p": 60.0}])
    assert "BETWEEN '2026-09-07' AND '2026-09-11'" in where
    assert where.endswith("employee_id IN ('E002')")


def test_group_key_not_in_result_is_ignored():
    sql = "SELECT count(*) AS n FROM v_attendance GROUP BY entity_id"
    assert _filter(sql, [{"n": 5}])[1] == "TRUE"


def test_group_with_no_rows_cites_nothing():
    sql = "SELECT employee_id, count(*) FROM v_attendance GROUP BY employee_id"
    assert _filter(sql, [{"employee_id": None, "count": 1}])[1].endswith("employee_id IN (NULL)")


def test_alias_is_kept():
    source, where = _filter("SELECT v.employee_id FROM v_attendance AS v WHERE v.status = 'absent'")
    assert source == "v_attendance AS v" and where == "v.status = 'absent'"


def test_join_has_no_single_filter():
    with pytest.raises(NoLineage):
        _filter(
            "SELECT a.employee_id FROM v_attendance a JOIN v_attendance b ON a.employee_id = b.employee_id"
        )


def test_quotes_in_group_values_are_escaped():
    sql = "SELECT employee_name, count(*) FROM v_attendance GROUP BY employee_name"
    _, where = _filter(sql, [{"employee_name": "O'Brien"}])
    assert "'O''Brien'" in where


def test_excerpt():
    row = {
        "attendance_date": "2026-09-01",
        "employee_id": "E001",
        "employee_name": "Alice Johnson",
        "status": "present",
    }
    assert excerpt(row) == "2026-09-01 | E001 Alice Johnson | present"
    conflict = {
        **row,
        "status": "conflict",
        "conflict": True,
        "reported_statuses": ["absent", "present"],
    }
    assert excerpt(conflict).endswith("conflicting sources: absent/present")
