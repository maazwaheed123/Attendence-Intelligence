"""SecurityContext: the authenticated caller, derived ONLY from verified token claims."""

from pydantic import BaseModel, ConfigDict

from app.security.rbac import ROLES, clearance_allowed
from app.security.scope import DbScope, ScopeError


class ClaimsError(ValueError):
    pass


class SecurityContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    sub: str
    product_id: str
    tenant_id: str
    module: str
    role: str
    entities: tuple[str, ...]
    clearance: str
    employee_id: str | None = None
    token_id: str

    @classmethod
    def from_claims(cls, claims: dict) -> "SecurityContext":
        if not isinstance(claims.get("entities"), list):
            raise ClaimsError("entities must be a list")
        try:
            ctx = cls(
                sub=claims["sub"],
                product_id=claims["product_id"],
                tenant_id=claims["tenant_id"],
                module=claims["module"],
                role=claims["role"],
                entities=tuple(claims["entities"]),
                clearance=claims["clearance"],
                employee_id=claims.get("employee_id"),
                token_id=claims["jti"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ClaimsError("missing or malformed claims") from exc
        if ctx.role not in ROLES:
            raise ClaimsError("unknown role")
        if not clearance_allowed(ctx.role, ctx.clearance):
            raise ClaimsError("clearance exceeds role ceiling")
        if ctx.role == "employee" and not ctx.employee_id:
            raise ClaimsError("employee role requires employee_id")
        try:
            ctx.to_scope()  # format validation of every isolation value
        except ScopeError as exc:
            raise ClaimsError(str(exc)) from exc
        return ctx

    @property
    def all_entities(self) -> bool:
        return self.entities == ("*",)

    def to_scope(self) -> DbScope:
        return DbScope(
            product_id=self.product_id,
            tenant_id=self.tenant_id,
            module=self.module,
            entities=self.entities,
            clearance=self.clearance,
            employee_id=self.employee_id if self.role == "employee" else None,
            sub=self.sub,
        )

    def public(self) -> dict:
        """What may be echoed back to the caller (no token id)."""
        return {
            "sub": self.sub,
            "product_id": self.product_id,
            "tenant_id": self.tenant_id,
            "module": self.module,
            "role": self.role,
            "entity_scope": list(self.entities),
            "clearance": self.clearance,
            "employee_id": self.employee_id,
        }
