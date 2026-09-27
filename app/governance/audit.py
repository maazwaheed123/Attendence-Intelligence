"""Append-only audit trail.

Records who/what/when/outcome with the isolation context. Never records tokens,
secrets or raw question text (questions are stored redacted + hashed by the query
pipeline). Writing is best-effort: an audit outage must not take the API down,
but it is logged loudly.
"""

import json
import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.session import get_engine
from app.security.context import SecurityContext

log = logging.getLogger(__name__)

_COLUMNS = (
    "request_id",
    "sub",
    "product_id",
    "tenant_id",
    "module",
    "entity_scope",
    "role",
    "event_type",
    "query_mode",
    "retrieved_source_ids",
    "provider",
    "model",
    "fallback_path",
    "confidence",
    "outcome",
    "error_code",
    "latency_ms",
)
_FORBIDDEN_DETAIL_KEYS = {"token", "authorization", "password", "secret", "jwt"}


def context_fields(ctx: SecurityContext | None) -> dict:
    if ctx is None:
        return {}
    return {
        "sub": ctx.sub,
        "product_id": ctx.product_id,
        "tenant_id": ctx.tenant_id,
        "module": ctx.module,
        "entity_scope": list(ctx.entities),
        "role": ctx.role,
    }


def record(event_type: str, request_id: str, *, details: dict | None = None, **fields) -> None:
    unknown = set(fields) - set(_COLUMNS)
    if unknown:
        raise ValueError(f"unknown audit fields: {unknown}")
    details = {k: v for k, v in (details or {}).items() if k.lower() not in _FORBIDDEN_DETAIL_KEYS}
    row = {c: fields.get(c) for c in _COLUMNS}
    row.update(request_id=request_id, event_type=event_type, details=json.dumps(details))
    cols = ", ".join(row)
    vals = ", ".join("CAST(:details AS jsonb)" if c == "details" else f":{c}" for c in row)
    try:
        with Session(get_engine("app")) as s, s.begin():
            s.execute(text(f"INSERT INTO audit_events ({cols}) VALUES ({vals})"), row)  # noqa: S608
    except Exception:  # noqa: BLE001 - audit must never break the request path
        log.exception("AUDIT WRITE FAILED event=%s request_id=%s", event_type, request_id)
