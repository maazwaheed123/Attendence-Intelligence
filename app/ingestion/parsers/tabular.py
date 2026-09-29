"""Shared logic for any row-based source (CSV, XLSX sheets, DOCX/PDF long tables)."""

import re
from collections.abc import Callable
from typing import Any

from app.ingestion.normalize.mapping import find_header, map_header
from app.ingestion.types import ParseResult, RawRecord

SKIP_PREFIXES = ("total", "grand total", "sum", "subtotal", "signature", "approved by")


def _blank(row) -> bool:
    return all(c is None or str(c).strip() == "" for c in row)


def extract_rows(
    rows: list[list[Any]],
    result: ParseResult,
    locator: Callable[[int], str],
    *,
    source_label: str,
) -> bool:
    """Find the header, then turn each data row into a RawRecord.

    `locator(i)` builds the locator for 0-based row index i. Returns False when no
    header row could be found (caller decides whether that is fatal).
    """
    found = find_header(rows)
    if not found:
        return False
    header_idx, mapping, restricted = found
    headers = [str(c).strip() if c is not None else "" for c in rows[header_idx]]
    if restricted:
        result.restricted_columns.extend(
            r for r in restricted if r not in result.restricted_columns
        )
    restricted_idx = {i for i, h in enumerate(headers) if h in restricted}

    for i in range(header_idx + 1, len(rows)):
        row = rows[i]
        if _blank(row):
            continue
        first = str(next((c for c in row if c not in (None, "")), "")).strip().lower()
        if first.startswith(SKIP_PREFIXES):
            result.skipped_rows.append({"locator": locator(i), "reason": "summary/footer row"})
            continue
        values = {canon: row[idx] if idx < len(row) else None for idx, canon in mapping.items()}
        raw = {
            headers[j] or f"col{j + 1}": _jsonable(row[j])
            for j in range(min(len(headers), len(row)))
            if j not in restricted_idx
        }
        result.records.append(RawRecord(locator=locator(i), values=values, raw=raw))
    if not result.records:
        result.warnings.append(f"{source_label}: header found but no data rows")
    return True


_DAY_COL = re.compile(r"^(?:[A-Za-z]{3,9}\.?\s+)?(\d{1,2})[/.\-](\d{1,2})(?:[/.\-](\d{2,4}))?$")
_YEAR = re.compile(r"\b(20\d{2})\b")


def context_year(text: str) -> int | None:
    m = _YEAR.search(text or "")
    return int(m.group(1)) if m else None


def extract_wide(
    rows: list[list[Any]],
    result: ParseResult,
    locator: Callable[[int, str], str],
    *,
    year: int | None,
    source_label: str,
) -> bool:
    """Wide layout: one row per employee, one column per day, status codes in cells.

    Each (employee, day) cell becomes its own RawRecord; the locator names the column.
    Day/month order is left to the normalizer (tenant date format).
    """
    for h_idx, header in enumerate(rows[:5]):  # noqa: B007 - h_idx used after the loop
        cells = [str(c or "").strip() for c in header]
        identity = {i: map_header(c) for i, c in enumerate(cells)}
        identity = {i: m for i, m in identity.items() if m in ("employee_id", "employee_name")}
        days = {}
        for i, c in enumerate(cells):
            if m := _DAY_COL.match(c):
                y = m.group(3) or (str(year) if year else None)
                if y:
                    y = f"20{y}" if len(y) == 2 else y
                    days[i] = f"{m.group(1)}/{m.group(2)}/{y}"
        if identity and len(days) >= 2:
            break
    else:
        return False

    for i in range(h_idx + 1, len(rows)):
        row = rows[i]
        if _blank(row):
            continue
        ident = {canon: row[idx] for idx, canon in identity.items() if idx < len(row)}
        for col, day in days.items():
            if col >= len(row) or str(row[col] or "").strip() == "":
                continue
            values = {**ident, "attendance_date": day, "status": row[col]}
            raw = {cells[idx]: _jsonable(row[idx]) for idx in identity}
            raw[cells[col]] = _jsonable(row[col])
            result.records.append(RawRecord(locator=locator(i, cells[col]), values=values, raw=raw))
    if not result.records:
        result.warnings.append(f"{source_label}: wide table found but no data cells")
    return True


def _jsonable(v: Any) -> Any:
    if v is None or isinstance(v, str | int | float | bool):
        return v
    return str(v)
