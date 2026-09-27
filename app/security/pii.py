"""PII masking for free text (narrative chunks, answers, exports).

Masks phone numbers, e-mail addresses and national-ID-like values. Tuned not to
touch attendance data: dates (01/09/2026, 2026-09-01), times (09:05) and employee
IDs (E001) never match.
"""

import re

_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9_.-]+\.[A-Za-z]{2,}\b")),
    ("NATIONAL_ID", re.compile(r"\bNID-\d{6,12}\b", re.I)),
    ("NATIONAL_ID", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),  # SSN-style
    ("NATIONAL_ID", re.compile(r"\b\d{4}\s\d{4}\s\d{4}\b")),  # Aadhaar-style
    # Phone: optional +, then >= 8 digits separated by spaces, dashes, dots or parentheses.
    ("PHONE", re.compile(r"(?<![\w/])\+?\(?\d[\d\s().-]{7,}\d(?![\w/])")),
]


def _digits(s: str) -> int:
    return sum(c.isdigit() for c in s)


def mask_text(text: str) -> tuple[str, dict[str, int]]:
    """Returns (masked_text, {kind: count})."""
    counts: dict[str, int] = {}

    def sub(kind):
        def _r(m: re.Match) -> str:
            value = m.group(0)
            if kind == "PHONE" and (
                _digits(value) < 8 or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip())
            ):
                return value
            counts[kind] = counts.get(kind, 0) + 1
            return f"[{kind}]"

        return _r

    for kind, pattern in _PATTERNS:
        text = pattern.sub(sub(kind), text)
    return text, counts


def contains_pii(text: str) -> bool:
    return bool(mask_text(text)[1])
