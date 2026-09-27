"""Request-ID middleware: every request/response carries X-Request-ID.

A caller-supplied ID is accepted only if it is short and safe; otherwise a new
one is generated (prevents log injection through the header).
"""

import logging
import re
import time
import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

log = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


def new_request_id() -> str:
    return f"req_{uuid.uuid4().hex}"


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        incoming = request.headers.get(REQUEST_ID_HEADER, "")
        rid = incoming if _SAFE_ID.match(incoming) else new_request_id()
        request.state.request_id = rid
        token = request_id_var.set(rid)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # Handle here (not only in the app-level handler, which runs outside
            # this middleware) so even 500s carry the request ID.
            log.exception("unhandled error request_id=%s", rid)
            response = JSONResponse(
                status_code=500,
                content={
                    "error": {
                        "code": "INTERNAL_ERROR",
                        "message": "An internal error occurred.",
                        "request_id": rid,
                    }
                },
            )
        finally:
            request_id_var.reset(token)
        response.headers[REQUEST_ID_HEADER] = rid
        response.headers["X-Response-Time-ms"] = f"{(time.perf_counter() - start) * 1000:.1f}"
        return response
