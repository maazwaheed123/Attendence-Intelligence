"""One export dataset, three renderers.

Every renderer (JSON, XLSX, PDF) receives the SAME ExportDataset, so the three
files always contain exactly the same rows in the same order. All reads run as
rag_reader inside the caller's scope: RLS decides which rows exist before the
export code sees anything, exactly as for /v1/query.

Two sources:
  records - resolved employee-days from v_attendance (the metric view) with filters.
  query   - the evidence behind a stored /v1/query answer: its citations,
            re-read in the caller's scope (a stored excerpt is never trusted).
"""

import datetime as dt
import hashlib
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import text

from app.db.session import scoped_session
from app.security import pii
from app.security.context import SecurityContext
from app.security.scope import CLEARANCE_RANK

MAX_ROWS = 5000
STATUSES = ("present", "absent", "leave", "holiday", "wfh", "half_day", "unknown", "conflict")

COLUMNS = (
    "row_type",
    "record_id",
    "chunk_id",
    "attendance_date",
    "employee_id",
    "employee_name",
    "department",
    "status",
    "check_in",
    "check_out",
    "total_hours",
    "source_file",
    "source_locator",
    "extraction_confidence",
    "conflict",
    "excerpt",
)
_TEXT_FIELDS = ("employee_name", "department", "source_locator", "excerpt")


class ExportNotFound(Exception):
    """The source request does not exist in the caller's scope."""


@dataclass
class ExportDataset:
    metadata: dict
    rows: list[dict] = field(default_factory=list)

    @property
    def row_ids(self) -> list[str]:
        return [row_key(r) for r in self.rows]


def row_key(r: dict) -> str:
    return r["record_id"] or r["chunk_id"]


def checksum(ids: list[str]) -> str:
    return hashlib.sha256("\n".join(ids).encode()).hexdigest()


def _plain(v):
    """DB values -> JSON-safe scalars; the renderers all use these same values."""
    if isinstance(v, dt.datetime | dt.date | dt.time):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    return v if v is None or isinstance(v, bool | int | float | str) else str(v)


def _row(r: dict, row_type: str, keep_tail: bool) -> dict:
    out = {c: _plain(r.get(c)) for c in COLUMNS}
    out["row_type"] = row_type
    out["conflict"] = bool(r.get("conflict"))
    for f in _TEXT_FIELDS:
        if isinstance(out[f], str):
            out[f] = pii.mask_text(out[f], keep_tail=keep_tail)[0]
    return out


_VIEW_COLS = (
    "record_id::text AS record_id, attendance_date, employee_id, employee_name, department, "
    "status, check_in, check_out, total_hours, source_file, source_locator, "
    "extraction_confidence, conflict, classification"
)


def _records(s, filters: dict) -> tuple[list[dict], dict]:
    where, params = ["true"], {}
    for key, clause in (
        ("date_from", "attendance_date >= :date_from"),
        ("date_to", "attendance_date <= :date_to"),
        ("entity_id", "entity_id = :entity_id"),
        ("employee_id", "employee_id = :employee_id"),
        ("status", "status = :status"),
    ):
        if filters.get(key) is not None:
            where.append(clause)
            params[key] = filters[key]
    cond = " AND ".join(where)
    rows = s.execute(
        text(
            f"SELECT {_VIEW_COLS} FROM v_attendance WHERE {cond} "  # noqa: S608 - fixed clauses
            f"ORDER BY attendance_date, employee_id, record_id LIMIT {MAX_ROWS + 1}"
        ),
        params,
    ).mappings()
    rows = [dict(r) for r in rows]
    lo, hi = s.execute(
        text("SELECT min(attendance_date), max(attendance_date) FROM v_attendance")
    ).one()
    period = {
        "date_from": _plain(filters.get("date_from") or lo),
        "date_to": _plain(filters.get("date_to") or hi),
    }
    return rows, period


def _query_rows(s, request_id: str) -> tuple[list[dict], dict]:
    stored = s.execute(
        text("SELECT response FROM query_responses WHERE request_id = :r"), {"r": request_id}
    ).scalar()
    if stored is None:
        raise ExportNotFound(request_id)
    citations = stored.get("citations") or []
    chunk_ids = [c["chunk_id"] for c in citations if c.get("chunk_id")]
    chunks = {
        r["chunk_id"]: r
        for r in s.execute(
            text(
                "SELECT chunk_id::text AS chunk_id, record_id::text AS record_id, text_masked, "
                "locator, classification FROM document_chunks "
                "WHERE chunk_id = ANY(CAST(:ids AS uuid[]))"
            ),
            {"ids": chunk_ids},
        ).mappings()
    }
    record_ids = [c["record_id"] for c in citations if c.get("record_id")]
    record_ids += [c["record_id"] for c in chunks.values() if c["record_id"]]
    records = {
        r["record_id"]: dict(r)
        for r in s.execute(
            text(
                "SELECT r.record_id::text AS record_id, r.attendance_date, r.employee_id, "
                "r.employee_name, r.department, r.status, r.check_in, r.check_out, "
                "r.total_hours, r.source_file, r.source_locator, r.extraction_confidence, "
                "r.classification, EXISTS (SELECT 1 FROM v_attendance v WHERE v.conflict "
                "AND r.record_id = ANY(v.source_record_ids)) AS conflict "
                "FROM attendance_records r WHERE r.record_id = ANY(CAST(:ids AS uuid[]))"
            ),
            {"ids": record_ids},
        ).mappings()
    }
    rows, seen = [], set()
    for c in citations:
        chunk = chunks.get(c.get("chunk_id") or "")
        rid = c.get("record_id") or (chunk or {}).get("record_id")
        if rid and rid in records:
            row = {**records[rid], "chunk_id": c.get("chunk_id")}
            if chunk:
                row["excerpt"] = chunk["text_masked"]
        elif chunk:
            row = {
                "record_id": None,
                "chunk_id": chunk["chunk_id"],
                "source_file": c["source_file"],
                "source_locator": chunk["locator"],
                "excerpt": chunk["text_masked"],
                "classification": chunk["classification"],
            }
        else:
            continue
        key = row["record_id"] or row["chunk_id"]
        if key not in seen:
            seen.add(key)
            rows.append(row)
    source = {
        "request_id": request_id,
        "status": stored.get("status"),
        "citation_total": stored.get("citation_total", len(citations)),
    }
    return rows, source


def build(ctx: SecurityContext, request_id: str, body: dict) -> ExportDataset:
    source = body["source"]
    filters = body.get("filters") or {}
    keep_tail = ctx.clearance == "restricted"
    with scoped_session(ctx.to_scope(), role="reader") as s:
        if source == "query":
            raw, extra = _query_rows(s, body["request_id"])
            meta_source = {"type": "query", **extra}
        else:
            raw, period = _records(s, filters)
            meta_source = {"type": "records", "filters": {k: _plain(v) for k, v in filters.items()}}
            meta_source["period"] = period
    truncated = len(raw) > MAX_ROWS
    raw = raw[:MAX_ROWS]
    kinds = [r.get("classification") or "internal" for r in raw] or ["internal"]
    rows = [_row(r, "record" if r.get("record_id") else "document", keep_tail) for r in raw]
    ds = ExportDataset(metadata={}, rows=rows)
    ds.metadata = {
        "request_id": request_id,
        "source": meta_source,
        "generated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "scope": {
            "tenant_id": ctx.tenant_id,
            "product_id": ctx.product_id,
            "module": ctx.module,
            "entity_scope": list(ctx.entities),
            "role": ctx.role,
        },
        "record_count": len(rows),
        "truncated": truncated,
        "checksum": checksum(ds.row_ids),
        "classification": max(kinds, key=lambda k: CLEARANCE_RANK.get(k, 1)),
        "columns": list(COLUMNS),
    }
    return ds
