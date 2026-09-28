"""Turn a reviewer's ideal output into a reusable answer template.

facts()       the values an answer for this intent states, formatted as they are shown
templatize()  replace every fact value found in the ideal text with a {placeholder};
              anything number-like or id-like left over is NOT supported by the data
render()      fill a template with facts recomputed from live, RLS-scoped rows

Because values are always recomputed, an example can change the wording of an
answer but never its numbers, and never what data it is computed from.
"""

import re

from app.generation.answer_templates import STATUS_TEXT, join, num, period_text, person
from app.retrieval.rewrite import fmt_date, fmt_period
from app.retrieval.sql.templates import winners
from app.retrieval.types import Slots

FEEDBACK_INTENTS = (
    "attendance_pct",
    "rank",
    "count_by_status",
    "hours",
    "employee_status_on_date",
    "list_by_status",
)
_LEFTOVER_NUMBER = re.compile(r"(?<![\w{])\d+(?:[.,]\d+)?(?![\w}])")
_LEFTOVER_ID = re.compile(r"\b[A-Z]\d{3,}\b")
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def signature(intent: str, s: Slots) -> str:
    """What makes two questions 'the same question' for feedback (values excluded)."""
    subject = "employee" if s.employee_id else "entity" if s.entity_id else "all"
    parts = [intent, subject, s.group_by or "-", s.rank_direction or "-", s.status or "-"]
    if intent == "hours":
        parts.append(s.aggregate or "total")
    return "|".join(parts)


def facts(intent: str, s: Slots, rows: list[dict], date_format: str) -> dict[str, str]:
    f = {
        "date_from": fmt_date(s.date_from, date_format),
        "date_to": fmt_date(s.date_to, date_format),
        "period": fmt_period(s.date_from, s.date_to, date_format),
        "period_text": period_text(s, date_format),
    }
    if s.period_label:
        f["period_label"] = s.period_label
    if s.employee_id:
        f.update(employee=person(s.employee_id, s.employee_name), employee_id=s.employee_id)
        if s.employee_name:
            f["employee_name"] = s.employee_name
    if s.entity_id:
        f["entity"] = s.entity_name or s.entity_id
    f["subject"] = f.get("employee") or f.get("entity") or "Overall"
    r = rows[0] if rows else {}
    if intent == "attendance_pct":
        f.update(
            attendance_pct=num(r["attendance_pct"]),
            present_days=num(r["present_days"]),
            scheduled_days=num(r["scheduled_days"]),
        )
    elif intent == "rank":
        top = winners(rows)
        dept = s.group_by == "department"
        names = [
            (w.get("department") or w["entity_id"])
            if dept
            else person(w["employee_id"], w["employee_name"])
            for w in top
        ]
        f.update(
            winner=join(names),
            attendance_pct=num(r["attendance_pct"]),
            present_days=num(r["present_days"]),
            scheduled_days=num(r["scheduled_days"]),
            direction=s.rank_direction or "highest",
        )
    elif intent == "count_by_status":
        f.update(
            count=num(r["employee_days"]),
            employees=num(r["employees"]),
            status=STATUS_TEXT.get(s.status, s.status),
        )
    elif intent == "hours":
        f.update(
            total_hours=num(r["total_hours"]),
            avg_hours=num(r["avg_hours"]),
            days=num(r["days_with_hours"]),
        )
    elif intent == "employee_status_on_date":
        f.update(
            status=STATUS_TEXT.get(r["status"], r["status"]),
            date=fmt_date(s.date_from, date_format),
        )
        if r.get("check_in"):
            f["check_in"] = r["check_in"]
        if r.get("check_out"):
            f["check_out"] = r["check_out"]
    elif intent == "list_by_status":
        f.update(
            count=str(len(rows)),
            names=join([person(x["employee_id"], x["employee_name"]) for x in rows])
            if rows
            else "",
            status=STATUS_TEXT.get(s.status, s.status),
        )
    return {k: str(v) for k, v in f.items() if v not in (None, "")}


def _pattern(value: str) -> re.Pattern:
    if re.fullmatch(r"[\d.,/:-]+", value):  # numbers, dates, times: never inside a longer number
        return re.compile(rf"(?<![\w.]){re.escape(value)}(?![\w]|\.\d)")
    return re.compile(rf"(?<!\w){re.escape(value)}(?!\w)")


def templatize(ideal: str, f: dict[str, str]) -> tuple[str, list[str]]:
    """(template, unsupported values). Longest values first, so a date is replaced
    before its day number could be."""
    template = ideal.replace("{", "{{").replace("}", "}}")
    for key, value in sorted(f.items(), key=lambda kv: -len(kv[1])):
        template = _pattern(value).sub("{" + key + "}", template)
    stripped = _PLACEHOLDER.sub(" ", template)
    unsupported = _LEFTOVER_NUMBER.findall(stripped) + _LEFTOVER_ID.findall(stripped)
    return template, unsupported


def render(template: str, f: dict[str, str]) -> str | None:
    try:
        return template.format(**f)
    except (KeyError, IndexError, ValueError):
        return None  # the live answer lacks a fact the template needs


def placeholders(template: str) -> list[str]:
    return _PLACEHOLDER.findall(template.replace("{{", "").replace("}}", ""))


def normalize(text: str) -> str:
    return " ".join(text.split())
