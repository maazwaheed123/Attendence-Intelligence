import logging

from fastapi import FastAPI

from app.api import (
    routes_auth,
    routes_export,
    routes_feedback,
    routes_health,
    routes_ingest,
    routes_me,
    routes_query,
)
from app.api.errors import register_error_handlers
from app.api.middleware import RequestIdMiddleware
from app.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)

    app = FastAPI(
        title="Attendance Intelligence API",
        version=settings.app_version,
        description=(
            "Enterprise RAG attendance microservice: ingestion, normalization, "
            "isolated retrieval, grounded answers with citations, feedback and export."
        ),
    )
    app.add_middleware(RequestIdMiddleware)
    register_error_handlers(app)
    app.include_router(routes_health.router)
    app.include_router(routes_me.router)
    app.include_router(routes_ingest.router)
    app.include_router(routes_query.router)
    app.include_router(routes_feedback.router)
    app.include_router(routes_export.router)
    if settings.dev_tokens_enabled:  # never mounted in prod
        app.include_router(routes_auth.router)
    return app


app = create_app()
