"""Request-ID + audit middleware.

- Every request/response carries X-Request-ID. A caller-supplied ID is accepted
  only if short and safe (prevents log injection through the header).
- Every API request (except health/docs) is written to the audit trail with the
  authenticated context, status and latency. Tokens are never recorded.
"""

import logging
import re
import time
import uuid
from contextvars import ContextVar

from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.governance import audit

log = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_UNAUDITED_PREFIXES = ("/v1/health", "/docs", "/openapi.json", "/redoc")

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


def new_request_id() -> str:
    return f"req_{uuid.uuid4().hex}"


def _outcome(status: int) -> str:
    if status < 400:
        return "ok"
    if status in (401, 403):
        return "denied"
    if status == 429:
        return "rate_limited"
    return "client_error" if status < 500 else "server_error"


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        incoming = request.headers.get(REQUEST_ID_HEADER, "")
        rid = incoming if _SAFE_ID.match(incoming) else new_request_id()
        request.state.request_id = rid
        token = request_id_var.set(rid)
        start = time.perf_counter()
        error_code = None
        try:
            response = await call_next(request)
        except Exception:
            log.exception("unhandled error request_id=%s", rid)
            error_code = "INTERNAL_ERROR"
            response = JSONResponse(
                status_code=500,
                content={
                    "error": {
                        "code": error_code,
                        "message": "An internal error occurred.",
                        "request_id": rid,
                    }
                },
            )
        finally:
            request_id_var.reset(token)
        latency_ms = int((time.perf_counter() - start) * 1000)
        response.headers[REQUEST_ID_HEADER] = rid
        response.headers["X-Response-Time-ms"] = str(latency_ms)

        path = request.url.path
        if not path.startswith(_UNAUDITED_PREFIXES):
            ctx = getattr(request.state, "ctx", None)
            await run_in_threadpool(
                audit.record,
                "http_request",
                rid,
                outcome=_outcome(response.status_code),
                error_code=error_code or response.headers.get("X-Error-Code"),
                latency_ms=latency_ms,
                details={"method": request.method, "path": path, "status": response.status_code},
                **audit.context_fields(ctx),
            )
        return response
