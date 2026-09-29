"""Vector and keyword stores over document_chunks (pgvector HNSW, Postgres FTS, pg_trgm).

Every search method takes the caller's session as its first argument and refuses
one without an isolation scope: RLS inside Postgres is what filters the rows, so
a search can only ever run inside a scoped transaction (fail-closed). Stores are
interfaces (Protocols) so Qdrant / OpenSearch adapters can replace them later.

HNSW + RLS: the index returns nearest neighbours before RLS filters them, which
could starve results for a small tenant. hnsw.iterative_scan = relaxed_order
(pgvector >= 0.8) keeps scanning until enough rows pass the policy.
"""

import re
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.retrieval.indexing import to_pgvector

EF_SEARCH = 100
_COLS = (
    "chunk_id, source_document_id, record_id, chunk_type, text_masked, locator, "
    "entity_id, employee_id, classification, suspicious"
)
_ACTIVE = "is_active"


class UnscopedSession(RuntimeError):
    """A retrieval call without an isolation scope (programming error)."""


@dataclass
class Hit:
    chunk_id: str
    source_document_id: str
    record_id: str | None
    chunk_type: str
    text: str
    locator: str
    entity_id: str | None
    employee_id: str | None
    classification: str
    suspicious: bool
    score: float
    source: str


def require_scope(session: Session) -> None:
    tenant = session.execute(text("SELECT current_setting('app.tenant_id', true)")).scalar()
    if not tenant:
        raise UnscopedSession("retrieval requires scoped_session(ctx.to_scope())")


def _hits(rows, source: str) -> list[Hit]:
    return [
        Hit(
            chunk_id=str(r.chunk_id),
            source_document_id=str(r.source_document_id),
            record_id=str(r.record_id) if r.record_id else None,
            chunk_type=r.chunk_type,
            text=r.text_masked,
            locator=r.locator,
            entity_id=r.entity_id,
            employee_id=r.employee_id,
            classification=r.classification,
            suspicious=r.suspicious,
            score=float(r.score),
            source=source,
        )
        for r in rows
    ]


def _type_filter(chunk_types: tuple[str, ...] | None, params: dict) -> str:
    if not chunk_types:
        return ""
    params["types"] = list(chunk_types)
    return " AND chunk_type = ANY(:types)"


class VectorStore(Protocol):
    def search(
        self, session: Session, vector: list[float], k: int = 20, chunk_types=None
    ) -> list[Hit]: ...


class KeywordStore(Protocol):
    def search(self, session: Session, query: str, k: int = 20, chunk_types=None) -> list[Hit]: ...


class PgVectorStore:
    def search(self, session: Session, vector: list[float], k: int = 20, chunk_types=None):
        require_scope(session)
        session.execute(text(f"SET LOCAL hnsw.ef_search = {EF_SEARCH}"))
        session.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
        params = {"v": to_pgvector(vector), "k": k}
        where = _ACTIVE + " AND embedding IS NOT NULL" + _type_filter(chunk_types, params)
        rows = session.execute(
            text(
                f"SELECT {_COLS}, 1 - (embedding <=> CAST(:v AS vector)) AS score "  # noqa: S608
                f"FROM document_chunks WHERE {where} "
                "ORDER BY embedding <=> CAST(:v AS vector) LIMIT :k"
            ),
            params,
        ).all()
        return _hits(rows, "vector")


_TERM = re.compile(r"[a-z0-9]+")


def any_terms(query: str) -> str:
    """'a | b | c' for to_tsquery: natural questions match on ANY term (ranked by
    ts_rank_cd), instead of websearch_to_tsquery's AND of every word. Only [a-z0-9]
    tokens survive, so no tsquery syntax can be injected."""
    terms = dict.fromkeys(t for t in _TERM.findall(query.lower()) if len(t) > 1)
    return " | ".join(terms)


class PgFtsStore:
    """BM25-like keyword ranking with ts_rank_cd over the generated tsvector.

    match="all": websearch syntax, every term required; match="any": OR of terms."""

    def search(self, session: Session, query: str, k: int = 20, chunk_types=None, match="all"):
        require_scope(session)
        if match == "any":
            query = any_terms(query)
            if not query:
                return []
            tsq = "to_tsquery('english', :q)"
        else:
            tsq = "websearch_to_tsquery('english', :q)"
        params = {"q": query, "k": k}
        where = _ACTIVE + " AND tsv @@ q" + _type_filter(chunk_types, params)
        rows = session.execute(
            text(
                f"SELECT {_COLS}, ts_rank_cd(tsv, q) AS score "  # noqa: S608
                f"FROM document_chunks, {tsq} AS q "
                f"WHERE {where} ORDER BY score DESC, chunk_id LIMIT :k"
            ),
            params,
        ).all()
        return _hits(rows, "keyword")


class PgTrigramStore:
    """Fuzzy matching for ids, names and file names (typos, partial ids)."""

    def search(self, session: Session, query: str, k: int = 20, chunk_types=None):
        require_scope(session)
        params = {"q": query, "k": k}
        where = _ACTIVE + " AND :q <% text_masked" + _type_filter(chunk_types, params)
        rows = session.execute(
            text(
                f"SELECT {_COLS}, word_similarity(:q, text_masked) AS score "  # noqa: S608
                f"FROM document_chunks WHERE {where} ORDER BY score DESC, chunk_id LIMIT :k"
            ),
            params,
        ).all()
        return _hits(rows, "trigram")
