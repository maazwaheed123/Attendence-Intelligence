"""Status vocabulary -> canonical status."""

import re

CANONICAL = ("present", "absent", "leave", "holiday", "wfh", "half_day")

_MAP = {
    "present": {"p", "present", "in", "yes", "y", "on duty", "attended", "✓", "✔", "x present"},
    "absent": {"a", "absent", "no", "n", "not present", "abs", "no show"},
    "leave": {
        "l",
        "leave",
        "on leave",
        "pl",
        "sl",
        "cl",
        "al",
        "sick leave",
        "annual leave",
        "casual leave",
        "vacation",
        "paid leave",
    },
    "holiday": {"h", "holiday", "ph", "public holiday", "bank holiday"},
    "wfh": {"wfh", "remote", "work from home", "working from home", "home", "telework"},
    "half_day": {"hd", "half", "half day", "halfday", "half day leave"},
}
UNCERTAIN_MARKS = ("?", "~~", "(smudged)", "[illegible]")
_LOOKUP = {syn: canon for canon, syns in _MAP.items() for syn in syns}


def _norm(value) -> str:
    s = str(value or "").strip().lower()
    s = s.replace("-", " ").replace("_", " ")
    s = re.sub(r"[^\w\s✓✔]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def map_status(value) -> tuple[str, bool]:
    """(canonical_status, recognized). Unrecognized -> ('unknown', False)."""
    if any(mark in str(value or "") for mark in UNCERTAIN_MARKS):
        return "unknown", False
    s = _norm(value)
    if s in _LOOKUP:
        return _LOOKUP[s], True
    return "unknown", False
