"""Header synonyms -> canonical fields, and header-row detection.

Restricted columns (phone, national ID, email, ...) are recognized so they can
be DROPPED at ingestion: attendance answers never need them.
"""

import re
from typing import Any

SYNONYMS: dict[str, set[str]] = {
    "employee_id": {
        "employee id",
        "emp id",
        "emp code",
        "employee code",
        "staff id",
        "employee no",
        "emp no",
        "id",
        "badge id",
        "employee number",
    },
    "employee_name": {"employee name", "name", "full name", "employee", "staff name", "staff"},
    "department": {"department", "dept", "team", "division", "unit"},
    "attendance_date": {"date", "day", "attendance date", "work date"},
    "status": {"status", "attendance", "presence", "att", "attendance status"},
    "check_in": {"check in", "in", "in time", "clock in", "time in", "login", "arrival"},
    "check_out": {"check out", "out", "out time", "clock out", "time out", "logout", "departure"},
}
RESTRICTED = {
    "phone",
    "mobile",
    "phone number",
    "mobile number",
    "national id",
    "nid",
    "ssn",
    "passport",
    "passport no",
    "email",
    "e mail",
    "email address",
    "address",
    "iqama",
    "aadhaar",
}
REQUIRED_ANY_IDENTITY = ("employee_id", "employee_name")
REQUIRED = ("attendance_date", "status")

_LOOKUP = {syn: canon for canon, syns in SYNONYMS.items() for syn in syns}


def norm_header(h: Any) -> str:
    s = str(h or "").strip().lower()
    s = re.sub(r"[_\-/.]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def map_header(h: Any) -> str | None:
    """Canonical field name, 'restricted', or None."""
    n = norm_header(h)
    if not n:
        return None
    if n in RESTRICTED:
        return "restricted"
    return _LOOKUP.get(n)


def header_mapping(row: list[Any]) -> tuple[dict[int, str], list[str]] | None:
    """If `row` looks like a header, return ({col_index: canonical}, restricted_names)."""
    mapping, restricted = {}, []
    for i, cell in enumerate(row):
        m = map_header(cell)
        if m == "restricted":
            restricted.append(str(cell).strip())
        elif m and m not in mapping.values():
            mapping[i] = m
    fields = set(mapping.values())
    if all(f in fields for f in REQUIRED) and any(f in fields for f in REQUIRED_ANY_IDENTITY):
        return mapping, restricted
    return None


def find_header(rows: list[list[Any]], max_scan: int = 15):
    """Index of the first header-looking row within the first `max_scan` rows."""
    for idx, row in enumerate(rows[:max_scan]):
        found = header_mapping(row)
        if found:
            return idx, found[0], found[1]
    return None
