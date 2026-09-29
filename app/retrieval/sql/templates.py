"""Deterministic SQL per intent: the fallback when no model is available or its SQL is
rejected, and the cross-check for LLM-written SQL on templatable questions.

Values are always bind parameters, never formatted into the SQL. The metric is the
one fixed in v_attendance (present_value / is_scheduled), same as expected_results.
"""

import math
import re
from dataclasses import dataclass, field, replace

from app.retrieval.types import Slots

PCT = "round(100 * sum(present_value) / nullif(sum(is_scheduled::int), 0), 2)"


@dataclass
class TemplateQuery:
    intent: str
    sql: str
    params: dict
    where: str
    group_cols: tuple[str, ...] = ()
    extra: dict = field(default_factory=dict)


def base_where(s: Slots, *, with_status: bool = False) -> tuple[str, dict]:
    conds = ["attendance_date BETWEEN :date_from AND :date_to"]
    params: dict = {"date_from": s.date_from, "date_to": s.date_to}
    if s.employee_id:
        conds.append("employee_id = :employee_id")
        params["employee_id"] = s.employee_id
    if s.entity_id:
        conds.append("entity_id = :entity_id")
        params["entity_id"] = s.entity_id
    if with_status and s.status:
        conds.append("status = :status")
        params["status"] = s.status
    return " AND ".join(conds), params


def build(intent: str, s: Slots) -> TemplateQuery | None:
    """TemplateQuery for the intent, or None when required slots are missing."""
    if s.date_from is None or s.date_to is None:
        return None
    if intent == "attendance_pct":
        w, p = base_where(s)
        sql = (
            f"SELECT {PCT} AS attendance_pct, sum(present_value) AS present_days, "
            "sum(is_scheduled::int) AS scheduled_days, "
            "count(*) FILTER (WHERE conflict) AS conflict_days "
            f"FROM v_attendance WHERE {w}"
        )
        return TemplateQuery(intent, sql, p, w)
    if intent == "rank":
        w, p = base_where(replace(s, employee_id=None))
        keys = (
            ("entity_id", "department")
            if s.group_by == "department"
            else ("employee_id", "employee_name")
        )
        order = "DESC" if s.rank_direction != "lowest" else "ASC"
        sql = (
            f"SELECT {keys[0]}, {keys[1]}, {PCT} AS attendance_pct, "
            "sum(present_value) AS present_days, sum(is_scheduled::int) AS scheduled_days "
            f"FROM v_attendance WHERE {w} GROUP BY {keys[0]}, {keys[1]} "
            f"HAVING sum(is_scheduled::int) > 0 ORDER BY attendance_pct {order}, {keys[0]}"
        )
        return TemplateQuery(intent, sql, p, w, group_cols=(keys[0],))
    if intent == "list_by_status" and s.status:
        w, p = base_where(s, with_status=True)
        sql = (
            "SELECT employee_id, employee_name, department, count(*) AS days "
            f"FROM v_attendance WHERE {w} "
            "GROUP BY employee_id, employee_name, department ORDER BY employee_id"
        )
        return TemplateQuery(intent, sql, p, w)
    if intent == "count_by_status" and s.status:
        w, p = base_where(s, with_status=True)
        sql = (
            "SELECT count(*) AS employee_days, count(DISTINCT employee_id) AS employees "
            f"FROM v_attendance WHERE {w}"
        )
        return TemplateQuery(intent, sql, p, w)
    if intent == "employee_status_on_date" and s.employee_id and s.single_date:
        w, p = base_where(s)
        sql = (
            "SELECT employee_id, employee_name, attendance_date, status, reported_statuses, "
            "check_in, check_out, total_hours, conflict, source_record_ids "
            f"FROM v_attendance WHERE {w}"
        )
        return TemplateQuery(intent, sql, p, w)
    if intent == "hours":
        w, p = base_where(s)
        sql = (
            "SELECT sum(total_hours) AS total_hours, round(avg(total_hours), 2) AS avg_hours, "
            f"count(total_hours) AS days_with_hours FROM v_attendance WHERE {w}"
        )
        return TemplateQuery(intent, sql, p, w)
    return None


def has_answer(intent: str, rows: list[dict]) -> bool:
    """False when the result carries no fact (no rows, NULL aggregate)."""
    if intent in ("list_by_status", "count_by_status"):
        return True
    if not rows:
        return False
    if intent == "attendance_pct":
        return rows[0].get("attendance_pct") is not None
    if intent == "hours":
        return bool(rows[0].get("days_with_hours"))
    return True


def winners(rows: list[dict]) -> list[dict]:
    """Rank rows tied with the first one (rows are already ordered)."""
    return [r for r in rows if rows and r["attendance_pct"] == rows[0]["attendance_pct"]]


_EMP_ID = re.compile(r"^[A-Za-z]\d{2,}$")


def key_values(intent: str, rows: list[dict]) -> list:
    """The facts an answer to this intent states (what two paths must agree on)."""
    if not rows:
        return []
    r0 = rows[0]
    return {
        "attendance_pct": lambda: [r0["attendance_pct"]],
        "rank": lambda: [w[next(iter(w))] for w in winners(rows)] + [r0["attendance_pct"]],
        "list_by_status": lambda: sorted(r["employee_id"] for r in rows),
        "count_by_status": lambda: [r0["employee_days"]],
        "employee_status_on_date": lambda: [r0["status"]],
        "hours": lambda: [r0["total_hours"], r0["avg_hours"]],
    }[intent]()


def _flat(rows: list[dict]) -> list:
    out = []
    for r in rows:
        for v in r.values():
            out.extend(v if isinstance(v, list) else [v])
    return out


def _same(a, b) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, int | float) and isinstance(b, int | float):
        return math.isclose(float(a), float(b), abs_tol=0.01)
    return str(a).strip().lower() == str(b).strip().lower()


def agree(intent: str, template_rows: list[dict], llm_rows: list[dict]) -> bool:
    expected = key_values(intent, template_rows)
    if intent == "list_by_status":
        got = sorted({v for v in _flat(llm_rows) if isinstance(v, str) and _EMP_ID.match(v)})
        return got == expected
    if not expected:
        return not llm_rows or all(v in (None, 0) for v in _flat(llm_rows))
    candidates = _flat(
        llm_rows[: max(1, len(winners(template_rows)))] if intent == "rank" else llm_rows
    )
    return all(e is None or any(_same(e, c) for c in candidates) for e in expected)
