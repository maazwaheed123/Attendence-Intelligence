import logging

from fastapi import FastAPI

from app.api import routes_health
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
    return app


app = create_app()
