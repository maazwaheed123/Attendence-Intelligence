"""Query rewrite: resolve dates, periods, employees and departments to concrete values.

Names are resolved ONLY against the caller's RLS-scoped directory (employees and
entities as rag_reader). A name from another tenant, entity or product therefore
resolves to nothing, exactly like a name that does not exist, and both produce
the same "no data in your permitted scope" answer (non-leaking denial).
"""

import calendar
import datetime as dt
import re
from dataclasses import dataclass, field

from sqlalchemy import text

from app.db.session import scoped_session
from app.retrieval.types import Slots
from app.security.scope import DbScope

MONTHS = {
    **{m.lower(): i for i, m in enumerate(calendar.month_name) if m},
    **{m.lower(): i for i, m in enumerate(calendar.month_abbr) if m},
    "sept": 9,
}
_MONTH_RX = "|".join(sorted(MONTHS, key=len, reverse=True))
_ORD = r"(?:st|nd|rd|th)?"
_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "last": -1}

_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_NUMERIC = re.compile(r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{4})\b")
_DAY_MONTH = re.compile(
    rf"(?<!week )(?<!week)\b(\d{{1,2}}){_ORD}\s+(?:of\s+)?({_MONTH_RX})\.?(?:,?\s+(\d{{4}}))?\b",
    re.I,
)
_MONTH_DAY = re.compile(rf"\b({_MONTH_RX})\.?\s+(\d{{1,2}}){_ORD}(?!\d)(?:,?\s+(\d{{4}}))?\b", re.I)
_MONTH_YEAR = re.compile(rf"\b({_MONTH_RX})\.?(?:\s+(\d{{4}}))?\b", re.I)
_WEEK_N = re.compile(r"\bweek\s*(\d)\b", re.I)
_NTH_WEEK = re.compile(r"\b(first|second|third|fourth|fifth|last)\s+week\b", re.I)
_RELATIVE_MONTH = re.compile(r"\b(last|previous|this|current)\s+month\b", re.I)
_RELATIVE_WEEK = re.compile(r"\b(last|previous|this|current)\s+week\b", re.I)
_RELATIVE_DAY = re.compile(r"\b(today|yesterday)\b", re.I)

NOT_NAMES = {
    *(m for m in MONTHS),
    *(d.lower() for d in calendar.day_name),
    *(d.lower() for d in calendar.day_abbr),
    *"""who what which when where why how was were is are am did does do show list give
    tell me the a an in on for of and or to at by average avg mean total count please
    summarise summarize ignore run compare find get name names employee employees staff
    department departments team teams attendance present presence absent absence absences
    leave holiday holidays week weeks month months year last this next today yesterday
    overall all across tenant tenants company highest lowest most least best worst top
    between from during hours hour rate percentage percent evidence proof source note notes
    manager managers remark remarks memo letter explain any anyone everyone everybody i my
    our we you your can could would will should has have had much many number days day
    work worked working home remote half sick public drop table select delete insert update
    answer question data record records report reports status late arrivals arrival check
    first second third fourth fifth per each every whole entire sum rank ranking ranked
    whose whom with without than more less over under also only just then there here that
    these those it its be been being not no yes ok okay hi hello thanks thank sep sept
    """.split(),  # noqa: SIM905 - a word list reads better as text
}
_DEPARTMENT_WORDS = {
    "engineering",
    "hr",
    "human resources",
    "finance",
    "sales",
    "marketing",
    "operations",
    "legal",
    "support",
    "accounting",
    "accounts",
    "research",
    "design",
    "product",
    "procurement",
    "logistics",
}
_CAP_WORD = re.compile(r"\b[A-Z][a-z]+(?:-[A-Z][a-z]+)?\b")


def today() -> dt.date:
    return dt.date.today()


@dataclass
class Directory:
    """What the caller may see, loaded through RLS (rag_reader)."""

    employees: list[tuple[str, str, str]] = field(default_factory=list)
    entities: list[tuple[str, str]] = field(default_factory=list)
    coverage: tuple[dt.date | None, dt.date | None] = (None, None)
    date_format: str = "DD/MM/YYYY"

    def entity_name(self, entity_id: str | None) -> str | None:
        return next((n for e, n in self.entities if e == entity_id), None)

    def employee(self, employee_id: str) -> tuple[str, str, str] | None:
        return next((e for e in self.employees if e[0] == employee_id), None)


def load_directory(scope: DbScope) -> Directory:
    with scoped_session(scope, role="reader") as s:
        employees = s.execute(
            text(
                "SELECT DISTINCT employee_id, employee_name, entity_id FROM employees "
                "ORDER BY employee_id"
            )
        ).all()
        entities = s.execute(text("SELECT entity_id, name FROM entities ORDER BY 1")).all()
        coverage = s.execute(
            text("SELECT min(attendance_date), max(attendance_date) FROM v_attendance")
        ).one()
        fmt = s.execute(
            text("SELECT date_format FROM tenants WHERE tenant_id = :t"), {"t": scope.tenant_id}
        ).scalar()
    return Directory(
        employees=[tuple(r) for r in employees],
        entities=[tuple(r) for r in entities],
        coverage=(coverage[0], coverage[1]),
        date_format=fmt or "DD/MM/YYYY",
    )


def fmt_date(d: dt.date, date_format: str = "DD/MM/YYYY") -> str:
    return d.strftime("%m/%d/%Y" if date_format.upper().startswith("MM") else "%d/%m/%Y")


def fmt_period(d1: dt.date, d2: dt.date, date_format: str = "DD/MM/YYYY") -> str:
    if d1 == d2:
        return fmt_date(d1, date_format)
    return f"{fmt_date(d1, date_format)} - {fmt_date(d2, date_format)}"


def month_range(year: int, month: int) -> tuple[dt.date, dt.date]:
    return dt.date(year, month, 1), dt.date(year, month, calendar.monthrange(year, month)[1])


def month_weeks(year: int, month: int) -> list[tuple[dt.date, dt.date]]:
    """Working weeks of a month: weekdays grouped by ISO week (week 1 may be short)."""
    first, last = month_range(year, month)
    weeks: dict[int, list[dt.date]] = {}
    day = first
    while day <= last:
        if day.weekday() < 5:
            weeks.setdefault(day.isocalendar()[1], []).append(day)
        day += dt.timedelta(days=1)
    return [(days[0], days[-1]) for _, days in sorted(weeks.items(), key=lambda kv: kv[1][0])]


def _safe_date(y: int, m: int, d: int) -> dt.date | None:
    try:
        return dt.date(y, m, d)
    except ValueError:
        return None


def _day_dates(q: str, date_format: str, default_year: int) -> tuple[list[dt.date], str]:
    """Explicit calendar dates in the question, and the text with them removed."""
    found: list[tuple[int, dt.date]] = []

    def take(rx, build):
        nonlocal q
        for m in rx.finditer(q):
            d = build(m)
            if d:
                found.append((m.start(), d))
        q = rx.sub(" ", q)

    take(_ISO, lambda m: _safe_date(int(m[1]), int(m[2]), int(m[3])))
    mm_first = date_format.upper().startswith("MM")
    take(
        _NUMERIC,
        lambda m: _safe_date(
            int(m[3]), int(m[1] if mm_first else m[2]), int(m[2] if mm_first else m[1])
        ),
    )
    take(
        _DAY_MONTH,
        lambda m: _safe_date(int(m[3] or default_year), MONTHS[m[2].lower()], int(m[1])),
    )
    take(
        _MONTH_DAY,
        lambda m: _safe_date(int(m[3] or default_year), MONTHS[m[1].lower()], int(m[2])),
    )
    return [d for _, d in sorted(found, key=lambda x: x[0])], q


def extract_period(
    question: str,
    *,
    date_format: str = "DD/MM/YYYY",
    latest: dt.date | None = None,
) -> tuple[dt.date, dt.date, str] | None:
    """(date_from, date_to, label) or None when the question names no period.

    Missing years default to the year of the latest data in scope (else today);
    "week N" without a month refers to the month of the latest data in scope.
    """
    ref = latest or today()
    dates, rest = _day_dates(question, date_format, ref.year)
    if dates:
        d1, d2 = min(dates), max(dates)
        return d1, d2, fmt_period(d1, d2, date_format)

    now = today()
    rel = _RELATIVE_DAY.search(rest)
    if rel:
        d = now if rel[1].lower() == "today" else now - dt.timedelta(days=1)
        return d, d, rel[1].lower()
    rel = _RELATIVE_MONTH.search(rest)
    if rel:
        anchor = now.replace(day=1)
        if rel[1].lower() in ("last", "previous"):
            anchor = (anchor - dt.timedelta(days=1)).replace(day=1)
        d1, d2 = month_range(anchor.year, anchor.month)
        return d1, d2, f"{calendar.month_name[anchor.month]} {anchor.year}"
    rel = _RELATIVE_WEEK.search(rest)
    if rel:
        monday = now - dt.timedelta(days=now.weekday())
        if rel[1].lower() in ("last", "previous"):
            monday -= dt.timedelta(days=7)
        return monday, monday + dt.timedelta(days=4), f"{rel[1].lower()} week"

    month_m = next((m for m in _MONTH_YEAR.finditer(rest) if m[1] != "may"), None)
    year, month = ref.year, ref.month
    if month_m:
        month = MONTHS[month_m[1].lower()]
        year = int(month_m[2]) if month_m[2] else ref.year

    week_m = _WEEK_N.search(rest) or _NTH_WEEK.search(rest)
    if week_m:
        token = week_m[1].lower()
        n = int(token) if token.isdigit() else _ORDINALS[token]
        weeks = month_weeks(year, month)
        label = f"week {n}" if n > 0 else "last week of the month"
        if not weeks or n == 0 or n > len(weeks):
            d1 = dt.date(year, month, 1)
            return d1, d1 - dt.timedelta(days=1), label
        d1, d2 = weeks[n - 1] if n > 0 else weeks[-1]
        return d1, d2, label

    if month_m:
        d1, d2 = month_range(year, month)
        return d1, d2, f"{calendar.month_name[month]} {year}"
    return None


def _word(term: str, flags=re.I) -> re.Pattern:
    return re.compile(rf"(?<![\w-]){re.escape(term)}(?:'s|’s)?(?![\w-])", flags)


def resolve_entity(question: str, directory: Directory) -> tuple[str | None, str | None, bool]:
    """(entity_id, entity_name, unresolved). Only in-scope entities can resolve."""
    for entity_id, name in directory.entities:
        terms = {entity_id.replace("_", " "), name}
        if entity_id == "hr" or name.lower() == "human resources":
            terms |= {"hr", "human resources"}
        if any(_word(t).search(question) for t in terms):
            return entity_id, name, False
    mentioned = any(_word(w).search(question) for w in _DEPARTMENT_WORDS)
    return None, None, mentioned


def resolve_people(question: str, directory: Directory) -> tuple[list[tuple[str, str]], bool]:
    """([(employee_id, name), ...] in order of mention, unresolved_person_mentioned)."""
    hits: list[tuple[int, str, str]] = []
    remaining = question
    by_first: dict[str, list] = {}
    by_last: dict[str, list] = {}
    for emp_id, name, _ in directory.employees:
        parts = name.split()
        by_first.setdefault(parts[0], []).append((emp_id, name))
        by_last.setdefault(parts[-1], []).append((emp_id, name))

    def hit(rx: re.Pattern, emp_id: str, name: str) -> None:
        nonlocal remaining
        m = rx.search(remaining)
        if m:
            hits.append((m.start(), emp_id, name))
            remaining = rx.sub(" ", remaining)

    for emp_id, name, _ in directory.employees:
        hit(_word(name), emp_id, name)
        hit(_word(emp_id), emp_id, name)
    for index in (by_first, by_last):
        for token, emps in index.items():
            for emp_id, name in emps:
                if not any(h[1] == emp_id for h in hits):
                    rx = _word(token, 0)
                    if rx.search(remaining):
                        hits.append((rx.search(remaining).start(), emp_id, name))
            remaining = _word(token, 0).sub(" ", remaining)

    entity_words = {w for _, n in directory.entities for w in n.lower().split()}
    entity_words |= {w for d in _DEPARTMENT_WORDS for w in d.split()}
    unresolved = False
    for m in _CAP_WORD.finditer(remaining):
        word = m.group(0).lower()
        if word not in NOT_NAMES and word not in entity_words:
            unresolved = True
            break
    if not unresolved and re.search(r"\bE\d{3,}\b", remaining, re.I):
        unresolved = True
    seen: dict[str, str] = {}
    for _, emp_id, name in sorted(hits):
        seen.setdefault(emp_id, name)
    return list(seen.items()), unresolved


def apply_names(question: str, directory: Directory, slots: Slots) -> Slots:
    people, unresolved = resolve_people(question, directory)
    if people:
        slots.employee_id, slots.employee_name = people[0]
        slots.extra_employees = [p[0] for p in people[1:]]
    slots.unresolved_person = unresolved and not people
    entity_id, entity_name, unresolved_entity = resolve_entity(question, directory)
    slots.entity_id, slots.entity_name = entity_id, entity_name
    slots.unresolved_entity = unresolved_entity
    return slots
