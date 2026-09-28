"""Deterministic answer text (the "template" entry of the provider chain).

Used when no model is available, when a phrased answer fails grounding, and for
every unavailable/refusal response, so those never depend on a model. Every
number and name here comes straight from the result rows.
"""

from app.retrieval.rewrite import fmt_date, fmt_period
from app.retrieval.sql.templates import winners
from app.retrieval.types import Slots

STATUS_TEXT = {
    "present": "present",
    "absent": "absent",
    "leave": "on leave",
    "holiday": "on a public holiday",
    "wfh": "working from home",
    "half_day": "on a half day",
    "unknown": "recorded with an unclear status",
}

UNAVAILABLE = {
    "out_of_scope": (
        "This question is outside what this attendance service can answer. Ask about "
        "presence, absence, leave, attendance percentages or hours for a date or period."
    ),
    "not_permitted": (
        "Personal contact and identity details are not available through this service."
    ),
    "unsafe": (
        "This request cannot be carried out. The service only answers read-only questions "
        "about attendance data."
    ),
    "document": (
        "Answering from document text (notes, remarks, letters, memos) is not available yet. "
        "Ask about attendance figures instead."
    ),
    "no_document_evidence": ("No document evidence in your permitted scope answers this question."),
    "unsupported": (
        "The question could not be mapped to an attendance query. Try asking about presence, "
        "absence, attendance % or hours for a person, department, date or period."
    ),
}


def num(v) -> str:
    if v is None:
        return "n/a"
    f = float(v)
    return str(int(f)) if f.is_integer() else f"{f:.2f}".rstrip("0").rstrip(".")


def person(employee_id: str, name: str | None) -> str:
    return f"{name} ({employee_id})" if name else employee_id


def join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def period_text(s: Slots, date_format: str) -> str:
    span = fmt_period(s.date_from, s.date_to, date_format)
    if s.period_label and s.period_label != span:
        return f"{s.period_label} ({span})"
    return span


def subject(s: Slots) -> str:
    if s.employee_id:
        return person(s.employee_id, s.employee_name)
    if s.entity_id:
        return s.entity_name or s.entity_id
    return ""


def no_data(s: Slots, date_format: str, *, has_period: bool) -> str:
    if s.unresolved_person:
        who = "the requested employee"
    elif s.unresolved_entity:
        who = "the requested department"
    else:
        who = subject(s) or "this question"
    when = f" for {period_text(s, date_format)}" if has_period and s.date_from else ""
    return f"No attendance data for {who} in your permitted scope{when}."


def _days(n) -> str:
    return f"{num(n)} day" + ("" if float(n) == 1 else "s")


def render(intent: str, s: Slots, rows: list[dict], date_format: str) -> str:
    when = period_text(s, date_format)
    if intent == "attendance_pct":
        r = rows[0]
        unit = "present days" if s.employee_id else "present employee-days"
        who = subject(s) or "Overall"
        return (
            f"{who} attendance for {when} was {num(r['attendance_pct'])}% "
            f"({num(r['present_days'])} {unit} out of {num(r['scheduled_days'])} scheduled)."
        )
    if intent == "rank":
        top = winners(rows)
        direction = s.rank_direction or "highest"
        dept = s.group_by == "department"
        names = [
            (r.get("department") or r["entity_id"])
            if dept
            else person(r["employee_id"], r["employee_name"])
            for r in top
        ]
        r = top[0]
        unit = "present employee-days" if dept else "present days"
        detail = f"({num(r['present_days'])} {unit} out of {num(r['scheduled_days'])} scheduled)"
        if len(top) == 1:
            return (
                f"{names[0]} had the {direction} attendance for {when}: "
                f"{num(r['attendance_pct'])}% {detail}."
            )
        pct = num(r["attendance_pct"])
        return f"{join(names)} tied for the {direction} attendance for {when} at {pct}%."
    if intent == "list_by_status":
        status = STATUS_TEXT.get(s.status, s.status)
        prep = "on" if s.single_date else "during"
        if not rows:
            return f"No employees were recorded as {status} {prep} {when}."
        if s.single_date:
            names = [person(r["employee_id"], r["employee_name"]) for r in rows]
        else:
            names = [f"{r['employee_name']} ({r['employee_id']}, {_days(r['days'])})" for r in rows]
        n = len(rows)
        noun = "employee was" if n == 1 else "employees were"
        return f"{n} {noun} {status} {prep} {when}: {join(names)}."
    if intent == "count_by_status":
        r = rows[0]
        status = STATUS_TEXT.get(s.status, s.status)
        if s.employee_id:
            return f"{subject(s)} was {status} on {_days(r['employee_days'])} during {when}."
        if s.single_date:
            n = r["employee_days"]
            if not n:
                return f"No employees were {status} on {when}."
            noun = "employee was" if n == 1 else "employees were"
            return f"{num(n)} {noun} {status} on {when}."
        return (
            f"There were {num(r['employee_days'])} {s.status} employee-days "
            f"({num(r['employees'])} employees) during {when}."
        )
    if intent == "employee_status_on_date":
        r = rows[0]
        day = fmt_date(s.date_from, date_format)
        who = person(r["employee_id"], r["employee_name"])
        if r.get("conflict"):
            reported = join(list(r.get("reported_statuses") or []))
            return (
                f"The sources disagree for {who} on {day}: they report {reported}. "
                "This day needs review before it can be confirmed."
            )
        times = []
        if r.get("check_in"):
            times.append(f"check-in {r['check_in']}")
        if r.get("check_out"):
            times.append(f"check-out {r['check_out']}")
        extra = f" ({', '.join(times)})" if times else ""
        return f"{who} was {STATUS_TEXT.get(r['status'], r['status'])} on {day}{extra}."
    if intent == "hours":
        r = rows[0]
        who = subject(s) or "Employees in your permitted scope"
        if s.aggregate == "average":
            return (
                f"{who} averaged {num(r['avg_hours'])} hours per day during {when} "
                f"({_days(r['days_with_hours'])} with recorded hours)."
            )
        return (
            f"{who} recorded {num(r['total_hours'])} hours in total during {when} "
            f"across {_days(r['days_with_hours'])} with recorded hours."
        )
    return generic(rows)


def generic(rows: list[dict]) -> str:
    """Plain rendering of an LLM-SQL result when no template fits and phrasing failed."""
    if len(rows) == 1 and len(rows[0]) == 1:
        return f"The result is {_value(next(iter(rows[0].values())))}."
    shown = [", ".join(f"{k} {_value(v)}" for k, v in r.items()) for r in rows[:10]]
    more = f" (and {len(rows) - 10} more)" if len(rows) > 10 else ""
    return f"Result ({len(rows)} rows): " + "; ".join(shown) + more + "."


def _value(v) -> str:
    return num(v) if isinstance(v, int | float) and not isinstance(v, bool) else str(v)


def pending_only(s: Slots, pending: list[dict], date_format: str) -> str:
    files = sorted({p["source_file"] for p in pending})
    who = subject(s) or "this question"
    n = len(pending)
    noun = "record" if n == 1 else "records"
    verb = "awaits" if n == 1 else "await"
    return (
        f"The only evidence for {who} for {period_text(s, date_format)} is {n} {noun} from "
        f"{join(files)} that {verb} human review, so it cannot be confirmed yet."
    )
