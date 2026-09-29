"""Index maintenance: row cards for canonical records, and embeddings for chunks.

Both work on whatever session they are given: the ingestion pipeline passes its
RLS-scoped app session (so it can only touch the uploader's rows), the reindex
script passes the owner session (maintenance across tenants).
Embedding is idempotent: only active chunks whose embedding is still NULL are
embedded, so an outage just leaves work for the next run.
"""

import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.ingestion.chunking import row_card_text
from app.retrieval.embeddings import Embedder
from app.security import pii

NS_CARD = uuid.UUID("0b6e3f0c-9a51-4c3f-8f0e-6d3f8f2f7a11")


def card_id(record_id) -> uuid.UUID:
    return uuid.uuid5(NS_CARD, str(record_id))


def create_row_cards(db: Session, document_id) -> int:
    """One row_card chunk per canonical record of the document (idempotent)."""
    records = (
        db.execute(
            text(
                "SELECT record_id, source_document_id, product_id, tenant_id, module, entity_id, "
                "employee_id, employee_name, department, classification, attendance_date, status, "
                "check_in, check_out, source_file, source_locator, review_required, is_active "
                "FROM attendance_records WHERE source_document_id = :d"
            ),
            {"d": document_id},
        )
        .mappings()
        .all()
    )
    rows = []
    for r in records:
        card, _ = pii.mask_text(row_card_text(r))
        rows.append(
            {
                "c": card_id(r["record_id"]),
                "d": r["source_document_id"],
                "rid": r["record_id"],
                "p": r["product_id"],
                "t": r["tenant_id"],
                "m": r["module"],
                "e": r["entity_id"],
                "emp": r["employee_id"],
                "cls": r["classification"],
                "txt": card,
                "loc": r["source_locator"],
                "active": r["is_active"],
            }
        )
    if rows:
        db.execute(
            text(
                """INSERT INTO document_chunks (chunk_id, source_document_id, record_id,
                   product_id, tenant_id, module, entity_id, employee_id, classification,
                   chunk_type, text, text_masked, locator, is_active)
                   VALUES (:c, :d, :rid, :p, :t, :m, :e, :emp, :cls, 'row_card', :txt, :txt,
                           :loc, :active)
                   ON CONFLICT (chunk_id) DO NOTHING"""
            ),
            rows,
        )
    return len(rows)


def to_pgvector(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in v) + "]"


def embed_pending(
    db: Session, embedder: Embedder, *, document_id=None, limit: int | None = None
) -> dict:
    """Embed active chunks without a vector. All vectors are computed before any
    write, so a backend failure (EmbeddingUnavailable) leaves the rows untouched."""
    where = "embedding IS NULL AND is_active"
    params: dict = {}
    if document_id is not None:
        where += " AND source_document_id = :d"
        params["d"] = document_id
    sql = f"SELECT chunk_id, text_masked FROM document_chunks WHERE {where} ORDER BY chunk_id"  # noqa: S608
    if limit:
        sql += f" LIMIT {int(limit)}"
    pending = db.execute(text(sql), params).all()
    if not pending:
        return {"embedded": 0, "model": embedder.model}
    vectors = embedder.embed_documents([p.text_masked for p in pending])
    db.execute(
        text("UPDATE document_chunks SET embedding = CAST(:v AS vector) WHERE chunk_id = :c"),
        [{"c": p.chunk_id, "v": to_pgvector(v)} for p, v in zip(pending, vectors, strict=True)],
    )
    return {"embedded": len(pending), "model": embedder.model}


def count_pending(db: Session, document_id=None) -> int:
    where = "embedding IS NULL AND is_active" + (
        " AND source_document_id = :d" if document_id else ""
    )
    return db.execute(
        text(f"SELECT count(*) FROM document_chunks WHERE {where}"),  # noqa: S608
        {"d": document_id} if document_id else {},
    ).scalar_one()
