"""Answer grounding: every date, time, employee id, number and person-like name in a
model-phrased answer must be supported by the result rows (or the question and
the resolved period). Anything else is treated as a hallucination and the answer
is re-rendered by the template engine.

Rounding is tolerated in one direction only: "82", "82.0" and "82%" match 82.0,
and 91.83 may be stated as 91.8 or 92, but no digit can be invented.
"""

import datetime as dt
import math
import re
from dataclasses import dataclass, field

from app.retrieval.rewrite import MONTHS, NOT_NAMES

_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_NUMERIC_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
_MONTH_RX = "|".join(sorted(MONTHS, key=len, reverse=True))
_WORD_DATE = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_RX})\.?(?:,?\s+(\d{{4}}))?\b"
    rf"|\b({_MONTH_RX})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?!\d)(?:,?\s+(\d{{4}}))?\b",
    re.I,
)
_TIME = re.compile(r"\b\d{1,2}:\d{2}\b")
_EMP_ID = re.compile(r"\b[A-Z]\d{3,}\b")
_NUMBER = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?![\w])")
_NAME = re.compile(r"\b([A-Z][a-z]+)\s+([A-Z][a-z]+)\b")
_COMMON = {
    "the", "sources", "source", "result", "results", "overall", "attendance", "engineering",
    "department", "employee", "employees", "total", "average", "records", "record", "day",
    "days", "there", "no", "none", "only", "both", "one", "two", "three", "four", "five",
}  # fmt: skip


@dataclass
class Grounding:
    ok: bool
    problems: list[str] = field(default_factory=list)


def _row_values(rows: list[dict]) -> list:
    out = []
    for r in rows:
        for v in r.values():
            out.extend(v if isinstance(v, list) else [v])
    return out


def _number_ok(a: float, allowed: list[float], decimals: int) -> bool:
    for b in allowed:
        if math.isclose(a, b, abs_tol=1e-9):
            return True
        if decimals <= 2 and math.isclose(round(b, decimals), a, abs_tol=1e-9):
            return True
    return False


def check(
    answer: str,
    rows: list[dict],
    *,
    question: str = "",
    period: tuple[dt.date, dt.date] | None = None,
    allowed_text: str = "",
    default_year: int | None = None,
) -> Grounding:
    values = _row_values(rows)
    strings = [str(v) for v in values if isinstance(v, str)]
    text_pool = " ".join([*strings, question, allowed_text])
    problems: list[str] = []

    allowed_dates: set[dt.date] = set()
    for v in [*strings, *(p.isoformat() for p in period or ())]:
        m = _ISO.match(v)
        if m:
            allowed_dates.add(dt.date(int(m[1]), int(m[2]), int(m[3])))
    year = default_year or (period[0].year if period else dt.date.today().year)
    rest = answer

    def dates(rx, build):
        nonlocal rest
        for m in rx.finditer(rest):
            try:
                d = build(m)
            except (ValueError, KeyError):
                d = None
            if d is not None and d not in allowed_dates and m.group(0) not in question:
                problems.append(f"date {m.group(0)!r} not in results")
        rest = rx.sub(" ", rest)

    dates(_ISO, lambda m: dt.date(int(m[1]), int(m[2]), int(m[3])))
    dates(_NUMERIC_DATE, lambda m: dt.date(int(m[3]), int(m[2]), int(m[1])))
    dates(
        _WORD_DATE,
        lambda m: (
            dt.date(int(m[3] or year), MONTHS[m[2].lower()], int(m[1]))
            if m[1]
            else dt.date(int(m[6] or year), MONTHS[m[4].lower()], int(m[5]))
        ),
    )
    for rx, kind in ((_TIME, "time"), (_EMP_ID, "employee id")):
        for m in rx.finditer(rest):
            if m.group(0) not in text_pool:
                problems.append(f"{kind} {m.group(0)!r} not in results")
        rest = rx.sub(" ", rest)

    allowed_numbers = [
        float(v) for v in values if isinstance(v, int | float) and not isinstance(v, bool)
    ]
    allowed_numbers += [float(len(rows))]
    allowed_numbers += [float(n) for n in _NUMBER.findall(f"{question} {allowed_text}")]
    for m in _NUMBER.finditer(rest):
        token = m.group(0)
        decimals = len(token.split(".")[1]) if "." in token else 0
        if not _number_ok(float(token), allowed_numbers, decimals):
            problems.append(f"number {token!r} not in results")

    vocab = {w.lower() for w in re.findall(r"[A-Za-z]+", text_pool)}
    for m in _NAME.finditer(answer):
        words = [m[1].lower(), m[2].lower()]
        if all(w not in vocab and w not in NOT_NAMES and w not in _COMMON for w in words):
            problems.append(f"name {m.group(0)!r} not in results")
    return Grounding(ok=not problems, problems=problems)
