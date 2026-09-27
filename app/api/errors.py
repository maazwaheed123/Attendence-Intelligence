"""Uniform error envelope: {"error": {"code", "message", "request_id"}}.

Internal exception details are never returned to the caller.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger(__name__)

STATUS_CODES = {
    400: "BAD_REQUEST",
    401: "UNAUTHENTICATED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    413: "PAYLOAD_TOO_LARGE",
    415: "UNSUPPORTED_FILE",
    422: "VALIDATION_ERROR",
    429: "RATE_LIMITED",
    503: "PROVIDER_UNAVAILABLE",
}


class AppError(Exception):
    """Raise for expected, caller-facing errors with a specific code."""

    def __init__(self, status_code: int, code: str, message: str, headers: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers


def _rid(request: Request) -> str:
    return getattr(request.state, "request_id", "-")


def envelope(request: Request, status: int, code: str, message: str, headers=None):
    headers = {**(headers or {}), "X-Error-Code": code}  # lets the audit trail record the code
    if status == 401:
        headers.setdefault("WWW-Authenticate", "Bearer")
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "request_id": _rid(request)}},
        headers=headers,
    )


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError):
        return envelope(request, exc.status_code, exc.code, exc.message, exc.headers)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        code = STATUS_CODES.get(exc.status_code, "HTTP_ERROR")
        message = exc.detail if isinstance(exc.detail, str) else code
        return envelope(request, exc.status_code, code, message, getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        fields = [".".join(str(p) for p in e.get("loc", [])) for e in exc.errors()]
        return envelope(request, 422, "VALIDATION_ERROR", f"Invalid fields: {', '.join(fields)}")

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        log.exception("unhandled error request_id=%s", _rid(request))
        return envelope(request, 500, "INTERNAL_ERROR", "An internal error occurred.")
