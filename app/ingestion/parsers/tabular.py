"""Shared logic for any row-based source (CSV, XLSX sheets, DOCX/PDF long tables)."""

from collections.abc import Callable
from typing import Any

from app.ingestion.normalize.mapping import find_header
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


def _jsonable(v: Any) -> Any:
    if v is None or isinstance(v, str | int | float | bool):
        return v
    return str(v)
