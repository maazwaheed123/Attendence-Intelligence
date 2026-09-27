"""DbScope: the isolation context applied to every database transaction.

The scope is written into transaction-local Postgres settings (set_config(..., true))
which the RLS policies read. Values are strictly validated AND passed as bound
parameters, so they can neither inject SQL nor smuggle extra entities in (e.g. a
comma inside an entity id would widen the entity list; it is rejected).
"""

import re
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.orm import Session

CLEARANCE_RANK = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}
ALL_ENTITIES = ("*",)

_SLUG = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_EMPLOYEE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_SUB = re.compile(r"^[A-Za-z0-9_.@:-]{0,128}$")


class ScopeError(ValueError):
    """Invalid isolation context. Never reaches the database."""


@dataclass(frozen=True)
class DbScope:
    product_id: str
    tenant_id: str
    module: str
    entities: tuple[str, ...]
    clearance: str
    employee_id: str | None = None
    sub: str = field(default="")

    def __post_init__(self):
        for name in ("product_id", "tenant_id", "module"):
            if not _SLUG.match(getattr(self, name) or ""):
                raise ScopeError(f"invalid {name}")
        if not self.entities:
            raise ScopeError("entities must not be empty")
        if self.entities != ALL_ENTITIES and not all(_SLUG.match(e) for e in self.entities):
            raise ScopeError("invalid entity id")
        if self.clearance not in CLEARANCE_RANK:
            raise ScopeError("invalid clearance")
        if self.employee_id is not None and not _EMPLOYEE.match(self.employee_id):
            raise ScopeError("invalid employee_id")
        if not _SUB.match(self.sub):
            raise ScopeError("invalid sub")

    @property
    def all_entities(self) -> bool:
        return self.entities == ALL_ENTITIES

    def settings(self) -> dict[str, str]:
        return {
            "product_id": self.product_id,
            "tenant_id": self.tenant_id,
            "module": self.module,
            "entities": "*" if self.all_entities else ",".join(self.entities),
            "clearance": str(CLEARANCE_RANK[self.clearance]),
            "employee_id": self.employee_id or "",
            "sub": self.sub,
        }


def apply_scope(session: Session, scope: DbScope) -> None:
    """Write the scope into the CURRENT transaction only (is_local = true)."""
    for key, value in scope.settings().items():
        session.execute(
            text("SELECT set_config(:key, :value, true)"), {"key": f"app.{key}", "value": value}
        )
