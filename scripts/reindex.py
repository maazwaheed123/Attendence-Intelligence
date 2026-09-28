"""Backfill row cards and embed every active chunk that has no vector yet (after an
embedding outage, for data ingested before Step 9, or after a model change: --reset).

Usage (inside the api container):
    python -m scripts.reindex            # fill missing embeddings
    python -m scripts.reindex --reset    # re-embed everything (new model)
Maintenance tool: runs as the schema owner across all tenants.
"""

import argparse

from sqlalchemy import text

from app.db.session import owner_session
from app.retrieval import indexing
from app.retrieval.embeddings import get_embedder

BATCH = 256


def reindex(reset: bool = False) -> dict:
    embedder = get_embedder()
    total = 0
    with owner_session() as s:
        docs = (
            s.execute(text("SELECT DISTINCT source_document_id FROM attendance_records"))
            .scalars()
            .all()
        )
        cards = sum(indexing.create_row_cards(s, d) for d in docs)  # idempotent
    if reset:
        with owner_session() as s:
            s.execute(text("UPDATE document_chunks SET embedding = NULL"))
    while True:
        with owner_session() as s:
            n = indexing.embed_pending(s, embedder, limit=BATCH)["embedded"]
        total += n
        if n < BATCH:
            break
    with owner_session() as s:
        pending = indexing.count_pending(s)
    return {"row_cards": cards, "embedded": total, "pending": pending, "model": embedder.model}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reset", action="store_true", help="re-embed every chunk")
    print("reindex:", reindex(parser.parse_args().reset))


if __name__ == "__main__":
    main()
