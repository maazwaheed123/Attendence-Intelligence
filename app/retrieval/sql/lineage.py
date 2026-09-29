"""Citations for structured answers, built by the system, never by the model.

The citation query reuses the row filter of the query that produced the answer
(template WHERE with its bind params, or the WHERE of the validated LLM AST) and,
for grouped results, restricts to the group keys actually returned. It runs as
rag_reader in the caller's scope, so every citation is a real, in-scope record.
Conflicted employee-days cite every contributing source record.
"""

from dataclasses import dataclass, field

from sqlglot import exp
from sqlglot.errors import SqlglotError

from app.retrieval.sql import executor
from app.retrieval.sql.validator import VIEW_COLUMNS, parse_validated
from app.retrieval.types import Slots
from app.security.scope import DbScope

MAX_CITATIONS = 50
_COLS = (
    "record_id, source_file, source_locator, employee_id, employee_name, attendance_date, "
    "status, conflict, source_record_ids, extraction_confidence"
)
_ORDER = "ORDER BY attendance_date, employee_id, record_id"


@dataclass
class Lineage:
    citations: list[dict] = field(default_factory=list)
    total: int = 0
    mean_confidence: float | None = None
    conflict_days: int = 0
    available: bool = True


def excerpt(r: dict) -> str:
    parts = [
        str(r["attendance_date"]),
        f"{r['employee_id']} {r.get('employee_name') or ''}".strip(),
    ]
    parts.append(str(r["status"]))
    if r.get("conflict"):
        parts.append("conflicting sources: " + "/".join(r.get("reported_statuses") or []))
    return " | ".join(parts)


def _citation(r: dict) -> dict:
    return {
        "record_id": r["record_id"],
        "source_file": r["source_file"],
        "locator": r["source_locator"],
        "excerpt": excerpt(r),
    }


def _collect(
    scope: DbScope, where: str, params: dict | None, source: str = "v_attendance"
) -> Lineage:
    """params=None: model-derived WHERE text, run verbatim (still rag_reader under RLS)."""
    rows = executor.run(
        scope, f"SELECT {_COLS} FROM {source} WHERE {where} {_ORDER} LIMIT {MAX_CITATIONS}", params
    ).rows
    stats = executor.run(
        scope,
        "SELECT sum(CASE WHEN conflict THEN source_count ELSE 1 END) AS n, "
        "avg(extraction_confidence) AS conf, "
        f"count(*) FILTER (WHERE conflict) AS conflicts FROM {source} WHERE {where}",
        params,
    ).rows[0]
    conflict_ids = [i for r in rows if r["conflict"] for i in r["source_record_ids"]]
    by_id = {c["record_id"]: c for c in (_citation(r) for r in rows if not r["conflict"])}
    for r in source_records(scope, conflict_ids):
        by_id.setdefault(r["record_id"], _citation({**r, "conflict": False}))
    return Lineage(
        citations=list(by_id.values())[:MAX_CITATIONS],
        total=stats["n"] or 0,
        mean_confidence=None if stats["conf"] is None else round(float(stats["conf"]), 4),
        conflict_days=stats["conflicts"],
    )


def source_records(scope: DbScope, record_ids: list[str]) -> list[dict]:
    """The individual records behind a (conflicted) view row, in scope."""
    if not record_ids:
        return []
    return executor.run(
        scope,
        "SELECT record_id, source_file, source_locator, employee_id, employee_name, "
        "attendance_date, status FROM attendance_records "
        "WHERE record_id = ANY(CAST(:ids AS uuid[])) "
        "ORDER BY attendance_date, employee_id, source_file",
        {"ids": record_ids},
    ).rows


def for_template(scope: DbScope, where: str, params: dict, group: dict | None = None) -> Lineage:
    params = dict(params)
    for i, (col, values) in enumerate((group or {}).items()):
        where += f" AND {col} = ANY(:_group{i})"
        params[f"_group{i}"] = list(values)
    return _collect(scope, where, params)


class NoLineage(Exception):
    """The query shape allows no single row filter (e.g. a self-join)."""


def citation_filter(sql: str, rows: list[dict]) -> tuple[str, str]:
    """(FROM source, WHERE text) selecting the rows behind validated LLM SQL.

    Reuses the query's WHERE and, for GROUP BY queries, adds `key IN (returned keys)`
    so a LIMIT-ed ranking cites only the groups it actually returned.
    """
    try:
        root = parse_validated(sql)
    except (ValueError, SqlglotError) as exc:
        raise NoLineage("unparseable") from exc
    if root.find(exp.Join):
        raise NoLineage("join")
    table = root.find(exp.Table)
    alias = table.alias if table is not None else ""
    where = root.args.get("where")
    cond: exp.Expression = where.this.copy() if where is not None else exp.true()
    group = root.args.get("group")
    if group is not None and rows:
        for g in group.expressions:
            name = g.name.lower() if isinstance(g, exp.Column) else ""
            if name not in VIEW_COLUMNS or name not in rows[0]:
                continue
            values = sorted({str(r[name]) for r in rows if r.get(name) is not None})
            literals = [exp.Literal.string(v) for v in values] or [exp.null()]
            cond = exp.and_(cond, exp.column(name).isin(*literals))
    source = f"v_attendance AS {alias}" if alias else "v_attendance"
    return source, cond.sql(dialect="postgres")


def for_llm_sql(scope: DbScope, sql: str, rows: list[dict]) -> Lineage:
    """Citations for validated LLM SQL (model-derived WHERE runs verbatim, under RLS)."""
    try:
        source, where = citation_filter(sql, rows)
    except NoLineage:
        return Lineage(available=False)
    return _collect(scope, where, None, source)


def pending_review(scope: DbScope, s: Slots) -> list[dict]:
    """Active records that match the question's filters but still await human review."""
    conds = ["review_required", "is_active", "attendance_date BETWEEN :d1 AND :d2"]
    params: dict = {"d1": s.date_from, "d2": s.date_to}
    if s.employee_id:
        conds.append("employee_id = :emp")
        params["emp"] = s.employee_id
    if s.entity_id:
        conds.append("entity_id = :ent")
        params["ent"] = s.entity_id
    return executor.run(
        scope,
        "SELECT record_id, source_file, source_locator, employee_id, employee_name, "
        f"attendance_date, status, extraction_confidence FROM attendance_records "
        f"WHERE {' AND '.join(conds)} ORDER BY attendance_date, employee_id",
        params,
    ).rows
