from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import db_reader, get_security_context, require
from app.api.errors import AppError
from app.db.session import scoped_session
from app.security.context import SecurityContext
from app.security.rbac import ROLE_PERMISSIONS, Permission

router = APIRouter(prefix="/v1", tags=["context"])


@router.get("/me", summary="The authenticated context (what the gateway will enforce)")
def me(
    ctx: SecurityContext = Depends(get_security_context), db: Session = Depends(db_reader)
) -> dict:
    visible_entities = sorted(db.execute(text("SELECT entity_id FROM entities")).scalars())
    return {
        "context": ctx.public(),
        "permissions": sorted(p.value for p in ROLE_PERMISSIONS[ctx.role]),
        "visible_entities": visible_entities,
    }


@router.get("/audit", summary="Audit events for the caller's tenant/product")
def audit(
    request_id: str | None = None,
    limit: int = 50,
    ctx: SecurityContext = Depends(require(Permission.AUDIT_READ)),
) -> dict:
    if not 1 <= limit <= 500:
        raise AppError(422, "VALIDATION_ERROR", "limit must be between 1 and 500")
    sql = (
        "SELECT event_id, request_id, ts, sub, role, event_type, query_mode, outcome, "
        "error_code, latency_ms, provider, model, fallback_path, confidence, details "
        "FROM audit_events WHERE (CAST(:rid AS text) IS NULL OR request_id = :rid) "
        "ORDER BY event_id DESC LIMIT :lim"
    )
    with scoped_session(ctx.to_scope(), role="app") as s:  # audit read policy = own tenant
        rows = s.execute(text(sql), {"rid": request_id, "lim": limit}).mappings().all()
    return {"events": [dict(r) for r in rows]}
