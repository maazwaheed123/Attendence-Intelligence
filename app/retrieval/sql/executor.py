"""Run structured SQL inside the caller's RLS scope as rag_reader.

scoped_session(role="reader") gives: SELECT-only role, READ ONLY transaction,
statement_timeout 3s, and the transaction-local isolation settings RLS reads.
"""

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

import psycopg
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from app.db.session import scoped_session
from app.security.scope import DbScope

MAX_ROWS = 500


class SqlExecutionError(RuntimeError):
    """The database refused or aborted the query (timeout, type error, ...)."""


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[dict]
    truncated: bool = False


def to_json(value):
    if isinstance(value, Decimal):
        return int(value) if value.as_tuple().exponent >= 0 else float(value)
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, dt.time):
        return value.strftime("%H:%M")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, list | tuple):
        return [to_json(v) for v in value]
    return value


def _run_verbatim(session, sql: str, limit: int) -> tuple[list[str], list[tuple]]:
    # psycopg only interprets placeholders when parameters are passed, so "%" in a
    # LIKE pattern or ":x" inside a literal stays literal. Same connection and
    # transaction as the session, so the scope settings apply.
    dbapi = session.connection().connection.driver_connection
    with dbapi.cursor() as cur:
        cur.execute(sql)
        return [d.name for d in cur.description], cur.fetchmany(limit)


def run(scope: DbScope, sql: str, params: dict | None = None, *, max_rows: int = MAX_ROWS):
    """Template SQL (bind params) or validated LLM SQL (params=None, sent verbatim)."""
    try:
        with scoped_session(scope, role="reader") as s:
            if params is None:
                columns, raw = _run_verbatim(s, sql, max_rows + 1)
            else:
                cursor = s.execute(text(sql), params)
                columns, raw = list(cursor.keys()), cursor.fetchmany(max_rows + 1)
    except (DBAPIError, SQLAlchemyError, psycopg.Error) as exc:
        detail = getattr(exc, "orig", None) or exc
        raise SqlExecutionError(str(detail).splitlines()[0][:200]) from exc
    rows = [{c: to_json(v) for c, v in zip(columns, r, strict=True)} for r in raw[:max_rows]]
    return QueryResult(columns, rows, truncated=len(raw) > max_rows)
