"""RawRecord -> canonical attendance draft, with honest confidence and review flags.

Rows that cannot become a trustworthy fact are either
  - FAILED (not stored): unknown/out-of-scope employee, missing/invalid date,
    duplicate row within the file; or
  - stored with review_required=true: unknown status, low extraction confidence,
    name/ID disagreement, unparseable time.
"""

from dataclasses import dataclass

from rapidfuzz import fuzz, process
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.ingestion.normalize.datetimes import parse_date, parse_time, total_hours
from app.ingestion.normalize.status import map_status
from app.ingestion.types import ParseResult, RowFailure


@dataclass(frozen=True)
class Employee:
    employee_id: str
    employee_name: str
    entity_id: str
    department: str


class Roster:
    """Employees visible in the uploader's scope (RLS applies to the lookup)."""

    def __init__(self, employees: list[Employee]):
        self.by_id = {e.employee_id.upper(): e for e in employees}
        self.names = {e.employee_name: e for e in employees}

    @classmethod
    def load(cls, session: Session) -> "Roster":
        rows = session.execute(
            text(
                "SELECT e.employee_id, e.employee_name, e.entity_id, en.name "
                "FROM employees e JOIN entities en "
                "  ON en.tenant_id = e.tenant_id AND en.entity_id = e.entity_id"
            )
        ).all()
        return cls([Employee(*r) for r in rows])

    def resolve(self, emp_id, name) -> tuple[Employee | None, float, list[str]]:
        """Returns (employee, confidence, review_reasons)."""
        emp_id = str(emp_id or "").strip().upper()
        name = str(name or "").strip()
        if emp_id and emp_id in self.by_id:
            e = self.by_id[emp_id]
            if name and fuzz.token_sort_ratio(name, e.employee_name) < 80:
                return e, 0.6, [f"name '{name}' does not match roster name for {e.employee_id}"]
            return e, 1.0, []
        if name and self.names:
            match = process.extractOne(name, list(self.names), scorer=fuzz.token_sort_ratio)
            if match and match[1] >= 90:
                e = self.names[match[0]]
                conf = match[1] / 100
                reasons = [] if match[1] >= 97 else [f"name matched to roster at {match[1]:.0f}%"]
                if emp_id:
                    reasons.append(f"employee id '{emp_id}' not in roster; matched by name")
                    conf = min(conf, 0.7)
                return e, conf, reasons
        return None, 0.0, []


def normalize(
    parsed: ParseResult,
    roster: Roster,
    *,
    date_format: str,
    review_threshold: float,
) -> tuple[list[dict], list[RowFailure]]:
    drafts, failures, seen = [], [], {}
    for rec in parsed.records:
        v = rec.values
        emp, id_conf, reasons = roster.resolve(v.get("employee_id"), v.get("employee_name"))
        if emp is None:
            who = v.get("employee_id") or v.get("employee_name") or "(blank)"
            failures.append(
                RowFailure(rec.locator, f"employee '{who}' not found in your permitted scope")
            )
            continue
        d, problem = parse_date(v.get("attendance_date"), date_format)
        if d is None:
            failures.append(RowFailure(rec.locator, problem or "invalid date"))
            continue
        key = (emp.employee_id, d)
        if key in seen:
            failures.append(
                RowFailure(rec.locator, f"duplicate of {seen[key]} (same employee and date)")
            )
            continue
        seen[key] = rec.locator

        reasons = list(reasons) + list(rec.notes)
        status, known = map_status(v.get("status"))
        status_conf = 1.0 if known else 0.3
        if not known:
            reasons.append(f"unrecognized status '{v.get('status')}'")
        cin, p1 = parse_time(v.get("check_in"))
        cout, p2 = parse_time(v.get("check_out"))
        time_conf = 1.0
        for p in (p1, p2):
            if p:
                reasons.append(p)
                time_conf = 0.7

        confidence = round(min(rec.confidence, id_conf, status_conf, time_conf), 3)
        # "info:" notes (e.g. an unreadable time that was dropped) are kept on the
        # record but do not cast doubt on the attendance fact itself.
        doubts = [r for r in reasons if not r.startswith("info:")]
        review = confidence < review_threshold or bool(doubts and confidence < 0.9)
        drafts.append(
            {
                "employee_id": emp.employee_id,
                "employee_name": emp.employee_name,
                "entity_id": emp.entity_id,
                "department": emp.department,
                "attendance_date": d,
                "status": status,
                "check_in": cin,
                "check_out": cout,
                "total_hours": total_hours(cin, cout),
                "source_locator": rec.locator,
                "raw_values": rec.raw,
                "extraction_confidence": confidence,
                "review_required": review,
                "review_reasons": reasons,
            }
        )
    return drafts, failures
