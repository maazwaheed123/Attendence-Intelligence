"""Liveness and dependency diagnostics.

/v1/health/deep reports each dependency without exposing URLs or secrets. LLM
providers being down makes the service 'degraded', not 'down': structured answers
still work through the deterministic template fallback.
"""

import time

from fastapi import APIRouter
from sqlalchemy import text

from app.config import get_settings
from app.db.session import get_engine

router = APIRouter(prefix="/v1", tags=["health"])


@router.get("/health", summary="Liveness check")
def health() -> dict:
    s = get_settings()
    return {"status": "ok", "service": s.app_name, "version": s.app_version, "env": s.app_env}


def _timed(fn):
    start = time.perf_counter()
    try:
        info = fn() or {}
        return {"status": "ok", **info, "latency_ms": int((time.perf_counter() - start) * 1000)}
    except Exception as exc:  # noqa: BLE001 - diagnostics report any failure
        return {"status": "down", "error": exc.__class__.__name__}


def _database():
    with get_engine("app").connect() as c:
        c.execute(text("SELECT 1"))
        ext = dict(c.execute(text("SELECT extname, extversion FROM pg_extension")).all())
    return {"pgvector": ext.get("vector"), "pg_trgm": ext.get("pg_trgm")}


def _redis():
    from app.security.ratelimit import get_redis

    get_redis().ping()


def _queue():
    from rq import Queue
    from rq.registry import FailedJobRegistry

    from app.ingestion.worker import get_queue_connection

    q = Queue("ingestion", connection=get_queue_connection())
    return {"queued": q.count, "failed": FailedJobRegistry(queue=q).count}


def _ocr():
    import pytesseract

    return {"tesseract": str(pytesseract.get_tesseract_version())}


def _vector():
    with get_engine("app").connect() as c:
        index = c.execute(
            text("SELECT 1 FROM pg_indexes WHERE indexname = 'ix_chunks_embedding'")
        ).scalar()
    return {"engine": "pgvector hnsw (cosine)", "status": "ok" if index else "down"}


def _embedder():
    from app.retrieval.embeddings import get_embedder

    e = get_embedder()
    info = {"provider": e.name, "model": e.model, "dim": e.dim, **e.ping()}
    return {**info, "status": "ok" if info.get("model_available") else "down"}


def _providers(router_getter):
    try:
        return router_getter().health(ping=True)
    except Exception as exc:  # noqa: BLE001
        return [{"provider": "router", "status": "down", "error": exc.__class__.__name__}]


@router.get("/health/deep", summary="Dependency diagnostics (DB, cache/queue, search, OCR, models)")
def health_deep() -> dict:
    from app.generation.router import get_router, get_vision_router

    s = get_settings()
    db = _timed(_database)
    components = {
        "service": {"status": "ok", "version": s.app_version},
        "database": db,
        "search": {"status": db["status"], "engine": "postgres full-text + pg_trgm"},
        "vector": _timed(_vector) if db.get("pgvector") else {"status": "down"},
        "embeddings": _timed(_embedder),
        "cache": _timed(_redis),
        "queue": _timed(_queue),
        "ocr": _timed(_ocr),
    }
    llm = _providers(get_router)
    vision = _providers(get_vision_router)
    for p in llm + vision:
        p.setdefault("status", "ok" if p.get("reachable") and p.get("model_available") else "down")
    components["llm_providers"] = llm
    components["vision_providers"] = vision
    components["llm_fallback"] = {
        "status": "ok",
        "engine": "deterministic template",
        "enabled": "template" in s.llm_chain_list,
    }

    core_ok = all(components[k]["status"] == "ok" for k in ("database", "cache"))
    llm_ok = any(p["status"] == "ok" for p in llm)
    # Models down = degraded, not down: structured answers use the template engine and
    # ingestion defers embeddings.
    optional_ok = llm_ok and components["embeddings"]["status"] == "ok"
    status = "ok" if core_ok and optional_ok else ("degraded" if core_ok else "down")
    return {"status": status, "components": components}
