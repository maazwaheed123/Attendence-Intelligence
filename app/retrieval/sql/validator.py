"""Allow-list validator for LLM-written SQL (sqlglot AST, postgres dialect).

Everything not explicitly allowed is rejected: one SELECT over v_attendance,
known columns, a short list of functions, safe casts, a bounded LIMIT. This is a
defence layer, not the isolation boundary: a query that passes (even one that
filters on tenant_id = 'tenant_b') still runs under RLS and sees only the
caller's rows.
"""

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

VIEW = "v_attendance"
MAX_LIMIT = 500

VIEW_COLUMNS = frozenset(
    {
        "record_id",
        "product_id",
        "tenant_id",
        "module",
        "entity_id",
        "classification",
        "employee_id",
        "employee_name",
        "department",
        "attendance_date",
        "status",
        "reported_statuses",
        "check_in",
        "check_out",
        "total_hours",
        "source_file",
        "source_locator",
        "extraction_method",
        "extraction_confidence",
        "conflict",
        "source_record_ids",
        "source_count",
        "is_scheduled",
        "present_value",
    }
)

# sqlglot function classes (exp.Func subclasses). exp.Anonymous = any function sqlglot
# does not know (pg_sleep, set_config, current_setting, dblink, lo_import, ...) and is
# therefore always rejected.
ALLOWED_FUNCTIONS = frozenset(
    {
        "And",  # boolean connectors are Func subclasses in sqlglot
        "Or",
        "Count",
        "Sum",
        "Avg",
        "Min",
        "Max",
        "Round",
        "Coalesce",
        "Nullif",
        "DateTrunc",
        "TimestampTrunc",
        "Extract",
        "Case",
        "If",  # WHEN branches inside CASE
        "Cast",
        "TryCast",
        "Lower",
        "Upper",
        "TimeToStr",  # TO_CHAR
        "ToChar",
    }
)
ALLOWED_CAST_TYPES = frozenset(
    {
        "INT",
        "BIGINT",
        "SMALLINT",
        "DECIMAL",
        "DOUBLE",
        "FLOAT",
        "TEXT",
        "VARCHAR",
        "CHAR",
        "DATE",
        "TIME",
        "TIMESTAMP",
        "BOOLEAN",
    }
)
# Node types that must never appear anywhere in the tree.
FORBIDDEN_NODES = (
    exp.Union,
    exp.Intersect,
    exp.Except,
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.Command,
    exp.Copy,
    exp.Set,
    exp.Into,
    exp.Lock,
    exp.With,
    exp.Parameter,
    exp.Placeholder,
    exp.ObjectIdentifier,
)
_FORBIDDEN_TEXT = ("--", "/*", "*/", "$", "\\")


class SqlRejected(ValueError):
    """Why the SQL was rejected; the message is safe to show the LLM on retry."""


def _strip(sql: str) -> str:
    sql = (sql or "").strip()
    if sql.endswith(";"):
        sql = sql[:-1].rstrip()
    return sql


def _check_text(sql: str) -> None:
    if not sql:
        raise SqlRejected("empty SQL")
    if len(sql) > 4000:
        raise SqlRejected("SQL too long")
    if ";" in sql:
        raise SqlRejected("multiple statements are not allowed")
    for token in _FORBIDDEN_TEXT:
        if token in sql:
            raise SqlRejected(f"forbidden token {token!r} (comments, dollar quoting, escapes)")


def _parse(sql: str) -> exp.Select:
    try:
        statements = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
    except SqlglotError as exc:
        raise SqlRejected(f"SQL could not be parsed: {str(exc)[:150]}") from exc
    if len(statements) != 1:
        raise SqlRejected("exactly one statement is required")
    root = statements[0]
    if not isinstance(root, exp.Select):
        raise SqlRejected(f"only SELECT is allowed (got {type(root).__name__.upper()})")
    return root


def _check_nodes(root: exp.Select) -> None:
    for node in root.walk():
        if isinstance(node, FORBIDDEN_NODES):
            raise SqlRejected(f"{type(node).__name__.upper()} is not allowed")
        if isinstance(node, exp.Func) and type(node).__name__ not in ALLOWED_FUNCTIONS:
            name = node.name if isinstance(node, exp.Anonymous) else node.sql_name()
            raise SqlRejected(f"function {str(name).upper()} is not allowed")
        if isinstance(node, exp.DataType) and node.this.name not in ALLOWED_CAST_TYPES:
            raise SqlRejected(f"cast to {node.sql('postgres')} is not allowed")


def _check_tables(root: exp.Select) -> set[str]:
    tables = list(root.find_all(exp.Table))
    if not tables:
        raise SqlRejected(f"the query must read from {VIEW}")
    aliases = {VIEW}
    for t in tables:
        if t.name.lower() != VIEW or t.args.get("db") or t.args.get("catalog"):
            raise SqlRejected(f"only the {VIEW} view may be queried (found {t.sql('postgres')})")
        if t.alias:
            aliases.add(t.alias.lower())
    if len(tables) > 2:
        raise SqlRejected(f"{VIEW} may be referenced at most twice (one self-join)")
    joins = list(root.find_all(exp.Join))
    if len(joins) > 1:
        raise SqlRejected("at most one join is allowed")
    for j in joins:
        if not (j.args.get("on") or j.args.get("using")) or (j.kind or "").upper() == "CROSS":
            raise SqlRejected("joins need an ON condition (no cross joins)")
    return aliases


def _check_columns(root: exp.Select, table_aliases: set[str]) -> None:
    output_aliases = {a.alias.lower() for a in root.find_all(exp.Alias) if a.alias}
    for col in root.find_all(exp.Column):
        name = col.name.lower()
        if col.table and col.table.lower() not in table_aliases:
            raise SqlRejected(f"unknown table reference {col.table!r}")
        if name not in VIEW_COLUMNS and name not in output_aliases:
            raise SqlRejected(f"unknown column {col.name!r}")


def _enforce_limit(root: exp.Select) -> exp.Select:
    limit = root.args.get("limit")
    if limit is None:
        return root.limit(MAX_LIMIT)
    value = limit.expression
    if not (isinstance(value, exp.Literal) and not value.is_string and value.this.isdigit()):
        raise SqlRejected("LIMIT must be a plain integer")
    if int(value.this) > MAX_LIMIT:
        return root.limit(MAX_LIMIT)
    return root


def validate(sql: str) -> str:
    """Return a normalized, safe SQL string or raise SqlRejected."""
    sql = _strip(sql)
    _check_text(sql)
    root = _parse(sql)
    _check_nodes(root)
    table_aliases = _check_tables(root)
    _check_columns(root, table_aliases)
    return _enforce_limit(root).sql(dialect="postgres")


def parse_validated(sql: str) -> exp.Select:
    """AST of SQL that already passed validate() (used for lineage)."""
    return _parse(_strip(sql))
