"""Database engines per role, and the scoped session every request must use.

Roles:
  reader  rag_reader  SELECT-only, non-PII columns; used for ALL retrieval
  app     app_rw      application writes (ingestion, feedback, responses, audit)
  owner   schema owner; migrations / seeding / test setup ONLY, never at request time
"""

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from typing import Literal

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.config import get_settings
from app.security.scope import DbScope, apply_scope

Role = Literal["reader", "app", "owner"]
READER_STATEMENT_TIMEOUT = "3s"


@lru_cache
def get_engine(role: Role) -> Engine:
    s = get_settings()
    url = {
        "reader": s.database_url_reader,
        "app": s.database_url_app,
        "owner": s.database_url_owner,
    }[role].get_secret_value()
    return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5)


def dispose_engines() -> None:
    for role in ("reader", "app", "owner"):
        if get_engine.cache_info().currsize:
            get_engine(role).dispose()
    get_engine.cache_clear()


@contextmanager
def scoped_session(scope: DbScope, role: Literal["reader", "app"] = "reader") -> Iterator[Session]:
    """One transaction with the isolation scope applied. Commits on success.

    The scope lives in transaction-local settings, so it cannot leak to the next
    user of the pooled connection.
    """
    with Session(get_engine(role), expire_on_commit=False) as session, session.begin():
        if role == "reader":
            session.execute(text("SET TRANSACTION READ ONLY"))
            session.execute(text(f"SET LOCAL statement_timeout = '{READER_STATEMENT_TIMEOUT}'"))
        apply_scope(session, scope)
        yield session


@contextmanager
def owner_session() -> Iterator[Session]:
    """Schema-owner session (bypasses RLS). Setup and maintenance only."""
    with Session(get_engine("owner"), expire_on_commit=False) as session, session.begin():
        yield session
