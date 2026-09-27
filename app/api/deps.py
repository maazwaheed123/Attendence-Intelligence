"""Gateway dependencies: authentication, context validation, RBAC, rate limiting.

Order for every protected request:
  1. Bearer token present and cryptographically valid (401 otherwise)
  2. Claims well-formed; clearance within role ceiling (401)
  3. Product/tenant/module/entities exist and belong together (403)
  4. Rate limit (429)
  5. Permission for the endpoint (403)
  6. Any context repeated in the body must match the token (403 CONTEXT_MISMATCH)
The resulting SecurityContext is the ONLY source of isolation values downstream.
"""

from collections.abc import Callable, Iterator

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.errors import AppError
from app.config import get_settings
from app.db.session import get_engine, scoped_session
from app.security import ratelimit
from app.security.context import ClaimsError, SecurityContext
from app.security.jwt import TokenError, verify_token
from app.security.rbac import Permission, has_permission

_bearer = HTTPBearer(auto_error=False, description="JWT from /v1/auth/dev-token (dev only)")


class Unauthenticated(AppError):
    def __init__(self, message: str):
        super().__init__(401, "UNAUTHENTICATED", message)


def _validate_against_registry(ctx: SecurityContext) -> None:
    s = get_settings()
    if ctx.module not in s.allowed_modules_list:
        raise AppError(403, "FORBIDDEN", "Module not enabled.")
    with Session(get_engine("app")) as session:
        registered = session.execute(
            text("SELECT 1 FROM tenant_products WHERE tenant_id = :t AND product_id = :p"),
            {"t": ctx.tenant_id, "p": ctx.product_id},
        ).first()
    if not registered:
        raise AppError(403, "FORBIDDEN", "Tenant is not registered for this product.")
    if not ctx.all_entities:
        with scoped_session(ctx.to_scope(), role="app") as session:
            known = set(session.execute(text("SELECT entity_id FROM entities")).scalars())
        if not set(ctx.entities) <= known:
            raise AppError(403, "FORBIDDEN", "Token references an unknown entity.")


def get_security_context(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> SecurityContext:
    if creds is None or creds.scheme.lower() != "bearer" or not creds.credentials:
        raise Unauthenticated("Missing bearer token.")
    try:
        ctx = SecurityContext.from_claims(verify_token(creds.credentials))
    except TokenError as exc:
        raise Unauthenticated(str(exc).capitalize() + ".") from exc
    except ClaimsError as exc:
        raise Unauthenticated("Invalid token claims.") from exc
    _validate_against_registry(ctx)
    request.state.ctx = ctx  # for audit middleware
    if not ratelimit.allow(ctx.sub):
        raise AppError(429, "RATE_LIMITED", "Too many requests.")
    return ctx


def require(permission: Permission) -> Callable[..., SecurityContext]:
    def _dep(ctx: SecurityContext = Depends(get_security_context)) -> SecurityContext:
        if not has_permission(ctx.role, permission):
            raise AppError(403, "FORBIDDEN", f"Role '{ctx.role}' may not {permission.value}.")
        return ctx

    return _dep


def enforce_request_context(
    ctx: SecurityContext,
    *,
    product_id: str | None = None,
    tenant_id: str | None = None,
    module: str | None = None,
    entity_id: str | None = None,
) -> None:
    """Body/form fields may repeat the context but never widen or change it."""
    for name, value in (("product_id", product_id), ("tenant_id", tenant_id), ("module", module)):
        if value is not None and value != getattr(ctx, name):
            raise AppError(
                403, "CONTEXT_MISMATCH", f"{name} does not match the authenticated context."
            )
    if entity_id is not None and not ctx.all_entities and entity_id not in ctx.entities:
        raise AppError(403, "CONTEXT_MISMATCH", "entity_id is outside the authenticated scope.")


def db_reader(ctx: SecurityContext = Depends(get_security_context)) -> Iterator[Session]:
    """Request-scoped read session with the caller's isolation scope applied."""
    with scoped_session(ctx.to_scope(), role="reader") as session:
        yield session
